"""Bounded synthetic probes. Never persist request headers or arbitrary response text."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jsonschema

from .common import digest, now, result, save

EVIDENCE = "synthetic-evidence-001"
PARAMETERS = {"type": "object", "properties": {"evidence_id": {"type": "string"}},
              "required": ["evidence_id"], "additionalProperties": False}
ANSWER_SCHEMA = {"type": "object", "properties": {"marker": {"type": "string"}},
                 "required": ["marker"], "additionalProperties": False}
TOOL = {"type": "function", "name": "echo_evidence_id",
        "description": "Read one synthetic evidence record and its receipt. No employee data.",
        "parameters": PARAMETERS, "strict": True}


class ProbeError(Exception):
    """Only fixed error codes; never include server bodies or credentials."""


@dataclass(repr=False)
class Config:
    base_url: str
    token: str = field(repr=False)
    model: str = "deepseek-v4-flash"

    def __post_init__(self):
        u = urlsplit(self.base_url)
        if u.username or u.password or u.query or u.fragment or not u.hostname:
            raise ValueError("invalid_provider_url")
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("tls_required")
        self.base_url = self.base_url.rstrip("/")
        if not self.token or re.search(r"[\r\n]", self.token):
            raise ValueError("invalid_token")

    @classmethod
    def load(cls, example=None):
        if example:
            text = Path(example).read_text(encoding="utf-8-sig")
            url = re.search(r"https?://[^\s\"'<>]+", text)
            key = re.search(r"Bearer\s+([^\s\"']+)", text)
            model = re.search(r"deepseek[\w.-]+", text)
            if not url or not key or not model or not url[0].endswith("/chat/completions"):
                raise ValueError("invalid_local_api_example")
            return cls(url[0].removesuffix("/chat/completions"), key[1], model[0])
        return cls(os.environ["MEDIC_PROVIDER_BASE_URL"], os.environ["MEDIC_PROVIDER_API_KEY"],
                   os.environ.get("MEDIC_PROVIDER_MODEL", "deepseek-v4-flash"))


def tool_result(name, arguments, receipt):
    if name != TOOL["name"]:
        return {"error": "unknown_tool"}
    try:
        args = json.loads(arguments)
        jsonschema.validate(args, PARAMETERS)
    except (ValueError, jsonschema.ValidationError, TypeError):
        return {"error": "invalid_arguments"}
    if args["evidence_id"] != EVIDENCE:
        return {"error": "unknown_evidence"}
    return {"evidence_id": EVIDENCE, "receipt": receipt}


def response_text(response):
    if response.get("status") != "completed" or response.get("error") or response.get("incomplete_details"):
        raise ProbeError("response_not_completed")
    return "".join(c.get("text", "") for item in response.get("output", [])
                   if item.get("type") == "message" for c in item.get("content", [])
                   if c.get("type") == "output_text")


def parse_sse(lines):
    """SSE framing, including multiline data; reject unterminated final frames."""
    event, data = None, []
    for line in lines:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        raise ProbeError("unterminated_sse_frame")


def numeric_usage(value):
    keys = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
            "cached_tokens", "cache_write_tokens", "input_tokens_details", "output_tokens_details", "prompt_tokens_details"}
    return {k: numeric_usage(v) if isinstance(v, dict) else v for k, v in value.items()
            if k in keys and (isinstance(v, dict) or type(v) is int and v >= 0)}


class Provider:
    def __init__(self, config, transport=None, max_requests=40):
        self.config = config
        self.client = httpx.Client(timeout=httpx.Timeout(45, connect=15), trust_env=False,
                                   follow_redirects=False, transport=transport)
        self.max_requests = max_requests
        self.trace = []
        self.usage = []

    def close(self):
        self.client.close()

    def call(self, route, body, stream=False):
        if len(self.trace) >= self.max_requests:
            raise ProbeError("request_budget_exhausted")
        record = {"route": route, "stream": stream, "request_sha256": digest(body)}
        self.trace.append(record)
        started = time.monotonic()
        try:
            with self.client.stream("POST", self.config.base_url + "/" + route,
                                    headers={"Authorization": "Bearer " + self.config.token},
                                    json={"model": self.config.model, **body}) as res:
                record["http_status"] = res.status_code
                if res.status_code != 200:
                    # No retries, including 401/403; a later caller must choose its bounded policy.
                    raise ProbeError(f"http_{res.status_code}")
                if stream:
                    if "text/event-stream" not in res.headers.get("content-type", ""):
                        raise ProbeError("not_sse_content_type")
                    events = []
                    byte_count = 0
                    for event, data in parse_sse(res.iter_lines()):
                        byte_count += len(data)
                        if byte_count > 2_000_000 or time.monotonic() - started > 60:
                            raise ProbeError("stream_budget_exhausted")
                        if data == "[DONE]":
                            events.append({"type": "done"})
                        else:
                            obj = json.loads(data)
                            if event and obj.get("type") and event != obj["type"]:
                                raise ProbeError("sse_event_type_mismatch")
                            events.append(obj)
                    record["event_types"] = [e.get("type", "chat.chunk") for e in events]
                    record["response_sha256"] = digest(events)
                    for e in events:
                        usage = e.get("usage") or e.get("response", {}).get("usage")
                        if usage:
                            record["usage"] = numeric_usage(usage)
                            self.usage.append(record["usage"])
                    return events
                raw = res.read()
                if len(raw) > 2_000_000:
                    raise ProbeError("response_budget_exhausted")
                obj = json.loads(raw)
                record["response_sha256"] = digest(raw)
                record["reported_model"] = obj.get("model") if obj.get("model") == self.config.model else "different_or_missing"
                if obj.get("usage"):
                    record["usage"] = numeric_usage(obj["usage"])
                    self.usage.append(record["usage"])
                return obj
        except (httpx.HTTPError, ValueError) as exc:
            raise ProbeError(type(exc).__name__) from None
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            record["usage_source"] = "reported" if record.get("usage") else "conservative_estimate"
            if not record.get("usage"):
                record["estimated_token_upper_bound"] = len(json.dumps(body).encode()) + 768

    def responses(self, inputs, **kwargs):
        return self.call("responses", {"input": inputs, "max_output_tokens": 768, **kwargs},
                         stream=kwargs.get("stream", False))

    def tool_loop(self, protocol="responses", count=1, error_first=False):
        receipt = "receipt-" + uuid.uuid4().hex
        instruction = (f"Synthetic protocol test. Call echo_evidence_id with evidence_id '{EVIDENCE}'. "
                       f"Make exactly {count} sequential successful calls. "
                       "If the tool returns an error, retry with the same valid evidence ID. "
                       "Then reply only with the receipt returned by the last tool result.")
        history = [{"role": "user", "content": instruction}]
        seen, successful, call_ids, injected = 0, 0, set(), False
        chat_tool = {"type": "function", "function": {k: v for k, v in TOOL.items() if k != "type"}}
        for _ in range(count + 4):
            if protocol == "responses":
                response = self.responses(history, tools=[TOOL], parallel_tool_calls=False)
                response_text(response)
                output = response.get("output", [])
                calls = [x for x in output if x.get("type") == "function_call"]
                final = response_text(response)
                history.extend(output)
            else:
                response = self.call("chat/completions", {"messages": history, "tools": [chat_tool],
                                     "parallel_tool_calls": False, "max_tokens": 768})
                choice = response["choices"][0]
                if choice["finish_reason"] not in ("stop", "tool_calls"):
                    raise ProbeError("chat_not_completed")
                message = choice["message"]
                history.append(message)  # preserve reasoning_content if the provider requires it
                calls = [{"name": x["function"]["name"], "arguments": x["function"]["arguments"],
                          "call_id": x["id"]} for x in (message.get("tool_calls") or [])]
                final = message.get("content") or ""
            if not calls:
                if successful != count or final.strip() != receipt or (error_first and not injected):
                    raise ProbeError("tool_roundtrip_not_proven")
                return {"successful_calls": successful, "total_calls": seen,
                        "error_recovered": injected, "receipt_verified": True}
            for call in calls:
                seen += 1
                if seen > count + 2 or not call.get("call_id") or call["call_id"] in call_ids:
                    raise ProbeError("invalid_call_id_or_loop_limit")
                call_ids.add(call["call_id"])
                value = tool_result(call["name"], call["arguments"], receipt)
                if error_first and not injected:
                    value, injected = {"error": "temporary_synthetic_tool_error"}, True
                if "error" not in value:
                    successful += 1
                if protocol == "responses":
                    history.append({"type": "function_call_output", "call_id": call["call_id"],
                                    "output": json.dumps(value)})
                else:
                    history.append({"role": "tool", "tool_call_id": call["call_id"], "content": json.dumps(value)})
        raise ProbeError("tool_loop_limit")

    def structured(self, protocol):
        marker = "json-" + uuid.uuid4().hex
        instruction = f'Return JSON with exactly one key marker and value "{marker}".'
        if protocol == "responses":
            obj = self.responses(instruction, text={"format": {"type": "json_schema", "name": "probe",
                                 "strict": True, "schema": ANSWER_SCHEMA}})
            text = response_text(obj)
        else:
            obj = self.call("chat/completions", {"messages": [{"role": "user", "content": instruction}],
                            "max_tokens": 128, "response_format": {"type": "json_schema", "json_schema": {
                                "name": "probe", "strict": True, "schema": ANSWER_SCHEMA}}})
            choice = obj["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ProbeError("chat_not_completed")
            text = choice["message"]["content"]
        value = json.loads(text)
        jsonschema.validate(value, ANSWER_SCHEMA)
        if value["marker"] != marker:
            raise ProbeError("structured_marker_mismatch")
        return {"schema_validated_locally": True, "native_schema_enforcement": "unproven"}

    def tool_choice(self):
        response = self.responses(f"Read {EVIDENCE}.", tools=[TOOL],
                                  tool_choice={"type": "function", "name": TOOL["name"]})
        response_text(response)
        calls = [x for x in response.get("output", []) if x.get("type") == "function_call"]
        if len(calls) != 1 or "error" in tool_result(calls[0]["name"], calls[0]["arguments"], "synthetic"):
            raise ProbeError("forced_tool_choice_not_observed")
        return {"named_tool_choice_observed": True}

    def namespace_tool(self):
        response = self.responses(f"Call echo_evidence_id with evidence_id {EVIDENCE}.", tools=[{
            "type": "namespace", "name": "mcp__synthetic", "description": "Synthetic tools", "tools": [TOOL]}])
        response_text(response)
        calls = [x for x in response.get("output", []) if x.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("namespace") != "mcp__synthetic" or calls[0].get("name") != TOOL["name"]:
            raise ProbeError("namespace_tool_not_called")
        return {"namespace_and_name_preserved": True}

    def sse(self, protocol):
        if protocol == "responses":
            events = self.responses("Reply only MEDIC_P0_OK.", stream=True)
            types = [e.get("type") for e in events]
            if "response.created" not in types or types.count("response.completed") != 1:
                raise ProbeError("missing_sse_lifecycle")
            if any(x in types for x in ("error", "response.failed", "response.incomplete")):
                raise ProbeError("failed_sse_terminal")
            terminal = next(e["response"] for e in events if e.get("type") == "response.completed")
            if response_text(terminal).strip() != "MEDIC_P0_OK":
                raise ProbeError("sse_text_mismatch")
            ids = [e["item"]["id"] for e in events if e.get("type") == "response.output_item.added"]
            if not ids or len(ids) != len(set(ids)):
                raise ProbeError("sse_item_ids_invalid")
            return {"events": types, "unique_item_ids": True}
        events = self.call("chat/completions", {"messages": [{"role": "user", "content": "Reply only MEDIC_P0_OK."}],
                           "stream": True, "stream_options": {"include_usage": True}, "max_tokens": 128}, stream=True)
        choices = [c for e in events for c in e.get("choices", [])]
        text = "".join(c.get("delta", {}).get("content") or "" for c in choices)
        finishes = [c.get("finish_reason") for c in choices if c.get("finish_reason")]
        if text.strip() != "MEDIC_P0_OK" or finishes != ["stop"] or events[-1].get("type") != "done":
            raise ProbeError("chat_sse_incomplete")
        return {"finish_reason": "stop", "done_received": True}


def suite(config, output):
    probe = Provider(config)
    checks = []
    def check(name, action):
        try:
            checks.append(result(name, "pass", **action()))
        except Exception as exc:
            checks.append(result(name, "fail", error=str(exc) if isinstance(exc, ProbeError) else type(exc).__name__))
    try:
        for protocol in ("responses", "chat"):
            check(protocol + ".tool_roundtrip", lambda p=protocol: probe.tool_loop(p))
            # Authentication errors stop the suite instead of repeatedly sending credentials.
            if checks[-1].get("error") in ("http_401", "http_403"):
                break
            check(protocol + ".sse", lambda p=protocol: probe.sse(p))
            check(protocol + ".json_schema", lambda p=protocol: probe.structured(p))
        if any(x["name"] == "responses.tool_roundtrip" and x["status"] == "pass" for x in checks):
            check("responses.two_tools", lambda: probe.tool_loop(count=2))
            check("responses.tool_error_recovery", lambda: probe.tool_loop(error_first=True))
            check("responses.tool_choice", probe.tool_choice)
            check("responses.namespace_tool", probe.namespace_tool)
            def context():
                marker = "context-" + uuid.uuid4().hex
                text = f"Remember {marker}.\n" + "Synthetic filler. " * 1000 + f"\nReply only the remembered marker."
                reply = response_text(probe.responses(text))
                if reply.strip() != marker:
                    raise ProbeError("context_marker_mismatch")
                return {"tested_input_chars": len(text), "maximum_context_window": "unknown"}
            check("responses.bounded_context", context)
        report = {"created_at": now(), "synthetic_only": True, "model": config.model,
                  "endpoint_sha256": digest(config.base_url), "checks": checks,
                  "usage": probe.usage, "usage_accounting": "reported" if len(probe.usage) == len(probe.trace) else "partial_or_missing",
                  "trace": probe.trace, "request_count": len(probe.trace),
                  "capabilities_not_claimed": ["vision", "maximum_context_window", "parallel_tool_calls",
                                                "native_schema_enforcement", "production_isolation"],
                  "route": "responses_direct_candidate" if all(any(c["name"] == n and c["status"] == "pass" for c in checks)
                           for n in ("responses.tool_roundtrip", "responses.sse", "responses.json_schema")) else "blocked_pending_gateway_or_endpoint",
                  "p0_accepted": False}
        if report["route"] == "responses_direct_candidate" and not any(
                c["name"] == "responses.namespace_tool" and c["status"] == "pass" for c in checks):
            report["route"] = "requires_namespace_gateway"
        report["status"] = "pass" if report["route"] == "responses_direct_candidate" else "fail"
        save(output, report)
        return report
    finally:
        probe.close()
