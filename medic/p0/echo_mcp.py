"""Synthetic read-only MCP server, newline-delimited stdio. No business filesystem access."""
from __future__ import annotations

import json
import os
import sys
import uuid

# Direct script invocation from an isolated Codex working directory.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from medic.p0.provider import PARAMETERS, tool_result


def handle(message, audit):
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    if method == "initialize":
        response["result"] = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "medic-p0-synthetic", "version": "0.1.0"}}
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": [{"name": "echo_evidence_id", "description": "Return a receipt for synthetic-evidence-001.",
                                        "inputSchema": PARAMETERS,
                                        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                                        "openWorldHint": False}}]}
    elif method == "tools/call":
        receipt = "receipt-" + uuid.uuid4().hex
        value = tool_result(params.get("name"), json.dumps(params.get("arguments")), receipt)
        audit({"receipt": receipt, "ok": "error" not in value})
        response["result"] = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": "error" in value}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    return response


def main():
    def audit(value):
        path = os.environ.get("MEDIC_SYNTHETIC_AUDIT")
        if path:
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(value) + "\n")
    for line in sys.stdin:
        try:
            response = handle(json.loads(line), audit)
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
