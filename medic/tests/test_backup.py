"""Restore contracts using synthetic uploads; no model-quality claims."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from medic.apps.api.bootstrap import bootstrap
from medic.apps.api.contracts import CreateAssessment
from medic.apps.api.database import AppError
from medic.apps.api.service import Service
from medic.deploy import backup
from medic.runtime import build
from medic.runtime.budget import BudgetLedger
from medic.tests.test_ingestion import word


class BackupContracts(unittest.TestCase):
    def setUp(self):
        base = build.ROOT / 'artifacts/backup-tests'; base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; bootstrap(self.source)
        self.config = json.loads((self.source / 'local-access.json').read_text(encoding='utf-8'))
        self.service = Service(self.config['database'], self.config['vault'])
        self.employee = self.service.db.authenticate(self.config['tokens']['employee'])
        self.manager = self.service.db.authenticate(self.config['tokens']['manager'])
        self.doc = self.root / 'synthetic.docx'; self.doc.write_bytes(word('Synthetic backup evidence.'))
        self.material = self.service.ingest(self.employee, 'SYN-001', self.doc, 'Q4')
        self.assessment = self.service.create(self.employee, CreateAssessment(
            employee_ref='SYN-001', period='Q4', registry_id=self.config['registry_id'],
            material_ids=[self.material['snapshot_id']], competency_scope=['M2']), 'backup-create-01')
        self.target = self.root / 'backup'; self.recovered = self.root / 'recovered'

    def create(self):
        return backup.create(self.config['database'], self.config['vault'], self.target)

    def manifest(self):
        return json.loads((self.target / 'manifest.json').read_text(encoding='utf-8'))

    def replace_manifest(self, manifest):
        (self.target / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_restore_preserves_state_and_scope_but_revokes_old_sessions(self):
        original = self.service.runtime.snapshot(self.assessment['run_id'])
        before = self.service.detail(self.manager, self.assessment['assessment_id'])
        manifest = self.create()
        self.assertEqual(manifest['counts']['materials'], 1)
        result = backup.restore(self.target, self.recovered)
        self.assertEqual(result['copied_sessions_invalidated'], 3)
        service = Service(self.recovered / 'runtime.sqlite3', self.recovered / 'vault')
        for token in self.config['tokens'].values():
            with self.assertRaises(AppError): service.db.authenticate(token)
        token = service.db.provision_user(self.config['tenant'], 'manager', role='manager')
        manager = service.db.authenticate(token)
        self.assertEqual(service.detail(manager, self.assessment['assessment_id']), before)
        self.assertEqual(service.runtime.snapshot(self.assessment['run_id']), original)
        self.assertEqual(service.material(manager, self.material['snapshot_id']), self.service.material(self.manager, self.material['snapshot_id']))
        # Restoring never revokes sessions in the source application.
        self.assertEqual(self.service.db.authenticate(self.config['tokens']['employee']), self.employee)

    def test_committed_wal_data_is_backed_up(self):
        with closing(sqlite3.connect(self.config['database'])) as writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("UPDATE app_assessments SET revision=7 WHERE assessment_id=?", (self.assessment['assessment_id'],))
            writer.commit()
            self.assertTrue(Path(self.config['database']+'-wal').stat().st_size > 0)
            self.create()
        with closing(backup.read_db(self.target / 'runtime.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT revision FROM app_assessments').fetchone()[0], 7)

    def test_active_task_and_unsettled_budget_are_not_reset_or_executed(self):
        run = self.assessment['run_id']
        binding = self.service.runtime.claim_next(run)
        ledger = BudgetLedger(self.service.runtime, run); ledger.reserve(3456)
        snapshot = self.service.runtime.snapshot(run); budget = ledger.snapshot()
        self.create(); result = backup.restore(self.target, self.recovered)
        recovered = Service(self.recovered / 'runtime.sqlite3', self.recovered / 'vault')
        self.assertEqual(recovered.runtime.snapshot(run), snapshot)
        self.assertEqual(BudgetLedger(recovered.runtime, run).snapshot(), budget)
        self.assertEqual(snapshot['tasks'][0]['task_id'], binding['task_id'])
        self.assertEqual(budget['unsettled_requests'], 1)
        self.assertFalse(result['services_started'])

    def test_upload_between_database_snapshots_does_not_break_closure(self):
        real = backup.snapshot_db
        def capture(source, target):
            real(source, target)
            if Path(target).name == 'runtime.sqlite3':
                self.service.ingest(self.employee, 'SYN-001', self.doc, 'Q4')
        with patch.object(backup, 'snapshot_db', side_effect=capture):
            manifest = self.create()
        self.assertEqual(manifest['counts']['materials'], 1)
        self.assertEqual(manifest['counts']['snapshots'], 2)
        self.assertEqual(manifest['counts']['blobs'], 1)
        backup.restore(self.target, self.recovered)
        self.assertEqual(backup.inspect_data(self.recovered), manifest['counts'])

    def test_corrupt_blob_rejected_before_destination_exists(self):
        self.create()
        blob = next((self.target / 'vault').glob('*/blobs/*'))
        blob.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'member_integrity'):
            backup.restore(self.target, self.recovered)
        self.assertFalse(self.recovered.exists())

    def test_corrupt_database_rejected(self):
        self.create()
        (self.target / 'runtime.sqlite3').write_bytes(b'not a database')
        with self.assertRaisesRegex(ValueError, 'member_integrity'):
            backup.verify_backup(self.target)

    def test_broken_cross_store_reference_is_not_a_valid_backup(self):
        with self.service.db.transaction() as db:
            db.execute("UPDATE app_materials SET snapshot_id='snapshot-missing'")
        with self.assertRaisesRegex(ValueError, 'material_reference_broken'):
            self.create()
        self.assertFalse((self.target / 'manifest.json').exists())
        with self.assertRaises(FileNotFoundError): backup.restore(self.target, self.recovered)

    def test_existing_backup_or_restore_target_never_overwritten(self):
        self.create(); marker = self.manifest()
        with self.assertRaises(FileExistsError): self.create()
        self.assertEqual(self.manifest(), marker)
        self.recovered.mkdir(); (self.recovered / 'keep').write_text('keep')
        with self.assertRaises(FileExistsError): backup.restore(self.target, self.recovered)
        self.assertEqual((self.recovered / 'keep').read_text(), 'keep')

    def test_nested_destination_cannot_modify_the_backup_or_source_vault(self):
        nested = Path(self.config['vault']) / 'backup'
        with self.assertRaisesRegex(ValueError, 'overlaps_vault'):
            backup.create(self.config['database'], self.config['vault'], nested)
        self.assertFalse(nested.exists())
        self.create()
        with self.assertRaisesRegex(ValueError, 'overlaps_backup'):
            backup.restore(self.target, self.target / 'restored')
        self.assertFalse((self.target / 'restored').exists())
        backup.verify_backup(self.target)

    def test_member_traversal_and_unlisted_files_rejected(self):
        self.create(); manifest = self.manifest()
        manifest['files']['../outside'] = {'sha256': '0'*64, 'bytes': 0}
        self.replace_manifest(manifest)
        with self.assertRaisesRegex(ValueError, 'invalid_backup_member'):
            backup.restore(self.target, self.recovered)
        del manifest['files']['../outside']; self.replace_manifest(manifest)
        (self.target / 'unlisted').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'unexpected_backup_member'):
            backup.verify_backup(self.target)

    def test_hash_rewritten_does_not_hide_broken_material_scope(self):
        self.create()
        with closing(sqlite3.connect(self.target / 'runtime.sqlite3')) as db, db:
            db.execute("UPDATE app_materials SET employee_ref='SOMEONE-ELSE'")
        manifest = self.manifest()
        manifest['files']['runtime.sqlite3'] = backup.file_info(self.target / 'runtime.sqlite3')
        self.replace_manifest(manifest)
        with self.assertRaisesRegex(ValueError, 'material_reference_broken'):
            backup.restore(self.target, self.recovered)

    def test_different_build_restore_rejected(self):
        self.create(); manifest = self.manifest()
        manifest['captured_build']['manifest_sha256'] = '0'*64
        self.replace_manifest(manifest)
        with self.assertRaisesRegex(ValueError, 'captured_build'):
            backup.restore(self.target, self.recovered)
        self.assertFalse(self.recovered.exists())

    def test_postgres_is_explicitly_unsupported_not_sqlite_fallback(self):
        with self.assertRaisesRegex(ValueError, 'postgresql_backup_not_implemented'):
            backup.create('postgresql://local/placeholder_test', self.config['vault'], self.target)
        self.assertFalse(self.target.exists())
