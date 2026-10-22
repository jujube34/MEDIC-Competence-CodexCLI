import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from medic.runtime import build
from medic.runtime.budget import BudgetLedger
from medic.runtime.store import Denied, Store
from medic.tests.test_runtime import fixture
from medic.tools.service import ToolService


class ReleaseFiles(unittest.TestCase):
    def test_changed_added_and_removed_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'runtime').mkdir()
            code = root / 'runtime/example.py'
            code.write_text('original = True\n', encoding='utf-8')
            files = build.inventory(root)
            manifest = root / 'runtime/build-manifest.json'
            manifest.write_text(json.dumps({'runtime_version': build.VERSION, 'files': files}), encoding='utf-8')
            args = {'root': root, 'manifest_path': manifest, 'boot_files': files}
            identity = build.verify(build.VERSION, **args)
            self.assertEqual(identity['runtime_version'], build.VERSION)
            code.write_text('changed = True\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'files_changed'):
                build.verify(build.VERSION, **args)
            code.write_text('original = True\n', encoding='utf-8')
            added = root / 'runtime/extra.py'
            added.write_text('# unexpected execution module', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'files_changed'):
                build.verify(build.VERSION, **args)
            added.unlink()
            code.unlink()
            with self.assertRaisesRegex(ValueError, 'files_changed'):
                build.verify(build.VERSION, **args)

    def test_replacing_manifest_cannot_refresh_an_old_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'runtime').mkdir()
            code = root / 'runtime/example.py'
            code.write_text('old = True', encoding='utf-8')
            old = build.inventory(root)
            code.write_text('new = True', encoding='utf-8')
            manifest = root / 'runtime/build-manifest.json'
            manifest.write_text(json.dumps({'runtime_version': build.VERSION, 'files': build.inventory(root)}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'requires_restart'):
                build.verify(build.VERSION, root=root, manifest_path=manifest, boot_files=old)


class BuildBindings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'runtime.sqlite3')
        self.run = self.store.create_run(fixture())
        self.ledger = BudgetLedger(self.store, self.run)

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_preserves_identity_and_budget_without_rebinding(self):
        self.ledger.reserve(1000)
        original = self.store.snapshot(self.run)['execution_build']
        script = ('import sys,json; from medic.runtime.store import Store; s=Store(sys.argv[1]); '
                  'b=s.claim_next(sys.argv[2]); print(json.dumps(b["context"]["execution_build"]))')
        result = subprocess.run([sys.executable, '-c', script, self.store.path, self.run],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), original)
        self.assertEqual(self.ledger.snapshot()['charged_or_reserved_tokens'], 1000)

    def test_mismatched_deployment_blocks_work_tools_spending_recovery_and_publish(self):
        binding = self.store.claim_next(self.run)
        service = ToolService(self.store, binding)
        before = self.store.snapshot(self.run)
        original = before['execution_build']
        for field in ('manifest_sha256', 'environment'):
            changed = copy.deepcopy(original)
            changed[field] = 'different-build-or-environment'
            with self.subTest(field=field), patch('medic.runtime.build.verify', return_value=changed):
                for action in (lambda: service.call('assessment_get_manifest', {}),
                               lambda: self.ledger.reserve(100), lambda: self.store.recover(self.run),
                               lambda: self.store.publish(self.run, {})):
                    with self.assertRaisesRegex(Denied, 'execution_build_mismatch'):
                        action()
                other = Store(self.store.path)
                with other.connect() as db, self.assertRaisesRegex(Denied, 'execution_build_mismatch'):
                    other.verify_build(db, self.run)
        self.assertEqual(self.store.snapshot(self.run), before)
        self.assertEqual(self.ledger.snapshot()['requests'], 0)

    def test_legacy_runs_remain_readable_and_cancellable_but_cannot_be_rebound(self):
        with self.store.connect(self.run) as db:
            db.execute('DELETE FROM run_builds WHERE run_id=?', (self.run,))
        self.assertIsNone(Store(self.store.path).snapshot(self.run)['execution_build'])
        for action in (lambda: self.store.claim_next(self.run), lambda: self.store.recover(self.run)):
            with self.assertRaisesRegex(Denied, 'legacy_run_has_no_frozen_build'):
                action()
        self.store.cancel(self.run)
        self.assertEqual(self.store.snapshot(self.run)['state'], 'cancelled')

    def test_publication_uses_database_build_not_caller_metadata(self):
        from medic.tests.test_runtime import RuntimeContracts
        harness = RuntimeContracts()
        harness.store, harness.source, harness.run = self.store, fixture(), self.run
        harness.coordinator = self.store.claim_next(self.run)
        snapshot = harness.full_graph()
        self.store.publish(self.run, {'gate': harness.gate(snapshot), 'execution_build': 'forged'})
        saved = self.store.snapshot(self.run)
        self.assertEqual(saved['report']['execution_build'], saved['execution_build'])


if __name__ == '__main__':
    unittest.main()
