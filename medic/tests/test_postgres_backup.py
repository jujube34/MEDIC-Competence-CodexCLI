"""Opt-in native PostgreSQL backup/restore tests; reports are explicit fixtures."""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
from urllib.parse import urlsplit, urlunsplit

from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo, conninfo_to_dict

from medic.apps.api.app import create_app
from medic.apps.api.contracts import CreateAssessment, ReviewRequest, FinalizeRequest
from medic.apps.api.service import Service
from medic.deploy import postgres_backup as backup
from medic.runtime import build
from medic.runtime.budget import BudgetLedger
from medic.tests.test_ingestion import word
from medic.tests.test_runtime import fixture
from medic.tests.test_postgres import database


class PostgreSQLBackupContracts(unittest.TestCase):
    def setUp(self):
        configured = database()
        if conninfo_to_dict(configured).get('host') not in ('127.0.0.1','localhost','::1'):
            raise ValueError('backup_tests_require_loopback')
        def uri(database_name):
            parsed=urlsplit(configured)
            if parsed.scheme not in ('postgres','postgresql'): raise ValueError('test_postgres_uri_required')
            return urlunsplit(parsed._replace(path='/'+database_name))
        self.admin = uri('postgres')
        self.bin = Path(os.environ.get('MEDIC_TEST_POSTGRES_BIN',''))
        if not (self.bin/'pg_dump.exe').is_file():
            raise unittest.SkipTest('MEDIC_TEST_POSTGRES_BIN is not configured')
        self.tag = uuid.uuid4().hex[:16]
        self.source_name = 'medic_backup_'+self.tag+'_test'
        self.restore_name = 'medic_restore_'+self.tag+'_test'
        self.owned = [self.source_name,self.restore_name]
        self.addCleanup(self.drop_owned)
        with backup.connect(self.admin,autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.source_name)))
        self.dsn = uri(self.source_name)
        self.restored_dsn = uri(self.restore_name)
        base = build.ROOT/'artifacts/postgres-backup-tests'; base.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.assertTrue(self.root.resolve().is_relative_to(base.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.vault = self.root/'vault'; self.target = self.root/'backup'; self.recovered=self.root/'restored'
        self.service=Service(self.dsn,self.vault)
        self.assertTrue(self.service.runtime.postgres, 'test_must_use_real_postgresql')
        self.tenant='synthetic-backup'; self.employee='SYN-001'
        app=self.service.db
        app.provision_employee(self.tenant,self.employee,'synthetic-role',synthetic=True)
        self.employee_token=app.provision_user(self.tenant,'employee',role='employee',employee_ref=self.employee)
        self.manager_token=app.provision_user(self.tenant,'manager',role='manager')
        app.grant(self.tenant,'manager',self.employee,can_review=True)
        self.ep=app.authenticate(self.employee_token); self.mp=app.authenticate(self.manager_token)
        source=fixture()
        self.registry=self.service.register_catalog(self.tenant,{'title':'Synthetic PostgreSQL backup fixture',
            'synthetic':True,'status':'synthetic',**{k:source['versions'][k] for k in ('competency_model_version','job_matrix_version','ai_policy_version')},
            'criteria':source['criteria'],'policies':source['policies'],'recommendations':{'synthetic-role':{'M2':'C'}}})
        self.doc=self.root/'synthetic.docx'; self.doc.write_bytes(word('Synthetic PostgreSQL backup evidence.'))
        self.material=self.service.ingest(self.ep,self.employee,self.doc,'Q4')
        self.assessment=self.service.create(self.ep,CreateAssessment(employee_ref=self.employee,period='Q4',registry_id=self.registry,
            material_ids=[self.material['snapshot_id']],competency_scope=['M2']),'pg-backup-create')
        self.run=self.assessment['run_id']

    def drop_owned(self):
        with backup.connect(self.admin,autocommit=True) as db:
            for name in self.owned:
                if self.tag not in name or not name.endswith('_test'): raise ValueError('unsafe_test_cleanup')
                db.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(name)))

    def create(self): return backup.create(self.dsn,self.vault,self.target,self.bin)
    def restore(self): return backup.restore(self.target,self.admin,self.restore_name,self.recovered,self.bin)

    def seed_human_final(self):
        # Explicit fixture only; no model result or quality provenance is claimed.
        report={'gate':{'passed':False,'issues':['synthetic_backup_fixture_no_model']},
                'assessments':[{'competency_id':'M2','grade':'B','status':'suggested'}]}
        with self.service.runtime.connect(self.run) as db:
            db.execute('UPDATE runs SET report_json=?,state=? WHERE run_id=?',
                (json.dumps(report),'awaiting_human_review',self.run))
        evidence=self.service.spans(self.service.material(self.mp,self.material['snapshot_id']))[0]
        request=ReviewRequest(expected_revision=1,decisions=[{'competency_id':'M2','disposition':'confirmed','grade':'B',
            'reason':'Synthetic restore exercise; no employee decision.',
            'evidence_refs':[{k:evidence[k] for k in ('evidence_id','quote','span_hash')}]}])
        reviewed=self.service.review(self.mp,self.assessment['assessment_id'],request,'pg-backup-review')
        self.service.finalize(self.mp,self.assessment['assessment_id'],FinalizeRequest(
            expected_revision=reviewed['revision'],limitation_disposition='Synthetic fixture tests backup only; preserve failed AI gate.'),'pg-backup-final')
        return report

    def test_report_review_permissions_and_blob_restore(self):
        self.seed_human_final()
        expected=self.service.detail(self.ep,self.assessment['assessment_id'])
        self.create(); result=self.restore()
        self.assertEqual(result['copied_sessions_invalidated'],2)
        app=create_app(self.restored_dsn,self.recovered/'vault')
        service=app.state.service
        token=service.db.provision_user(self.tenant,'employee',role='employee',employee_ref=self.employee)
        manager=service.db.provision_user(self.tenant,'manager',role='manager')
        outsider=service.db.provision_user(self.tenant,'outsider',role='manager')
        with TestClient(app) as client:
            auth=lambda value:{'Authorization':'Bearer '+value}
            for old in (self.employee_token,self.manager_token):
                self.assertEqual(client.get('/me',headers=auth(old)).status_code,401)
            self.assertEqual(client.get('/assessments/'+self.assessment['assessment_id'],headers=auth(token)).json(),expected)
            self.assertTrue(client.get('/assessments/'+self.assessment['assessment_id'],headers=auth(manager)).json()['permissions']['can_review'])
            self.assertEqual(client.get('/assessments/'+self.assessment['assessment_id'],headers=auth(outsider)).status_code,404)
            self.assertEqual(client.get('/materials/'+self.material['snapshot_id']+'/source',headers=auth(token)).content,self.doc.read_bytes())
        with backup.connect(self.restored_dsn) as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM model_requests').fetchone()['n'],0)
        self.assertEqual(self.service.db.authenticate(self.employee_token),self.ep)

    def test_active_budget_lease_and_original_deadline_preserved(self):
        self.service.runtime.claim_next(self.run)
        ledger=BudgetLedger(self.service.runtime,self.run); ledger.reserve(4321)
        old=self.service.runtime.snapshot(self.run); budget=ledger.snapshot()
        self.create(); self.restore()
        service=Service(self.restored_dsn,self.recovered/'vault')
        self.assertEqual(service.runtime.snapshot(self.run),old)
        self.assertEqual(BudgetLedger(service.runtime,self.run).snapshot(),budget)

    def test_exported_snapshot_excludes_later_application_write(self):
        real=backup.pg_tool
        def tool(bin_dir,name,args,**kwargs):
            if name=='pg_dump' and '--format=custom' in args:
                self.service.ingest(self.ep,self.employee,self.doc,'Q4')
            return real(bin_dir,name,args,**kwargs)
        with patch.object(backup,'pg_tool',side_effect=tool): manifest=self.create()
        self.assertEqual(manifest['counts']['materials'],1)
        self.assertEqual(manifest['counts']['snapshots'],2)
        self.restore()
        with backup.connect(self.restored_dsn) as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM app_materials').fetchone()['n'],1)
        with backup.connect(self.dsn) as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM app_materials').fetchone()['n'],2)

    def test_native_sequence_state_allows_new_runtime_events(self):
        self.create(); self.restore()
        service=Service(self.restored_dsn,self.recovered/'vault')
        with service.runtime.connect(self.run) as db:
            maximum=db.execute('SELECT max(seq) FROM events').fetchone()[0]
            service.runtime.event(db,self.run,None,'synthetic_restore_sequence_probe',{})
            self.assertGreater(db.execute('SELECT max(seq) FROM events').fetchone()[0],maximum)
        new=service.runtime.create_run(fixture())
        self.assertNotEqual(new,self.run)

    def test_new_restore_database_has_no_public_connect_grant(self):
        self.create(); self.restore()
        with backup.connect(self.admin) as db:
            grants=db.execute("SELECT a.grantee,a.privilege_type FROM pg_database d, LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a WHERE d.datname=%s",(self.restore_name,)).fetchall()
        self.assertFalse(any(r['grantee']==0 and r['privilege_type']=='CONNECT' for r in grants))

    def test_existing_database_is_never_replaced(self):
        self.create()
        with backup.connect(self.admin,autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.restore_name)))
        with self.assertRaisesRegex(ValueError,'already_exists'): self.restore()
        self.assertFalse(self.recovered.exists())

    def test_corrupt_archive_refused_before_database_creation(self):
        self.create(); (self.target/'runtime.dump').write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError,'member_integrity'): self.restore()
        with backup.connect(self.admin) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(self.restore_name,)).fetchone())
        self.assertFalse(self.recovered.exists())

    def test_invalid_archive_even_with_rewritten_file_hash_is_refused(self):
        self.create(); (self.target/'runtime.dump').write_bytes(b'broken')
        manifest=json.loads((self.target/'manifest.json').read_text())
        manifest['files']['runtime.dump']=backup.file_info(self.target/'runtime.dump')
        (self.target/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError,'pg_restore_failed'): self.restore()
        self.assertFalse(self.recovered.exists())

    def test_non_public_schema_is_not_silently_omitted(self):
        with backup.connect(self.dsn) as db:
            db.execute('CREATE SCHEMA other'); db.execute('CREATE TABLE other.extra(value TEXT)')
        with self.assertRaisesRegex(ValueError,'non_public_tables'): self.create()
        self.assertFalse((self.target/'manifest.json').exists())

    def test_connection_options_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError,'explicit_supported'):
            backup.connection_env(make_conninfo(self.dsn,options='-c search_path=other'))

    def test_wrong_build_and_nested_target_rejected(self):
        self.create()
        with self.assertRaisesRegex(ValueError,'overlaps_backup'):
            backup.restore(self.target,self.admin,self.restore_name,self.target/'nested',self.bin)
        manifest=json.loads((self.target/'manifest.json').read_text())
        manifest['captured_build']['manifest_sha256']='0'*64
        (self.target/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'captured_build'): self.restore()
        self.assertFalse(self.recovered.exists())
