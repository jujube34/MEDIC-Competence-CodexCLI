"""P0 loopback-only Responses namespace adapter; not a production gateway.

Only function namespaces are flattened. Messages, call IDs, tool outputs and SSE
lifecycles remain Responses objects. Unsupported tool types fail explicitly.
"""
from __future__ import annotations

import copy
import hmac
import json
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from .common import digest
from .provider import Config, ProbeError, numeric_usage, parse_sse


class NamespaceMap:
    def __init__(self):
        self.aliases = {}

    def alias(self, namespace, name):
        alias = "medic_" + digest([namespace, name])[:32] + "_" + name[:20]
        original = (namespace, name)
        if alias in self.aliases and self.aliases[alias] != original:
            raise ProbeError("namespace_alias_collision")
        self.aliases[alias] = original
        return alias

    def request(self, original):
        body = copy.deepcopy(original)
        if body.get("previous_response_id"):
            raise ProbeError("stateful_response_ids_not_supported")
        flat, names = [], set()
        for definition in body.get("tools", []):
            if definition.get("type") == "namespace":
                namespace = definition["name"]
                children = definition["tools"]
            else:
                namespace, children = None, [definition]
            for child in children:
                if child.get("type") != "function" or child.get("defer_loading"):
                    raise ProbeError("unsupported_tool_type_or_deferral")
                tool = copy.deepcopy(child)
                if namespace:
                    tool["name"] = self.alias(namespace, child["name"])
                    tool["description"] = f'{namespace}.{child["name"]}: ' + tool.get("description", "")
                if tool["name"] in names:
                    raise ProbeError("duplicate_tool_identity")
                names.add(tool["name"])
                flat.append(tool)
        body["tools"] = flat
        for item in body.get("input", []) if isinstance(body.get("input"), list) else []:
            if item.get("type") == "function_call" and item.get("namespace"):
                item["name"] = self.alias(item.pop("namespace"), item["name"])
            if item.get("type") in ("custom_tool_call", "tool_search_call", "additional_tools"):
                raise ProbeError("unsupported_history_tool_type")
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            if choice.get("type") != "function":
                raise ProbeError("unsupported_tool_choice")
            if choice.get("namespace"):
                choice["name"] = self.alias(choice.pop("namespace"), choice["name"])
        return body

    def response(self, original):
        value = copy.deepcopy(original)
        def visit(obj):
            if isinstance(obj, list):
                for item in obj:
                    visit(item)
            elif isinstance(obj, dict):
                if obj.get("type") in ("function_call", "response.function_call_arguments.done") and obj.get("name") in self.aliases:
                    obj["namespace"], obj["name"] = self.aliases[obj["name"]]
                # Recurse only protocol containers, never tool arguments or user JSON/text.
                for key in ("output", "response", "item"):
                    if key in obj:
                        visit(obj[key])
        visit(value)
        return value


class Gateway:
    def __init__(self, upstream, transport=None, *, max_requests=40, max_output_tokens=2048,
                 max_concurrency=4, max_total_tokens=500000, request_timeout=90, ledger=None):
        self.upstream = upstream
        self.token = secrets.token_urlsafe(32)
        self.trace = []
        self.admission_rejections = []
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max_concurrency)
        self.charged_tokens = 0
        self.reserved_tokens = 0
        self.transport = transport
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def error(self, status, code):
                data = json.dumps({"error": {"code": code, "message": code}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def do_POST(self):
                if self.path != "/v1/responses":
                    self.error(404, "unsupported_route")
                    return
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + gateway.token):
                    self.error(401, "invalid_gateway_token")
                    return
                # Briefly queue actual concurrent requests. Completed responses
                # release their admission before exposing a terminal to clients.
                if not gateway.slots.acquire(timeout=2):
                    with gateway.lock:
                        gateway.admission_rejections.append({"code": "concurrency_limit", "status": 429})
                    self.error(429, "concurrency_limit")
                    return
                record = {"terminal": "not_observed"}
                started = time.monotonic()
                headers_sent = False
                stop_monitor = threading.Event()
                monitor = None
                reserved = 0
                reservation_id = None
                accounted = False
                released = False
                terminal_data = None

                def finish_accounting():
                    nonlocal accounted
                    if accounted:
                        return
                    accounted = True
                    stop_monitor.set()
                    if monitor:
                        monitor.join(timeout=1)
                    has_total = "total_tokens" in record.get("usage", {})
                    if reservation_id is not None:
                        try:
                            ledger.settle(reservation_id, record.get("usage", {}).get("total_tokens"))
                        except Exception as exc:
                            # Keep the durable reservation; do not announce a
                            # successful model step whose accounting did not finish.
                            record["accounting_error"] = type(exc).__name__
                    record["usage_source"] = "reported" if has_total else "conservative_estimate"
                    with gateway.lock:
                        gateway.reserved_tokens -= reserved
                        gateway.charged_tokens += (reserved if record.get('accounting_error')
                            else record.get("usage", {}).get("total_tokens", reserved))
                    if has_total and not record.get('accounting_error'):
                        record.pop("estimated_token_upper_bound", None)

                def release_slot():
                    nonlocal released
                    if not released:
                        released = True
                        gateway.slots.release()

                try:
                    with gateway.lock:
                        if len(gateway.trace) >= max_requests:
                            self.error(429, "request_budget")
                            return
                        gateway.trace.append(record)
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 2_000_000 or self.headers.get("Content-Encoding"):
                        self.error(400, "invalid_request_size_or_encoding")
                        return
                    self.connection.settimeout(60)
                    original = json.loads(self.rfile.read(size))
                    if original.get("model") != upstream.model:
                        raise ProbeError("model_mismatch")
                    mapper = NamespaceMap()
                    body = mapper.request(original)
                    if body.get("max_output_tokens", max_output_tokens) > max_output_tokens:
                        raise ProbeError("output_budget_exceeded")
                    body.setdefault("max_output_tokens", max_output_tokens)
                    record["tool_inventory"] = [{"namespace": tool.get("name") if tool.get("type") == "namespace" else None,
                                                 "tools": [x["name"] for x in tool["tools"]] if tool.get("type") == "namespace" else [tool.get("name")]}
                                                for tool in original.get("tools", [])]
                    record["namespaced_tools"] = len(mapper.aliases)
                    record["request_sha256"] = digest(original)
                    record["stream"] = bool(body.get("stream"))
                    record["estimated_token_upper_bound"] = len(json.dumps(body).encode()) + max_output_tokens
                    if ledger is not None:
                        try:
                            reservation_id = ledger.reserve(record["estimated_token_upper_bound"], request_timeout + 45)
                            record["reservation_id"] = reservation_id
                        except ValueError as exc:
                            if str(exc) == "run_model_concurrency_exhausted":
                                record["error"] = str(exc)
                                self.error(429, str(exc))
                                return
                            raise ProbeError(str(exc)) from None
                    with gateway.lock:
                        estimate = record["estimated_token_upper_bound"]
                        if gateway.charged_tokens + gateway.reserved_tokens + estimate > max_total_tokens:
                            raise ProbeError("total_token_budget_exhausted")
                        gateway.reserved_tokens += estimate
                        reserved = estimate
                    with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(request_timeout, connect=min(15, request_timeout)),
                                      transport=gateway.transport) as client:
                        with client.stream("POST", upstream.base_url + "/responses",
                                           headers={"Authorization": "Bearer " + upstream.token}, json=body) as res:
                            record["upstream_http_status"] = res.status_code
                            if res.status_code != 200:
                                self.error(res.status_code, f"upstream_http_{res.status_code}")
                                return
                            if body.get("stream"):
                                if "text/event-stream" not in res.headers.get("content-type", ""):
                                    raise ProbeError("upstream_not_sse")
                                self.send_response(200)
                                self.send_header("Content-Type", "text/event-stream")
                                self.send_header("Connection", "close")
                                self.end_headers()
                                headers_sent = True
                                self.close_connection = True
                                def watch_disconnect():
                                    while not stop_monitor.wait(0.05):
                                        if time.monotonic() - started >= request_timeout:
                                            record["error"] = "gateway_deadline"
                                            res.close()
                                            return
                                        try:
                                            readable, _, _ = select.select([self.connection], [], [], 0)
                                            if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                                                record["client_disconnected"] = True
                                                res.close()
                                                return
                                        except OSError:
                                            return
                                monitor = threading.Thread(target=watch_disconnect, daemon=True)
                                monitor.start()
                                record["events"] = []
                                for event, data in parse_sse(res.iter_lines()):
                                    if time.monotonic() - started > request_timeout:
                                        raise ProbeError("gateway_deadline")
                                    if data == "[DONE]":
                                        self.wfile.write(b"data: [DONE]\n\n")
                                    else:
                                        obj = mapper.response(json.loads(data))
                                        kind = obj.get("type", event)
                                        record["events"].append(kind)
                                        if kind in ("response.completed", "response.failed", "response.incomplete"):
                                            record["terminal"] = kind
                                        usage = obj.get("response", {}).get("usage")
                                        if usage:
                                            record["usage"] = numeric_usage(usage)
                                        prefix = f"event: {event}\n" if event else ""
                                        data_bytes = (prefix + "data: " + json.dumps(obj) + "\n\n").encode()
                                        if record["terminal"] != "not_observed":
                                            terminal_data = data_bytes
                                            # Leave the upstream context now; no
                                            # optional [DONE]/EOF is needed.
                                            break
                                        self.wfile.write(data_bytes)
                                    self.wfile.flush()
                                if record["terminal"] == "not_observed":
                                    raise ProbeError(record.get("error", "upstream_stream_incomplete"))
                            else:
                                obj = mapper.response(res.json())
                                terminal_data = json.dumps(obj).encode()
                                record["terminal"] = obj.get("status", "unknown")
                                if obj.get("usage"):
                                    record["usage"] = numeric_usage(obj["usage"])
                    # Upstream resources and durable accounting must finish
                    # before a client can start its next tool/model step.
                    finish_accounting()
                    if record.get('accounting_error'):
                        raise ProbeError('gateway_accounting_incomplete')
                    release_slot()
                    if not body.get('stream'):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(terminal_data)))
                        self.end_headers()
                        headers_sent = True
                    self.wfile.write(terminal_data)
                    self.wfile.flush()
                    record['terminal_delivered'] = True
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    record["client_disconnected"] = True
                except Exception as exc:
                    code = str(exc) if isinstance(exc, ProbeError) else type(exc).__name__
                    if not record.get("client_disconnected"):
                        record.setdefault("error", code)
                    if not headers_sent:
                        self.error(400 if code != 'gateway_accounting_incomplete' and isinstance(exc, (ProbeError, ValueError, KeyError)) else 502, code)
                    else:
                        # Close the stream without a success terminal; Codex must reject it.
                        self.close_connection = True
                finally:
                    finish_accounting()
                    release_slot()
                    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    record["finished"] = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def config(self):
        return Config(f"http://127.0.0.1:{self.server.server_port}/v1", self.token, self.upstream.model)

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        deadline = time.monotonic() + 5
        while any(not r.get("finished") for r in self.trace) and time.monotonic() < deadline:
            time.sleep(0.05)
