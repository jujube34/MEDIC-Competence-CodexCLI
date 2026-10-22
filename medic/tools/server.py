"""Bound MCP stdio worker; the launcher supplies a private binding path."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import jsonschema
from pydantic import ValidationError

from medic.runtime.store import Denied, Store
from medic.tools.service import ToolService


def main():
    # MCP stdio is UTF-8 regardless of the Windows console/locale inherited by Codex.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    binding = json.loads(Path(os.environ["MEDIC_TASK_BINDING"]).read_text(encoding="utf-8"))
    service = ToolService(Store(binding.pop("database")), binding)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            method = request.get("method")
            if method == "initialize":
                value = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                         "serverInfo": {"name": "medic-scoped-tools", "version": "0.1.0"}}
            elif method == "ping":
                value = {}
            elif method == "tools/list":
                value = {"tools": service.definitions()}
            elif method == "tools/call":
                params = request.get("params") or {}
                try:
                    output = service.call(params.get("name"), params.get("arguments") or {})
                    value = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}], "isError": False}
                except (Denied, ValueError, jsonschema.ValidationError) as exc:
                    if isinstance(exc, ValidationError):
                        detail = [{"field": ".".join(str(x) for x in e["loc"]), "error": e["type"]} for e in exc.errors()[:8]]
                    elif isinstance(exc, jsonschema.ValidationError):
                        detail = {"field": ".".join(str(x) for x in exc.absolute_path), "error": exc.validator}
                    else:
                        detail = str(exc)
                    value = {"content": [{"type": "text", "text": json.dumps({"error": detail})}], "isError": True}
            else:
                response["error"] = {"code": -32601, "message": "Method not found"}
                print(json.dumps(response), flush=True)
                continue
            response["result"] = value
        except (ValueError, TypeError, KeyError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid request"}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
