"""Run-scoped reservations survive gateway/dispatcher crashes and restarts."""
from __future__ import annotations

import json
import secrets
import time

from pydantic import Field

from medic.domain.contracts import Contract


class BudgetExceeded(ValueError):
    pass


class BudgetLimits(Contract):
    requests: int = Field(default=80, ge=1, le=10000)
    total_tokens: int = Field(default=500000, ge=1, le=100000000)
    concurrency: int = Field(default=2, ge=1, le=4)
    wall_seconds: int = Field(default=1800, ge=1, le=86400)


class BudgetLedger:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def reserve(self, estimate, active_seconds=195):
        if type(estimate) is not int or estimate <= 0:
            raise ValueError("invalid_token_estimate")
        with self.store.connect(self.run_id) as db:
            self.store.verify_build(db, self.run_id)
            run = db.execute("SELECT state FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            if not run or run["state"] in ("failed", "cancelled", "ready_for_human_review", "awaiting_human_review"):
                raise BudgetExceeded("run_not_active")
            row = db.execute("SELECT * FROM run_limits WHERE run_id=?", (self.run_id,)).fetchone()
            limits = BudgetLimits.model_validate_json(row["limits_json"])
            now = time.time()
            if now >= row["deadline"]:
                raise BudgetExceeded("run_wall_clock_budget_exhausted")
            usage = db.execute("SELECT count(*) AS requests, COALESCE(sum(COALESCE(charged,estimate)),0) AS tokens "
                               "FROM model_requests WHERE run_id=?", (self.run_id,)).fetchone()
            active = db.execute("SELECT count(*) FROM model_requests WHERE run_id=? AND charged IS NULL AND active_until>?",
                                (self.run_id, now)).fetchone()[0]
            if usage["requests"] >= limits.requests:
                raise BudgetExceeded("run_request_budget_exhausted")
            if usage["tokens"] + estimate > limits.total_tokens:
                raise BudgetExceeded("run_token_budget_exhausted")
            if active >= limits.concurrency:
                raise BudgetExceeded("run_model_concurrency_exhausted")
            ident = "request-" + secrets.token_hex(16)
            db.execute("INSERT INTO model_requests VALUES(?,?,?,?,?,?,NULL,NULL)",
                       (ident, self.run_id, estimate, now, min(now + active_seconds, row["deadline"]), "reserved"))
            return ident

    def settle(self, ident, reported_total=None):
        if reported_total is not None and (type(reported_total) is not int or reported_total < 0):
            raise ValueError("invalid_reported_usage")
        with self.store.connect(self.run_id) as db:
            row = db.execute("SELECT * FROM model_requests WHERE request_id=? AND run_id=?", (ident, self.run_id)).fetchone()
            if not row:
                raise ValueError("unknown_budget_reservation")
            charged = row["estimate"] if reported_total is None else reported_total
            if row["charged"] is not None:
                if row["charged"] != charged:
                    raise ValueError("budget_settlement_conflict")
                return
            db.execute("UPDATE model_requests SET charged=?,status=?,settled=? WHERE request_id=?",
                       (charged, "estimated" if reported_total is None else "reported", time.time(), ident))

    def snapshot(self):
        with self.store.read(self.run_id) as db:
            limits = db.execute("SELECT * FROM run_limits WHERE run_id=?", (self.run_id,)).fetchone()
            rows = [dict(r) for r in db.execute("SELECT * FROM model_requests WHERE run_id=? ORDER BY started,request_id", (self.run_id,))]
            return {"limits": json.loads(limits["limits_json"]), "deadline": limits["deadline"],
                    "requests": len(rows), "charged_or_reserved_tokens": sum(r["estimate"] if r["charged"] is None else r["charged"] for r in rows),
                    "unsettled_requests": sum(r["charged"] is None for r in rows), "ledger": rows}
