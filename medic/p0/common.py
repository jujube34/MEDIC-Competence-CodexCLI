from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVISION = "221e4e7c05386d06aa7b91da2a9b04f7f2bd921f"
SCHEMAS = ROOT / "codex-rs/app-server-protocol/schema/json"
CODES = tuple("M1 M2 M3 M4 E1 E2 E3 E4 D1 D2 D3 I1 I2 C1 C2 C3 C4".split())


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def result(name, status, **details):
    return {"name": name, "status": status, **details}
