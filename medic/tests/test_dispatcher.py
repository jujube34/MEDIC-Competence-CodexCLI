import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from medic.runtime.runner import execute
from medic.runtime.store import REQUIRED, Store
from medic.tests.test_runtime import fixture


class DurableDispatcher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "runtime.sqlite3")
        self.store = Store(self.path)
        self.run = self.store.create_run(fixture())

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        binding = self.store.claim_next(self.run)
        ids = {}
        for role, deps in REQUIRED.items():
            ids[role] = self.store.dispatch(binding, {"role": role, "material_id": "synthetic-material-1", "competency_id": "M2",
                                                    "dependencies": [ids[d] for d in deps], "reason": "Synthetic dispatcher fault test"})["task_id"]
        # No child can start before the real coordinator protocol would complete.
        self.assertIsNone(self.store.claim_next(self.run))
        receipt = self.store.submit(binding, {})["receipt"]
        self.store.complete(binding, receipt, "test-thread", "test-turn")
        return ids

    def test_separate_processes_consume_outbox_once_and_recover_dead_workers(self):
        ids = self.plan()
        script = "from medic.runtime.store import Store; import sys,json; b=Store(sys.argv[1]).claim_next(sys.argv[2]); " \
                 "print(json.dumps({'task_id':b['task_id'],'epoch':b['lease_epoch']} if b else None))"
        def claim(_):
            p = subprocess.run([sys.executable, "-c", script, self.path, self.run], capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = [r for r in pool.map(claim, range(4)) if r is not None]
        self.assertEqual({r["task_id"] for r in claimed}, {ids["evaluator"], ids["challenger_scan"]})
        self.assertEqual(len(claimed), 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND status='running'", (self.run,)).fetchone()[0], 2)
            db.execute("UPDATE tasks SET lease_until=0 WHERE run_id=? AND status='running'", (self.run,))
        restarted = Store(self.path)
        self.assertEqual(len(restarted.recover(self.run)), 2)
        new = restarted.claim_next(self.run)
        self.assertEqual(new["lease_epoch"], 2)
        self.assertIn(new["task_id"], {x["task_id"] for x in claimed})
        self.assertEqual(len(restarted.snapshot(self.run)["tasks"]), 6)

    def test_worker_start_failure_is_persisted_and_does_not_publish(self):
        class BrokenWorker:
            def run(self, task_id, binding=None):
                raise RuntimeError("synthetic start failure")
        result = execute(self.store, self.run, BrokenWorker())
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["report"])
        self.assertEqual(result["tasks"][0]["error"], "RuntimeError")

    def test_failure_callback_requires_matching_run_token_and_epoch(self):
        binding = self.store.claim_next(self.run)
        other_run = self.store.create_run(fixture())
        other = self.store.claim_next(other_run)
        for forged in (binding | {'token': 'forged'}, binding | {'lease_epoch': 2},
                       binding | {'task_id': other['task_id'], 'token': other['token']}):
            self.store.fail(forged, 'forged_failure')
        self.assertEqual(self.store.snapshot(self.run)['tasks'][0]['status'], 'running')
        self.assertEqual(self.store.snapshot(other_run)['tasks'][0]['status'], 'running')
        self.store.fail(binding, 'actual_failure')
        self.assertEqual(self.store.snapshot(self.run)['state'], 'failed')
        self.assertEqual(self.store.snapshot(other_run)['state'], 'planning')

    def test_expired_run_never_launches_a_worker(self):
        with self.store.connect() as db:
            db.execute("UPDATE run_limits SET deadline=0 WHERE run_id=?", (self.run,))
        class NeverWorker:
            def run(self, *args, **kwargs):
                raise AssertionError("must not launch")
        result = execute(self.store, self.run, NeverWorker())
        self.assertEqual(result["state"], "failed")
        self.assertIn("run_deadline_exhausted", [e["kind"] for e in result["events"]])

    def test_plan_only_does_not_start_child_even_if_coordinator_finishes_immediately(self):
        store, calls = self.store, []
        class CoordinatorOnly:
            def run(self, task_id, binding=None):
                calls.append(binding["context"]["role"])
                ids = {}
                for role, deps in REQUIRED.items():
                    ids[role] = store.dispatch(binding, {"role": role, "material_id": "synthetic-material-1", "competency_id": "M2",
                                                       "dependencies": [ids[d] for d in deps], "reason": "Synthetic plan-only test"})["task_id"]
                receipt = store.submit(binding, {})["receipt"]
                store.complete(binding, receipt, "test-thread", "test-turn")
                return {"status": "completed"}
        result = execute(store, self.run, CoordinatorOnly(), stop_after_plan=True)
        self.assertEqual(calls, ["coordinator"])
        self.assertIsNone(result["report"])
        self.assertTrue(all(t["status"] == "queued" for t in result["tasks"] if t["role"] != "coordinator"))

    def test_plan_only_atomic_claim_rejects_children_after_stale_snapshot(self):
        self.plan()
        actual_snapshot = self.store.snapshot
        stale = actual_snapshot(self.run)
        next(t for t in stale['tasks'] if t['role'] == 'coordinator')['status'] = 'running'
        calls = []
        class NoChildren:
            def run(self, task_id, binding=None):
                calls.append(task_id)
                raise AssertionError('plan-only must not launch a child')
        snapshots = [stale]
        def observe(run):
            return snapshots.pop() if snapshots else actual_snapshot(run)
        with patch.object(self.store, 'snapshot', side_effect=observe):
            result = execute(self.store, self.run, NoChildren(), stop_after_plan=True)
        self.assertEqual(calls, [])
        self.assertTrue(all(t['status'] == 'queued' and t['lease_epoch'] == 0 for t in result['tasks'] if t['role'] != 'coordinator'))
