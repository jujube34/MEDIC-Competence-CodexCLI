"""Private synthetic forensic-archive tests; no live model execution claims."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medic.deploy import terminal_archive as archive
from medic.runtime import build
from medic.runtime.store import Store
from medic.runtime.budget import BudgetLedger
from medic.tests.test_runtime import fixture


class TerminalArchiveContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='terminal-archive-test-',dir=build.ROOT/'artifacts')
        self.base=Path(self.temp.name);self.source=self.base/'source';self.source.mkdir()
        self.backup=self.base/'backup';self.restored=self.base/'restored'
        self.store=Store(self.source/'runtime.sqlite3');self.run=self.store.create_run(fixture())
        self.binding=self.store.claim_next(self.run)
        self.store.bind_execution(self.binding,'fixture-thread','fixture-turn')
        self.ledger=BudgetLedger(self.store,self.run);self.ledger.reserve(123)
        self.folder=self.source/'workers'/self.binding['task_id']/'1';self.folder.mkdir(parents=True)
        self.write_worker(self.binding,'fixture-thread')
        (self.folder/'binding.json').write_text('{"token":"secret-sentinel-not-for-backup"}')
        (self.folder/'codex-home/config.toml').write_text('secret = "not-for-backup"')
        self.store.cancel(self.run);self.export()
        self.release_manifest=build.ROOT/'deploy/releases'/f'{build.VERSION}.manifest.json'
        self.release_archive=build.ROOT/'artifacts/releases'/build.VERSION/'source.zip'

    def write_worker(self,binding,thread):
        folder=self.source/'workers'/binding['task_id']/str(binding['lease_epoch']);folder.mkdir(parents=True,exist_ok=True)
        archive.write_new(folder/'protocol-summary.json',{'role':binding['context']['role'],
            'context_hash':binding['context']['context_hash'],'execution_build':binding['context']['execution_build'],
            'events':[],'fixture_only':True})
        sessions=folder/'codex-home/sessions/2026/10/23';sessions.mkdir(parents=True)
        (sessions/'fixture.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'id':thread},'fixture_only':True})+'\n')

    def export(self):
        (self.source/'run.json').write_text(json.dumps(self.store.snapshot(self.run),ensure_ascii=False),encoding='utf-8')
        (self.source/'gateway-summary.json').write_text(json.dumps({'durable_budget':self.ledger.snapshot(),'requests':[]}),encoding='utf-8')

    def create(self):
        return archive.create(self.source,self.release_manifest,self.release_archive,self.backup)

    def tearDown(self):
        self.assertTrue(self.base.resolve().is_relative_to((build.ROOT/'artifacts').resolve()))
        self.assertTrue(self.base.name.startswith('terminal-archive-test-'))
        self.temp.cleanup()

    def test_roundtrip_preserves_all_tables_unknown_usage_and_raw_protocol(self):
        before=archive.table_snapshot(self.source/'runtime.sqlite3')
        result=self.create();restored=archive.restore(self.backup,self.restored)
        self.assertEqual(restored,result)
        self.assertEqual(archive.table_snapshot(self.restored/'runtime.sqlite3'),before)
        self.assertEqual(archive.table_snapshot(self.source/'runtime.sqlite3'),before)
        self.assertIsNone(result['payload']['measurements']['total_tokens'])
        self.assertEqual(result['payload']['measurements']['charged_or_reserved_tokens'],123)
        self.assertFalse((self.restored/'workers'/self.binding['task_id']/'1/binding.json').exists())
        self.assertFalse((self.restored/'workers'/self.binding['task_id']/'1/codex-home/config.toml').exists())
        self.assertEqual(len(list(self.restored.rglob('*.jsonl'))),1)
        self.assertFalse(archive.read(self.restored/'RESTORED.json')['services_started'])

    def test_running_or_multiple_runs_rejected_before_creating_output(self):
        with self.store.connect() as db:db.execute("UPDATE runs SET state='evaluating' WHERE run_id=?",(self.run,))
        with self.assertRaisesRegex(ValueError,'terminal_run_required'):self.create()
        self.assertFalse(self.backup.exists())
        self.store.cancel(self.run);other=self.store.create_run(fixture());self.store.cancel(other)
        with self.assertRaisesRegex(ValueError,'terminal_run_required'):self.create()
        self.assertFalse(self.backup.exists())

    def test_application_tables_are_not_silently_copied(self):
        with self.store.connect() as db:db.execute('CREATE TABLE app_tokens(secret TEXT)')
        with self.assertRaisesRegex(ValueError,'isolated_runtime_database'):self.create()
        self.assertFalse(self.backup.exists())

    def test_missing_protocol_or_session_refuses_partial_archive(self):
        path=self.folder/'protocol-summary.json';raw=path.read_bytes();path.unlink()
        with self.assertRaisesRegex(ValueError,'protocol_missing'):self.create()
        path.write_bytes(raw)
        next((self.folder/'codex-home/sessions').rglob('*.jsonl')).unlink()
        with self.assertRaisesRegex(ValueError,'session_missing'):self.create()
        self.assertFalse(self.backup.exists())

    def test_wrong_scope_protocol_is_rejected(self):
        path=self.folder/'protocol-summary.json';v=archive.read(path);v['context_hash']='0'*64
        path.write_text(json.dumps(v))
        with self.assertRaisesRegex(ValueError,'binding_mismatch'):self.create()

    def test_snapshot_cannot_invent_context_reads_or_historical_messages(self):
        original=archive.read(self.source/'run.json')
        for kind in ('context','reads','history','mailbox'):
            with self.subTest(kind=kind):
                value=copy.deepcopy(original)
                if kind=='context':value['tasks'][0]['context_json']='{}'
                elif kind=='reads':value['tasks'][0]['read_evidence_ids']=['invented']
                elif kind=='history':value['task_history']=[{'invented':True}]
                else:value['mailbox']=[{'invented':True}]
                (self.source/'run.json').write_text(json.dumps(value))
                with self.assertRaises(ValueError):self.create()
                self.assertFalse(self.backup.exists())
        (self.source/'run.json').write_text(json.dumps(original))

    def test_unknown_worker_epoch_is_rejected(self):
        (self.folder.parent/'2').mkdir()
        with self.assertRaisesRegex(ValueError,'unexpected_worker_epoch'):self.create()

    def test_raw_trace_tampering_refuses_restore_without_output(self):
        self.create();next(self.backup.rglob('*.jsonl')).write_text('changed')
        with self.assertRaisesRegex(ValueError,'file_integrity'):archive.restore(self.backup,self.restored)
        self.assertFalse(self.restored.exists())

    def test_resealed_snapshot_tampering_fails_database_binding(self):
        self.create();v=archive.read(self.backup/'run.json');v['input_hash']='f'*64
        (self.backup/'run.json').write_text(json.dumps(v))
        m=archive.read(self.backup/'manifest.json');m['payload']['files']['run.json']=archive.file_info(self.backup/'run.json')
        m['sha256']=archive.digest(m['payload']);(self.backup/'manifest.json').write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'snapshot_database_mismatch'):archive.verify(self.backup)

    def test_path_traversal_and_extra_file_are_rejected(self):
        self.create();m=archive.read(self.backup/'manifest.json');original=copy.deepcopy(m)
        m['payload']['files']['../escape']={'bytes':0,'sha256':'0'*64};m['sha256']=archive.digest(m['payload'])
        (self.backup/'manifest.json').write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'invalid_member'):archive.verify(self.backup)
        (self.backup/'manifest.json').write_text(json.dumps(original));(self.backup/'unexpected.secret').write_text('x')
        with self.assertRaisesRegex(ValueError,'file_integrity'):archive.verify(self.backup)

    def test_source_changed_during_copy_leaves_no_success_manifest(self):
        original=archive.copy_new;changed=[]
        def changing(source,target):
            original(source,target)
            if not changed:
                p=next(self.source.rglob('*.jsonl'));p.write_text(p.read_text()+'{}\n');changed.append(True)
        with patch.object(archive,'copy_new',side_effect=changing),self.assertRaisesRegex(ValueError,'source_changed'):
            self.create()
        self.assertFalse((self.backup/'manifest.json').exists())

    def test_resealed_manifest_cannot_add_credentials_or_claim_execution(self):
        self.create();original=archive.read(self.backup/'manifest.json')
        path=self.backup/'workers'/self.binding['task_id']/'1/binding.json';path.write_text('{"token":"test-only"}')
        manifest=copy.deepcopy(original);manifest['payload']['files'][path.relative_to(self.backup).as_posix()]=archive.file_info(path)
        manifest['sha256']=archive.digest(manifest['payload']);(self.backup/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'disallowed_member'):archive.verify(self.backup)
        path.unlink();manifest=copy.deepcopy(original);manifest['payload']['model_execution_claim']=True
        manifest['sha256']=archive.digest(manifest['payload']);(self.backup/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'assurance_scope_changed'):archive.verify(self.backup)

    def test_no_overwrite_or_overlapping_directories(self):
        self.create()
        with self.assertRaises(ValueError):self.create()
        self.restored.mkdir()
        with self.assertRaises(FileExistsError):archive.restore(self.backup,self.restored)
        with self.assertRaisesRegex(ValueError,'overlapping'):archive.restore(self.backup,self.backup/'nested')
        with self.assertRaisesRegex(ValueError,'overlapping'):
            archive.create(self.source,self.release_manifest,self.release_archive,self.source/'nested')

    def test_release_archive_must_match_execution_identity(self):
        manifest=archive.read(self.release_manifest);manifest['runtime_version']='invented'
        path=self.base/'wrong-release.json';path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'release_manifest_mismatch'):
            archive.create(self.source,path,self.release_archive,self.backup)
        self.assertFalse(self.backup.exists())

    def test_technical_retry_protocol_is_required_even_without_history_result(self):
        # A new independent synthetic run, kept separate from the cancelled setup.
        self.source=self.base/'retry-source';self.source.mkdir();self.store=Store(self.source/'runtime.sqlite3')
        self.run=self.store.create_run(fixture());old=self.store.claim_next(self.run)
        self.store.bind_execution(old,'old-fixture-thread','turn')
        self.write_worker(old,'old-fixture-thread')
        with self.store.connect() as db:db.execute('UPDATE tasks SET lease_until=0 WHERE task_id=?',(old['task_id'],))
        self.store.recover(self.run);new=self.store.claim_next(self.run)
        self.store.bind_execution(new,'new-fixture-thread','turn');self.write_worker(new,'new-fixture-thread')
        self.ledger=BudgetLedger(self.store,self.run);self.store.cancel(self.run);self.export()
        result=self.create()
        self.assertEqual(len(result['payload']['worker_coverage']),2)
        self.assertEqual({r['lease_epoch'] for r in result['payload']['worker_coverage']},{1,2})


if __name__=='__main__':unittest.main()
