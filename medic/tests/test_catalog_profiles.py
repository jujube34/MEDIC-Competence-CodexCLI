"""Read-only job display metadata must not redefine historical scoring inputs."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import openpyxl
from fastapi.testclient import TestClient
from pydantic import ValidationError

from medic.apps.api.app import create_app
from medic.apps.api.contracts import Catalog
from medic.apps.api.service import Service
from medic.domain.registry import validate_bundle
from medic.p0.assets import matrix_profiles
from medic.p0.common import digest
from medic.tests import test_registry
from medic.tests.test_runtime import fixture


class ProfileExtraction(unittest.TestCase):
    def test_repeated_labels_conflicts_formulas_and_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic.xlsx'
            book=openpyxl.Workbook(); sheet=book.active; sheet.title='明细-测试'
            sheet.append(['配置键','一级分类','二级分类','岗位名称','职级','MEDIC维度'])
            row=['SYN-1','分类甲','分支乙','合成岗位','L9','M']
            sheet.append(row); sheet.append(row); book.save(path)
            profiles,issues=matrix_profiles(path)
            self.assertEqual(profiles,{'SYN-1':{'family':'分类甲','track':'分支乙','title':'合成岗位','level':'L9'}})
            self.assertFalse(issues)
            sheet.append(['SYN-1','冲突分类','分支乙','合成岗位','L9','M'])
            sheet.append(['SYN-2','=1+1','分支乙','合成岗位','L9','M'])
            book.save(path)
            profiles,issues=matrix_profiles(path)
            self.assertEqual(len(issues),2); self.assertNotIn('SYN-2',profiles)
            sheet['B1']='unknown'; book.save(path); book.close()
            with self.assertRaisesRegex(ValueError,'header_changed'): matrix_profiles(path)

    def test_registry_legacy_stays_valid_and_enriched_coverage_is_exact(self):
        value=test_registry.CandidateRegistry().fixture(); validate_bundle(value)
        value['job_profiles']={f'synthetic-job-{i}':{'family':'F','track':'T','title':'J','level':'L1'} for i in range(49)}
        validate_bundle(value)
        value['job_profiles'].pop('synthetic-job-0')
        with self.assertRaisesRegex(ValueError,'profiles_mismatch'): validate_bundle(value)

    def test_frontend_cascades_and_does_not_invent_old_classifications(self):
        binary=shutil.which('node')
        if not binary: self.skipTest('Node.js required')
        script=r"""
const assert=require('node:assert/strict');
const {catalogSelection}=require('./medic/apps/web/navigation.js');
const c={recommendations:{x:{M2:'A'},y:{M2:'C'},z:{M2:'D'}},job_profiles:{
 x:{family:'F1',track:'T1',title:'Same name',level:'L1'},
 y:{family:'F1',track:'T2',title:'Same name',level:'L2'},
 z:{family:'F2',track:'T3',title:'Other',level:'L3'}}};
const before=JSON.stringify(c);
let s=catalogSelection(c,{preferredJob:'y'});assert.equal(s.selected.key,'y');assert.deepEqual(s.tracks,['T1','T2']);
s=catalogSelection(c,{family:'F2',track:'T2',job:'y',preferredJob:'y'});
assert.deepEqual([s.family,s.track,s.selected.key],['F2','T3','z']);
s=catalogSelection(c,{family:'F1',track:'T1',job:'x',preferredJob:'y'});assert.equal(s.selected.key,'x');
s=catalogSelection({recommendations:{'DO-NOT-PARSE-L9':{M2:'B'}}});
assert.equal(s.family,'未提供分类');assert.equal(s.selected.title,'DO-NOT-PARSE-L9');assert.equal(s.selected.level,'');
assert.equal(catalogSelection({recommendations:{}}).selected,null);
assert.equal(JSON.stringify(c),before);
"""
        result=subprocess.run([binary,'-e',script],cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)


class CatalogProfiles(unittest.TestCase):
    def database(self): return Path(self.folder.name)/'runtime.sqlite3'

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.database_location=self.database()
        self.service=Service(self.database_location,Path(self.folder.name)/'vault')
        source=fixture()
        self.catalog={'title':'Synthetic display metadata','synthetic':True,'status':'synthetic',
            **{k:source['versions'][k] for k in ('competency_model_version','job_matrix_version','ai_policy_version')},
            'criteria':source['criteria'],'policies':source['policies'],'recommendations':{'synthetic-role':{'M2':'C'}}}
        import uuid
        self.tenant='profiles-'+uuid.uuid4().hex

    def enriched(self):
        value=copy.deepcopy(self.catalog)
        value['job_profiles']={'synthetic-role':{'family':'Family','track':'Track','title':'Test role','level':'L1'}}
        value['competency_names']={'M2':'Synthetic critical thinking'}
        return value

    def test_legacy_id_preserved_and_new_metadata_is_a_new_snapshot(self):
        expected=digest(self.catalog)
        ident=self.service.register_catalog(self.tenant,self.catalog)
        self.assertEqual(ident,expected)
        value=self.enriched(); newer=self.service.register_catalog(self.tenant,value)
        self.assertNotEqual(newer,ident)
        self.assertEqual(self.service.register_catalog(self.tenant,value),newer)
        with self.service.db.transaction(self.tenant) as db:
            old=json.loads(db.execute('SELECT payload FROM app_catalogs WHERE tenant=? AND registry_id=?',(self.tenant,ident)).fetchone()[0])
        self.assertEqual(old,self.catalog)
        self.assertEqual(old['recommendations'],value['recommendations'])

    def test_metadata_must_cover_existing_jobs_and_competencies(self):
        for change in ('missing_job','extra_name','blank_label'):
            value=self.enriched()
            if change=='missing_job': value['job_profiles']['outside']=value['job_profiles'].pop('synthetic-role')
            if change=='extra_name': value['competency_names']['E1']='Unrelated'
            if change=='blank_label': value['job_profiles']['synthetic-role']['family']='   '
            with self.subTest(change=change),self.assertRaises(ValidationError): Catalog.model_validate(value)

    def test_http_metadata_stays_tenant_scoped_and_has_no_write_route(self):
        self.service.register_catalog(self.tenant,self.enriched())
        own=self.service.db.provision_user(self.tenant,'viewer',role='employee')
        other=self.service.db.provision_user('outside-'+self.tenant,'viewer',role='employee')
        with TestClient(create_app(self.database_location,Path(self.folder.name)/'vault')) as client:
            response=client.get('/catalogs',headers={'Authorization':'Bearer '+own})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()[0]['job_profiles']['synthetic-role']['level'],'L1')
            self.assertEqual(client.get('/catalogs',headers={'Authorization':'Bearer '+other}).json(),[])
            self.assertEqual(client.post('/catalogs',json=self.enriched(),headers={'Authorization':'Bearer '+own}).status_code,405)

    def test_display_metadata_never_enters_agent_input_or_rewrites_history(self):
        from unittest.mock import patch
        from medic.tests.test_ingestion import word
        from medic.ingestion.parser import extract
        self.service.db.provision_employee(self.tenant,'SYN-1','synthetic-role',synthetic=True)
        token=self.service.db.provision_user(self.tenant,'employee',role='employee',employee_ref='SYN-1')
        first=self.service.register_catalog(self.tenant,self.enriched())
        with patch('medic.ingestion.vault.parse_file',side_effect=lambda path,kind,limits:extract(Path(path).read_bytes(),kind,limits)), TestClient(create_app(self.database_location,Path(self.folder.name)/'vault')) as client:
            auth={'Authorization':'Bearer '+token}
            uploaded=client.post('/employees/SYN-1/materials?filename=synthetic.docx&period=Q4',content=word('Synthetic profile separation test.'),headers=auth)
            self.assertEqual(uploaded.status_code,201)
            body={'employee_ref':'SYN-1','period':'Q4','registry_id':first,'material_ids':[uploaded.json()['snapshot_id']],'competency_scope':['M2']}
            response=client.post('/assessments',json=body,headers={**auth,'Idempotency-Key':'profile-create-1'})
            self.assertEqual(response.status_code,201,response.text)
            ident=response.json()['assessment_id']
            with self.service.db.transaction(self.tenant) as db:
                before=dict(db.execute('SELECT * FROM app_assessments WHERE assessment_id=?',(ident,)).fetchone())
            source=json.loads(before['source_json'])
            for field in ('job_profiles','competency_names','recommendations'):
                self.assertNotIn(field,source)
            updated=self.enriched(); updated['job_profiles']['synthetic-role']['title']='New title'
            updated['job_matrix_version']='synthetic-new-matrix'; updated['recommendations']['synthetic-role']['M2']='D'
            self.service.register_catalog(self.tenant,updated)
            with self.service.db.transaction(self.tenant) as db:
                after=dict(db.execute('SELECT * FROM app_assessments WHERE assessment_id=?',(ident,)).fetchone())
            self.assertEqual(after,before)
            self.assertEqual(json.loads(after['recommendations_json']),{'M2':'C'})


class PostgreSQLCatalogProfiles(CatalogProfiles):
    def database(self):
        from medic.tests.test_postgres import database
        return database()


if __name__=='__main__': unittest.main()
