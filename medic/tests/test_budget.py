import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from medic.p0.namespace_gateway import Gateway
from medic.p0.provider import Config
from medic.runtime.budget import BudgetExceeded, BudgetLedger
from medic.runtime.store import Denied, Store
from medic.tests.test_runtime import fixture


class PersistentBudget(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "runtime.sqlite3")
        self.store = Store(self.path)
        self.run = self.store.create_run(fixture(), limits={"requests": 3, "total_tokens": 10000, "concurrency": 2})
        self.ledger = BudgetLedger(self.store, self.run)

    def tearDown(self):
        self.temp.cleanup()

    def test_gateway_restart_does_not_reset_requests_or_usage(self):
        seen = []
        def provider(req):
            seen.append(req)
            return httpx.Response(200, json={"status": "completed", "usage": {"total_tokens": 17}})
        for expected in (200, 200, 200, 400):
            ledger = BudgetLedger(Store(self.path), self.run)
            with Gateway(Config("https://example.invalid", "x"), httpx.MockTransport(provider), ledger=ledger) as gateway:
                with httpx.Client(trust_env=False) as client:
                    result = client.post(gateway.config.base_url + "/responses", headers={"Authorization": "Bearer " + gateway.token},
                                         json={"model": "deepseek-v4-flash", "input": "synthetic", "tools": []})
                    self.assertEqual(result.status_code, expected)
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.ledger.snapshot()["charged_or_reserved_tokens"], 51)

    def test_process_death_keeps_reservation_and_idempotent_settlement(self):
        script = "from medic.runtime.store import Store; from medic.runtime.budget import BudgetLedger; import sys,os; " \
                 "BudgetLedger(Store(sys.argv[1]),sys.argv[2]).reserve(7000); os._exit(23)"
        result = subprocess.run([sys.executable, "-c", script, self.path, self.run], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23)
        usage = self.ledger.snapshot()
        self.assertEqual(usage["unsettled_requests"], 1)
        self.assertEqual(usage["charged_or_reserved_tokens"], 7000)
        with self.assertRaisesRegex(BudgetExceeded, "token_budget"):
            BudgetLedger(Store(self.path), self.run).reserve(4000)
        ident = usage["ledger"][0]["request_id"]
        self.ledger.settle(ident, None)
        self.ledger.settle(ident, None)
        with self.assertRaisesRegex(ValueError, "settlement_conflict"):
            self.ledger.settle(ident, 0)

    def test_concurrent_gateways_reserve_atomically(self):
        def attempt(_):
            try:
                return BudgetLedger(Store(self.path), self.run).reserve(6000)
            except BudgetExceeded:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.ledger.snapshot()["requests"], 1)

    def test_deadline_and_cancellation_stop_new_model_work(self):
        task = self.store.snapshot(self.run)["tasks"][0]["task_id"]
        binding = self.store.lease(task)
        with self.store.connect() as db:
            db.execute("UPDATE run_limits SET deadline=0 WHERE run_id=?", (self.run,))
        with self.assertRaisesRegex(BudgetExceeded, "wall_clock"):
            self.ledger.reserve(1)
        with self.store.connect() as db:
            with self.assertRaisesRegex(Denied, "wall_clock"):
                self.store.authorize(db, binding)
        self.store.cancel(self.run)
        with self.assertRaisesRegex(BudgetExceeded, "run_not_active"):
            self.ledger.reserve(1)

    def test_database_failure_prevents_model_request(self):
        import sqlite3
        seen = []
        def provider(req):
            seen.append(req)
            return httpx.Response(200, json={"status": "completed"})
        with Gateway(Config("https://example.invalid", "x"), httpx.MockTransport(provider), ledger=self.ledger) as gateway:
            with patch.object(self.store, "connect", side_effect=sqlite3.OperationalError("synthetic unavailable database")):
                with httpx.Client(trust_env=False) as client:
                    result = client.post(gateway.config.base_url + "/responses", headers={"Authorization": "Bearer " + gateway.token},
                                         json={"model": "deepseek-v4-flash", "input": "synthetic", "tools": []})
                    self.assertEqual(result.status_code, 502)
        self.assertEqual(seen, [])
