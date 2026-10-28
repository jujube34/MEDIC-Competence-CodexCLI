"""Fixed-schema app-server contract runner using a synthetic MCP tool."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import jsonschema

from .common import ROOT, SCHEMAS, digest, now, result, save
from .provider import ANSWER_SCHEMA, ProbeError

METHOD_SCHEMAS = {
    "initialize": ("v1/InitializeParams", "v1/InitializeResponse"),
    "thread/start": ("v2/ThreadStartParams", "v2/ThreadStartResponse"),
    "thread/resume": ("v2/ThreadResumeParams", "v2/ThreadResumeResponse"),
    "turn/start": ("v2/TurnStartParams", "v2/TurnStartResponse"),
    "turn/interrupt": ("v2/TurnInterruptParams", "v2/TurnInterruptResponse"),
    "mcpServerStatus/list": ("v2/ListMcpServerStatusParams", "v2/ListMcpServerStatusResponse"),
}


def validate(name, value):
    schema = json.loads((SCHEMAS / (name + ".json")).read_text(encoding="utf-8"))
    jsonschema.Draft7Validator(schema).validate(value)


def isolated_config(config, work):
    work = Path(work).resolve()
    home, cwd = work / "codex-home", work / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    audit = work / "mcp-audit.jsonl"
    quote = json.dumps
    text = f'''model = {quote(config.model)}
model_provider = "medic_p0"
model_reasoning_effort = "low"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
project_doc_max_bytes = 0

[model_providers.medic_p0]
name = "MEDIC P0 synthetic probe"
base_url = {quote(config.base_url)}
env_key = "MEDIC_P0_PROVIDER_TOKEN"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 240000

[analytics]
enabled = false
[feedback]
enabled = false
[features]
shell_tool = false
view_image = false
goals = false
default_mode_request_user_input = false
multi_agent = false
multi_agent_v2 = false
js_repl = false
code_mode = false
code_mode_only = false
apply_patch_freeform = false

[mcp_servers.synthetic]
command = {quote(sys.executable)}
args = [{quote(str(ROOT / "medic/p0/echo_mcp.py"))}]
startup_timeout_sec = 20
tool_timeout_sec = 20
enabled_tools = ["echo_evidence_id"]
[mcp_servers.synthetic.env]
MEDIC_SYNTHETIC_AUDIT = {quote(str(audit))}
'''
    (home / "config.toml").write_text(text, encoding="utf-8")
    # Do not inherit personal model credentials, proxies or Codex settings.
    allowed = {"systemroot", "windir", "comspec", "path", "pathext", "temp", "tmp", "localappdata", "appdata"}
    env = {k: v for k, v in os.environ.items() if k.lower() in allowed}
    env.update(CODEX_HOME=str(home), HOME=str(home), USERPROFILE=str(home),
               MEDIC_P0_PROVIDER_TOKEN=config.token, NO_PROXY="*", PYTHONUTF8="1")
    return cwd, env, audit


class Client:
    def __init__(self, binary, cwd, env):
        self.process = subprocess.Popen([str(Path(binary).resolve()), "app-server", "--listen", "stdio://"],
                                        cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.incoming = queue.Queue()
        self.responses, self.events, self.trace = {}, [], []
        self.next_id = 0
        self.active_turns = set()
        self.max_active_turns = 0
        self.stderr = []
        def reader():
            for line in self.process.stdout:
                try:
                    self.incoming.put(json.loads(line))
                except ValueError:
                    self.incoming.put({"invalid_json": True})
            self.incoming.put({"process_eof": True})
        def errors():
            for line in self.process.stderr:
                # Keep only hashes, never server URLs, tokens, prompts or filesystem paths.
                self.stderr.append(digest(line))
        self.read_thread = threading.Thread(target=reader, daemon=True)
        self.error_thread = threading.Thread(target=errors, daemon=True)
        self.read_thread.start()
        self.error_thread.start()

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def pump(self, deadline):
        try:
            value = self.incoming.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            raise ProbeError("appserver_timeout") from None
        if value.get("invalid_json") or value.get("process_eof"):
            raise ProbeError("appserver_invalid_json_or_eof")
        if "method" in value:
            # Preserve lifecycle and tool events; aggregate verbose text/reasoning deltas by count.
            if value["method"].endswith("Delta") or value["method"].endswith("/delta"):
                if self.trace and self.trace[-1].get("method") == value["method"]:
                    self.trace[-1]["count"] = self.trace[-1].get("count", 1) + 1
                else:
                    self.trace.append({"direction": "server", "method": value["method"], "count": 1})
            else:
                self.trace.append({"direction": "server", "method": value["method"]})
            if "id" in value:
                self.send({"id": value["id"], "error": {"code": -32601, "message": "Disabled in synthetic probe"}})
                raise ProbeError("unexpected_server_request")
            if value["method"] == "turn/completed":
                validate("v2/TurnCompletedNotification", value["params"])
                self.active_turns.discard(value["params"]["turn"]["id"])
            if value["method"] == "turn/started":
                self.active_turns.add(value["params"]["turn"]["id"])
                self.max_active_turns = max(self.max_active_turns, len(self.active_turns))
            self.events.append(value)
        else:
            self.responses[value.get("id")] = value

    def request(self, method, params, timeout=90):
        validate(METHOD_SCHEMAS[method][0], params)
        self.next_id += 1
        ident = self.next_id
        self.trace.append({"direction": "client", "method": method})
        self.send({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while ident not in self.responses:
            if time.monotonic() >= deadline:
                raise ProbeError("appserver_timeout")
            self.pump(deadline)
        response = self.responses.pop(ident)
        if "error" in response:
            raise ProbeError("rpc_error_" + str(response["error"].get("code")))
        validate(METHOD_SCHEMAS[method][1], response["result"])
        return response["result"]

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "medic_p0", "version": "0.1.0"},
                                    "capabilities": {"experimentalApi": False}})
        self.send({"method": "initialized", "params": {}})

    def start_thread(self, model):
        thread = self.request("thread/start", {"model": model, "modelProvider": "medic_p0", "approvalPolicy": "never",
                           "sandbox": "read-only", "baseInstructions": "You run synthetic protocol tests. Follow the user's exact output format. Use only the synthetic echo evidence MCP tool when requested. Do not access files or network."})["thread"]["id"]
        self.wait_mcp(thread)
        return thread

    def wait_mcp(self, thread):
        deadline = time.monotonic() + 30
        while True:
            statuses = [e["params"] for e in self.events if e["method"] == "mcpServer/startupStatus/updated"
                        and e["params"].get("threadId") == thread and e["params"].get("name") == "synthetic"]
            if statuses and statuses[-1]["status"] == "ready":
                return
            if statuses and statuses[-1]["status"] in ("failed", "cancelled"):
                raise ProbeError("synthetic_mcp_startup_failed")
            if time.monotonic() >= deadline:
                raise ProbeError("synthetic_mcp_startup_timeout")
            self.pump(deadline)

    def start_turn(self, thread, text, schema=None):
        params = {"threadId": thread, "input": [{"type": "text", "text": text}]}
        if schema:
            params["outputSchema"] = schema
        return self.request("turn/start", params)["turn"]["id"]

    def completed(self, thread, turn, status="completed", timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            matches = [e["params"]["turn"] for e in self.events if e["method"] == "turn/completed"
                       and e["params"]["threadId"] == thread and e["params"]["turn"]["id"] == turn]
            if matches:
                if matches[0]["status"] != status:
                    raise ProbeError("turn_status_" + matches[0]["status"])
                return matches[0]
            if time.monotonic() >= deadline:
                raise ProbeError("turn_timeout")
            self.pump(deadline)

    def wait_streaming(self, thread, turn, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            if any(e["method"] == "item/agentMessage/delta" and e["params"].get("threadId") == thread
                   and e["params"].get("turnId") == turn for e in self.events):
                return
            if time.monotonic() >= deadline:
                raise ProbeError("stream_start_timeout")
            self.pump(deadline)

    def final_text(self, thread, turn):
        items = [e["params"]["item"] for e in self.events if e["method"] == "item/completed"
                 and e["params"].get("threadId") == thread and e["params"].get("turnId") == turn
                 and e["params"]["item"]["type"] == "agentMessage"]
        return items[-1]["text"].strip() if items else ""

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self.read_thread.join(timeout=2)
        self.error_thread.join(timeout=2)


def suite(binary, config, output, threads=4):
    if threads not in (2, 3, 4):
        raise ValueError("threads_must_be_2_to_4")
    output = Path(output).resolve()
    work = output.parent / ("session-" + uuid.uuid4().hex)
    cwd, env, audit = isolated_config(config, work)
    checks, traces = [], []
    client = Client(binary, cwd, env)
    try:
        client.initialize()
        checks.append(result("initialize", "pass"))
        thread = client.start_thread(config.model)
        turn = client.start_turn(thread, "Call echo_evidence_id with evidence_id synthetic-evidence-001. Then reply only with its receipt value.")
        client.completed(thread, turn)
        answer = client.final_text(thread, turn)
        receipts = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        if not any(r["ok"] and r["receipt"] == answer for r in receipts):
            raise ProbeError("mcp_roundtrip_not_proven")
        checks.append(result("mcp_roundtrip", "pass", actual_tool_executed=True))
        marker = "saved-" + uuid.uuid4().hex
        turn = client.start_turn(thread, f'Remember marker {marker}. Return JSON {{"marker":"{marker}"}}.', ANSWER_SCHEMA)
        client.completed(thread, turn)
        value = json.loads(client.final_text(thread, turn))
        jsonschema.validate(value, ANSWER_SCHEMA)
        if value["marker"] != marker:
            raise ProbeError("structured_marker_mismatch")
        checks.append(result("output_schema", "pass"))
        client.close()
        traces.extend(client.trace)
        client = Client(binary, cwd, env)
        client.initialize()
        client.request("thread/resume", {"threadId": thread})
        turn = client.start_turn(thread, "Reply only with the saved marker from the preceding conversation.")
        client.completed(thread, turn)
        if client.final_text(thread, turn) != marker:
            raise ProbeError("resume_marker_mismatch")
        checks.append(result("resume_after_process_restart", "pass"))
        pending = []
        for _ in range(threads):
            ident = client.start_thread(config.model)
            secret = "isolated-" + uuid.uuid4().hex
            task = client.start_turn(ident, f'Reply as JSON with marker "{secret}".', ANSWER_SCHEMA)
            pending.append((ident, task, secret))
        for ident, task, secret in pending:
            client.completed(ident, task)
            value = json.loads(client.final_text(ident, task))
            jsonschema.validate(value, ANSWER_SCHEMA)
            if value["marker"] != secret:
                raise ProbeError("cross_thread_marker_mismatch")
        if client.max_active_turns < 2:
            raise ProbeError("concurrency_not_observed")
        checks.append(result("concurrent_threads", "pass", threads=threads, peak_active_turns=client.max_active_turns))
        ident = client.start_thread(config.model)
        task = client.start_turn(ident, "Count from 1 to 100000, one number per line.")
        # turn/start is only an acknowledgement; interrupt must target an active turn.
        client.wait_streaming(ident, task)
        client.request("turn/interrupt", {"threadId": ident, "turnId": task})
        client.completed(ident, task, status="interrupted")
        checks.append(result("interrupt", "pass"))
    except Exception as exc:
        checks.append(result("lifecycle", "fail", error=str(exc) if isinstance(exc, ProbeError) else type(exc).__name__))
    finally:
        client.close()
        traces.extend(client.trace)
    report = {"created_at": now(), "synthetic_only": True, "binary_sha256": digest(Path(binary).read_bytes()),
              "model": config.model, "endpoint_sha256": digest(config.base_url),
              "checks": checks, "trace": traces, "status": "pass" if len(checks) == 6 and all(c["status"] == "pass" for c in checks) else "fail",
              "production_isolation": "unproven_requires_container_acl_and_egress_tests"}
    save(output, report)
    return report
