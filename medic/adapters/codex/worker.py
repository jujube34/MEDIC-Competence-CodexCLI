from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from medic.runtime import build  # Capture source identity before loading execution modules.
from medic.p0.appserver import Client, isolated_config
from medic.p0.common import ROOT, digest, save
from medic.p0.provider import ProbeError
from medic.runtime.store import Store
from medic.prompts import load_bundles
from medic.tools.service import ToolService

def parse_receipt(text):
    text = text.strip()
    # The assessment itself is already schema-validated in the submission tool.
    # Allow a prose preamble followed by one separate receipt paragraph, but never
    # choose between multiple JSON objects or extract an embedded inline value.
    paragraphs = text.rsplit("\n\n", 1)
    if len(paragraphs) == 2 and not any(c in paragraphs[0] for c in "{}[]`"):
        text = paragraphs[1].strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        final = json.loads(text)
    except json.JSONDecodeError:
        raise ProbeError("final_receipt_missing_or_invalid_json") from None
    if not isinstance(final, dict) or set(final) != {"receipt"} or not isinstance(final["receipt"], str):
        raise ProbeError("invalid_final_receipt")
    return final["receipt"]


def wait_for_task(client, store, binding, thread, turn, timeout=240):
    deadline = time.monotonic() + timeout
    grace_used = False
    while True:
        # Poll the authoritative lease even during a silent or long model request.
        with store.read(binding['run_id']) as db:
            task, _ = store.authorize(db, binding)
        matches = [e["params"]["turn"] for e in client.events if e["method"] == "turn/completed"
                   and e["params"]["threadId"] == thread and e["params"]["turn"]["id"] == turn]
        if matches:
            if matches[0]["status"] != "completed":
                raise ProbeError("turn_status_" + matches[0]["status"])
            return
        now = time.monotonic()
        if now >= deadline:
            # A valid result is not completion. Wait only for the SAME turn's
            # verified final receipt, within the unchanged task lease/budget.
            submitted = False
            if not grace_used and task['status'] == 'submitted':
                with store.read(binding['run_id']) as db:
                    task, _ = store.authorize(db, binding)
                    result = db.execute('SELECT result_hash FROM results WHERE task_id=? AND lease_epoch=?',
                                        (binding['task_id'], binding['lease_epoch'])).fetchone()
                    submitted = task['status'] == 'submitted' and result and result['result_hash'] == task['result_hash']
            if submitted:
                remaining = task['lease_until'] - time.time()
                deadline = min(deadline + 30, now + remaining)
                grace_used = True
                if deadline <= now:
                    raise ProbeError('turn_timeout')
            else:
                raise ProbeError("turn_timeout")
        try:
            client.pump(min(deadline, time.monotonic() + 0.5))
        except ProbeError as exc:
            if str(exc) != "appserver_timeout":
                raise


class CodexWorker:
    def __init__(self, binary, provider, store: Store, work_root):
        self.binary, self.provider, self.store = str(Path(binary).resolve()), provider, store
        self.work_root = Path(work_root).resolve()
        self.prompt_bundles = load_bundles()
        from medic.p0.binaries import verify_binary
        verify_binary(self.binary)

    def run(self, task_id, binding=None):
        binding = binding or self.store.lease(task_id)
        with self.store.read(binding['run_id']) as db:
            self.store.authorize(db, binding)
        if binding["task_id"] != task_id:
            raise ValueError("worker_binding_task_mismatch")
        context = binding["context"]
        folder = self.work_root / task_id / str(binding["lease_epoch"])
        cwd, env, _ = isolated_config(self.provider, folder)
        path = folder / "binding.json"
        # Binding never enters model context; only this task's MCP process reads it.
        save(path, {k: v for k, v in binding.items() if k != "context"} | {"database": self.store.path})
        config_path = folder / "codex-home/config.toml"
        config = config_path.read_text(encoding="utf-8").split("[mcp_servers.synthetic]")[0]
        tools = ToolService(self.store, binding).definitions()
        config += f'''[mcp_servers.medic]
command = {json.dumps(sys.executable)}
args = [{json.dumps(str(ROOT / "medic/tools/server.py"))}]
startup_timeout_sec = 25
tool_timeout_sec = 25
enabled_tools = {json.dumps([t["name"] for t in tools])}
[mcp_servers.medic.env]
MEDIC_TASK_BINDING = {json.dumps(str(path))}
PYTHONUTF8 = "1"
'''
        config_path.write_text(config, encoding="utf-8")
        client = Client(self.binary, cwd, env)
        started = time.monotonic()
        thread, turn = None, None
        try:
            bundle = self.prompt_bundles.get(context["versions"]["prompt_bundle_version"])
            if bundle is None:
                raise ProbeError("unknown_frozen_prompt_bundle")
            client.initialize()
            prompt = bundle["common"] + "\n\nROLE:\n" + bundle[context["role"]]
            thread = client.request("thread/start", {"model": self.provider.model, "modelProvider": "medic_p0",
                "approvalPolicy": "never", "sandbox": "read-only", "baseInstructions": prompt})["thread"]["id"]
            deadline = time.monotonic() + 30
            while True:
                statuses = [e["params"] for e in client.events if e["method"] == "mcpServer/startupStatus/updated"
                            and e["params"].get("threadId") == thread and e["params"].get("name") == "medic"]
                if statuses and statuses[-1]["status"] == "ready":
                    break
                if statuses and statuses[-1]["status"] in ("failed", "cancelled"):
                    raise ProbeError("scoped_mcp_startup_failed")
                if time.monotonic() > deadline:
                    raise ProbeError("scoped_mcp_startup_timeout")
                client.pump(deadline)
            # This provider's combined tools + outputSchema path produced a fabricated
            # final receipt without tool execution. The typed submission tool is the
            # structured-output boundary; locally validate the final receipt instead.
            turn = client.start_turn(thread, "Execute your bound task. Get the manifest and use your authorized tools. Submit the result and return its receipt.")
            self.store.bind_execution(binding, thread, turn)
            while True:
                with self.store.read(binding['run_id']) as db:
                    leased, _ = self.store.authorize(db, binding)
                    remaining = max(0, leased['lease_until'] - time.time())
                wait_for_task(client, self.store, binding, thread, turn, timeout=min(420, remaining))
                try:
                    receipt = parse_receipt(client.final_text(thread, turn))
                    self.store.complete(binding, receipt, thread, turn)
                    break
                except (ProbeError, ValueError) as exc:
                    if str(exc) not in ('final_receipt_missing_or_invalid_json', 'invalid_final_receipt',
                                        'result_receipt_or_terminal_not_verified'):
                        raise
                    self.store.request_finalization(binding, thread, turn)
                    previous_turn = turn
                    turn = client.start_turn(thread,
                        f'The previous final answer failed receipt verification: {exc}. '
                        'Do not repeat an unverified receipt from your previous answer. '
                        'Call assessment_get_submission_receipt to read the exact persisted receipt for the SAME bound task. '
                        'If status is submitted, copy its receipt verbatim and return only {"receipt":"<exact returned receipt>"}. '
                        'If status is not_submitted, finish the bound task using authorized tools, including any required dispute reply and structured submission, then return its exact receipt JSON. '
                        'Do not merely announce a future action. Preserve all evidence, policy, and unresolved issues.')
                    self.store.bind_finalization(binding, thread, previous_turn, turn)
            return {"task_id": task_id, "role": context["role"], "status": "completed", "elapsed_seconds": round(time.monotonic() - started, 3)}
        except Exception as exc:
            if thread and turn:
                try:
                    client.request("turn/interrupt", {"threadId": thread, "turnId": turn}, timeout=10)
                except Exception:
                    pass
            from medic.runtime.store import Denied
            error = str(exc) if isinstance(exc, (ProbeError, Denied)) else type(exc).__name__
            self.store.fail(binding, error)
            return {"task_id": task_id, "role": context["role"], "status": "failed", "error": error}
        finally:
            client.close()
            save(folder / "protocol-summary.json", {"role": context["role"], "context_hash": context["context_hash"],
                                                    "prompt_bundle_hash": digest(bundle) if bundle else None,
                                                    'completion_protocol': 'bounded-finalization-v1',
                                                    'execution_build': context['execution_build'],
                                                    "events": client.trace})
