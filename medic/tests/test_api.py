"""HTTP/auth/workflow contracts. Seeded report cases are not model-quality evidence."""
import copy
import json
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from medic.apps.api.app import create_app
from medic.apps.api.database import AppError
from medic.apps.api.contracts import Catalog
from medic.ingestion.parser import extract
from medic.p0.common import digest
from medic.runtime.budget import BudgetLedger
from medic.runtime.store import Denied
from medic.tests.test_ingestion import word, excel
from medic.tests.test_runtime import fixture


class ApplicationContracts(unittest.TestCase):
    def database(self): return Path(self.temp.name)/'application.sqlite3'

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app=create_app(self.database(),Path(self.temp.name)/'vault')
        self.service=self.app.state.service
        self.client=TestClient(self.app,raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.tenant='test-'+uuid.uuid4().hex
        db=self.service.db
        db.provision_employee(self.tenant,'E1','synthetic-role',synthetic=True)
        db.provision_employee(self.tenant,'E2','synthetic-role',synthetic=True)
        self.employee=db.provision_user(self.tenant,'employee',role='employee',employee_ref='E1')
        self.other=db.provision_user(self.tenant,'other',role='employee',employee_ref='E2')
        self.manager=db.provision_user(self.tenant,'manager',role='manager')
        self.admin=db.provision_user(self.tenant,'admin',role='admin')
        db.grant(self.tenant,'manager','E1',can_review=True)
        db.grant(self.tenant,'admin','E1',can_review=True)
        source=fixture()
        self.catalog={'title':'Synthetic software test only','synthetic':True,'status':'synthetic',
                      **{k:source['versions'][k] for k in ('competency_model_version','job_matrix_version','ai_policy_version')},
                      'criteria':source['criteria'],'policies':source['policies'],
                      'recommendations':{'synthetic-role':{'M2':'C'}}}
        self.registry=self.service.register_catalog(self.tenant,self.catalog)
        # Parser/OS limits have separate real-process tests. Keep boundary cases deterministic.
        self.parser=patch('medic.ingestion.vault.parse_file',side_effect=lambda path,kind,limits: extract(Path(path).read_bytes(),kind,limits))
        self.parser.start(); self.addCleanup(self.parser.stop)

    @staticmethod
    def auth(token, key=None):
        return {'Authorization':'Bearer '+token,**({'Idempotency-Key':key} if key else {})}

    def upload(self, *, data=None, filename='synthetic.docx',period='Q4',token=None,employee='E1'):
        return self.client.post('/employees/'+employee+'/materials',params={'filename':filename,'period':period},
                                content=word('Synthetic exact evidence for software tests only.') if data is None else data,
                                headers=self.auth(token or self.employee))

    def create(self, *, registry=None, material=None, codes=None, key='create-0001'):
        if material is None:
            response=self.upload(); self.assertEqual(response.status_code,201,response.text); material=response.json()['snapshot_id']
        self.body={'employee_ref':'E1','period':'Q4','registry_id':registry or self.registry,'material_ids':[material],'competency_scope':codes or ['M2']}
        response=self.client.post('/assessments',json=self.body,headers=self.auth(self.employee,key))
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def seed_report(self, assessment, *, grade='B',passed=False):
        # Explicitly a report fixture, bypassing worker execution only in tests.
        snap=self.service.runtime.snapshot(assessment['run_id'])
        report={'gate':{'passed':passed,'issues':['fixture_manual_boundary'] if not passed else []},
                'assessments':[{'competency_id':c,'grade':grade,'status':'suggested' if grade else 'insufficient_evidence'} for c in snap['source']['goal']['competency_scope']],
                'task_history':[{'context_json':'internal-context-must-not-leak'}]}
        with self.service.runtime.connect(assessment['run_id']) as db:
            db.execute('UPDATE runs SET report_json=?,state=? WHERE run_id=?',(json.dumps(report),'ready_for_human_review' if passed else 'awaiting_human_review',assessment['run_id']))
        return report

    def review(self, a, *, revision=1,grade='D',disposition='adjusted',code='M2',key='review-0001',token=None,**extra):
        decision={'competency_id':code,'disposition':disposition,'grade':grade,'reason':'Independent reviewer observation','manual_basis':'Observed independently in a synthetic review scenario.',**extra.pop('decision',{})}
        return self.client.post('/assessments/'+a['assessment_id']+'/reviews',json={'expected_revision':revision,'decisions':[decision],**extra},headers=self.auth(token or self.manager,key))

    def test_authority_scope_and_immediate_revocation(self):
        self.assertEqual(self.client.get('/employees').status_code,401)
        self.assertEqual([x['employee_ref'] for x in self.client.get('/employees',headers=self.auth(self.employee)).json()],['E1'])
        headers={**self.auth(self.other),'X-Role':'admin','X-Employee':'E1','X-Tenant':self.tenant}
        self.assertEqual(self.client.get('/employees/E1/materials',headers=headers).status_code,404)
        tenant2='second-'+uuid.uuid4().hex
        self.service.db.provision_employee(tenant2,'E1','synthetic-role',synthetic=True)
        foreign=self.service.db.provision_user(tenant2,'employee',role='employee',employee_ref='E1')
        mid=self.upload().json()['snapshot_id']
        self.assertEqual(self.client.get('/materials/'+mid,headers=self.auth(foreign)).status_code,404)
        with self.service.db.transaction(self.tenant) as db:
            db.execute('UPDATE app_tokens SET expires=? WHERE token_hash=?',(time.time()-1,digest(self.employee)))
        self.assertEqual(self.client.get('/me',headers=self.auth(self.employee)).status_code,401)
        with self.service.db.transaction(self.tenant) as db: db.execute('UPDATE app_users SET active=0 WHERE tenant=? AND user_id=?',(self.tenant,'manager'))
        self.assertEqual(self.client.get('/me',headers=self.auth(self.manager)).status_code,401)

    def test_upload_locators_source_download_and_path_rejection(self):
        data=word('Exact  text kept')
        response=self.upload(data=data); self.assertEqual(response.status_code,201,response.text)
        mid=response.json()['snapshot_id']
        block=self.client.get('/materials/'+mid+'/blocks',headers=self.auth(self.employee)).json()['blocks'][0]
        self.assertEqual(block['quote'],'Exact  text kept'); self.assertIn('locator',block)
        located=self.client.get('/materials/'+mid+'/blocks/'+block['evidence_id'],headers=self.auth(self.employee))
        self.assertEqual(located.json()['block'],block)
        self.assertEqual(located.json()['index'],0)
        self.assertEqual(self.client.get('/materials/'+mid+'/blocks/unknown',headers=self.auth(self.employee)).status_code,404)
        self.assertEqual(self.client.get('/materials/'+mid+'/blocks/'+block['evidence_id'],headers=self.auth(self.other)).status_code,404)
        source=self.client.get('/materials/'+mid+'/source',headers=self.auth(self.manager))
        self.assertEqual(source.content,data); self.assertEqual(source.headers['cache-control'],'no-store')
        for name in ('../bad.docx','C:\\bad.docx','CON.docx','bad.docx:ads','bad.html'):
            self.assertEqual(self.upload(filename=name).status_code,400)
        self.assertEqual(self.upload(data=b'').status_code,400)
        self.assertEqual(self.client.get('/materials/'+mid+'/blocks?limit=101',headers=self.auth(self.employee)).status_code,422)

    def test_revoked_scope_during_parse_cannot_publish_upload(self):
        original=self.service.vault.ingest
        def revoke(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.service.db.transaction(self.tenant) as db:
                db.execute('DELETE FROM app_grants WHERE tenant=? AND user_id=?',(self.tenant,'manager'))
            return result
        with patch.object(self.service.vault,'ingest',side_effect=revoke):
            self.assertEqual(self.upload(token=self.manager).status_code,404)
        self.assertEqual(self.client.get('/employees/E1/materials',headers=self.auth(self.employee)).json(),[])

    def test_quality_period_and_business_admission(self):
        data=excel('<sheetData><row r="1"><c r="A1"><f>1+1</f><v/></c></row></sheetData>')
        response=self.upload(data=data,filename='formula.xlsx')
        self.assertEqual(response.json()['state'],'needs_review')
        body={'employee_ref':'E1','period':'Q4','registry_id':self.registry,'material_ids':[response.json()['snapshot_id']],'competency_scope':['M2']}
        self.assertEqual(self.client.post('/assessments',json=body,headers=self.auth(self.employee,'quality-01')).status_code,409)
        body['material_ids']=[self.upload(period='Q3').json()['snapshot_id']]
        self.assertEqual(self.client.post('/assessments',json=body,headers=self.auth(self.employee,'period-001')).status_code,400)
        catalog=copy.deepcopy(self.catalog); catalog.update(synthetic=False,status='awaiting_business_signoff')
        body.update(registry_id=self.service.register_catalog(self.tenant,catalog),period='Q3')
        self.assertEqual(self.client.post('/assessments',json=body,headers=self.auth(self.employee,'policy-001')).json()['error'],'production_inputs_not_enabled')

    def test_creation_replay_preserves_budget_and_rejects_changed_body(self):
        a=self.create(); snap=self.service.runtime.snapshot(a['run_id'])
        ledger=BudgetLedger(self.service.runtime,a['run_id']); ledger.reserve(50)
        again=self.client.post('/assessments',json=self.body,headers=self.auth(self.employee,'create-0001'))
        self.assertEqual(again.json()['run_id'],a['run_id'])
        self.assertEqual(self.service.runtime.snapshot(a['run_id'])['deadline'],snap['deadline'])
        with self.service.runtime.connect(a['run_id']) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM model_requests WHERE run_id=?',(a['run_id'],)).fetchone()[0],1)
        self.body['period']='changed'
        self.assertEqual(self.client.post('/assessments',json=self.body,headers=self.auth(self.employee,'create-0001')).status_code,409)
        self.assertNotIn('recommendations',snap['source'])

    def test_crash_after_runtime_creation_recovers_same_run(self):
        material=self.upload().json()['snapshot_id']
        body={'employee_ref':'E1','period':'Q4','registry_id':self.registry,'material_ids':[material],'competency_scope':['M2']}
        original=self.service.runtime.create_run
        captured=[]
        def crash(*args,**kwargs):
            captured.append(original(*args,**kwargs)); raise RuntimeError('injected crash after commit')
        with patch.object(self.service.runtime,'create_run',side_effect=crash):
            result=self.client.post('/assessments',json=body,headers=self.auth(self.employee,'crash-0001'))
            self.assertEqual(result.status_code,500); self.assertNotIn('injected',result.text)
        again=self.client.post('/assessments',json=body,headers=self.auth(self.employee,'crash-0001'))
        self.assertEqual(again.status_code,201,again.text); self.assertEqual(again.json()['run_id'],captured[0])
        source=self.service.runtime.snapshot(captured[0])['source']; source['period']='changed'
        with self.assertRaisesRegex(Denied,'run_creation_key_conflict'):
            original(source,creation_key=again.json()['assessment_id'])

    def test_review_restrictions_revision_history_and_finalization(self):
        a=self.create()
        self.assertEqual(self.review(a).status_code,409)
        original=self.seed_report(a)
        for token in (self.employee,self.admin): self.assertEqual(self.review(a,token=token).status_code,403)
        first=self.review(a); self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(self.review(a).json(),first.json())
        self.assertEqual(self.review(a,key='stale-0001').status_code,409)
        detail=self.client.get('/assessments/'+a['assessment_id'],headers=self.auth(self.employee)).json()
        self.assertEqual(detail['rows'][0]['ai']['grade'],'B')
        self.assertEqual(detail['rows'][0]['human']['decision']['grade'],'D')
        self.assertEqual(detail['rows'][0]['difference_from_recommendation'],1)
        self.assertNotIn('internal-context',json.dumps(detail))
        endpoint='/assessments/'+a['assessment_id']+'/finalize'
        body={'expected_revision':2,'limitation_disposition':'Human handled the synthetic AI limitation independently.'}
        final=self.client.post(endpoint,json=body,headers=self.auth(self.manager,'final-0001'))
        self.assertEqual(final.status_code,200,final.text)
        self.assertEqual(self.client.post(endpoint,json=body,headers=self.auth(self.manager,'final-0001')).json(),final.json())
        self.assertEqual(self.review(a,revision=3,key='after-0001').status_code,409)
        detail=self.service.detail(self.service.db.authenticate(self.employee),a['assessment_id'])
        self.assertFalse(detail['final']['ai_gate']['passed']); self.assertFalse(detail['final_employee_decision'])
        self.assertEqual(self.service.runtime.snapshot(a['run_id'])['report'],original)
        history=self.client.get('/assessments/'+a['assessment_id']+'/history',headers=self.auth(self.manager)).json()
        self.assertEqual([h['kind'] for h in history].count('human_review_saved'),1)

    def test_concurrent_review_has_one_winner(self):
        a=self.create(); self.seed_report(a)
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda i:self.review(a,key=f'concurrent-{i}').status_code,range(2)))
        self.assertEqual(sorted(statuses),[200,409])

    def test_pdf_preview_is_scoped_and_only_highlights_frozen_evidence(self):
        from medic.tests.test_preview import pdf_fixture
        original=pdf_fixture(90,True)
        saved=self.upload(data=original,filename='synthetic.pdf').json();ident=saved['snapshot_id']
        self.assertTrue(saved['preview']['supported']);self.assertEqual(saved['preview']['page_count'],2)
        endpoint='/materials/'+ident+'/preview'
        span=self.client.get('/materials/'+ident+'/blocks',headers=self.auth(self.employee)).json()['blocks'][0]
        response=self.client.get(endpoint,params={'evidence_id':span['evidence_id']},headers=self.auth(self.manager))
        self.assertEqual(response.status_code,200,response.text[:100] if response.status_code!=200 else '')
        info=json.loads(response.headers['X-Preview-Info'])
        self.assertEqual(info['highlight'],'exact_block');self.assertEqual(info['source_sha256'],saved['source_sha256'])
        self.assertEqual(info['image_sha256'],digest(response.content));self.assertEqual(info['rotation'],90)
        self.assertEqual(response.headers['Content-Type'],'image/png');self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.client.get(endpoint).status_code,401)
        self.assertEqual(self.client.get(endpoint,headers=self.auth(self.other)).status_code,404)
        for params,status in [({'page':3},404),({'width':1601},422),({'evidence_id':'invented'},404),({'page':2,'evidence_id':span['evidence_id']},400)]:
            self.assertEqual(self.client.get(endpoint,params=params,headers=self.auth(self.employee)).status_code,status)
        self.assertEqual(self.client.get('/materials/'+ident+'/source',headers=self.auth(self.employee)).content,original)
        office=self.upload().json()['snapshot_id']
        with patch('medic.ingestion.office.engine_available',return_value=False):
            self.assertEqual(self.client.get('/materials/'+office+'/preview',headers=self.auth(self.employee)).status_code,503)

    def test_ocr_material_keeps_review_gate_scope_and_original_page_without_red_box(self):
        from medic.tests.test_ocr import scanned_pdf
        data=scanned_pdf()
        def recognize(page,collector,number):
            collector.add('pdf_ocr',{'page':number,'ocr_block':1,'bbox':[100,100,200,200]},
                          'Synthetic OCR boundary fixture',{'ocr_applied':True},['ocr_text_requires_review'])
            return {'recognized_blocks':1,'ocr_applied':True}
        with patch('medic.ingestion.ocr.recognize_page',side_effect=recognize), patch('medic.ingestion.vault.parse_file',side_effect=lambda path,kind,limits: extract(Path(path).read_bytes(),kind,limits,ocr=True)):
            uploaded=self.upload(data=data,filename='synthetic-scan.pdf')
        self.assertEqual(uploaded.status_code,201,uploaded.text)
        material=uploaded.json();ident=material['snapshot_id'];headers=self.auth(self.employee)
        self.assertEqual(material['state'],'needs_review')
        self.assertEqual(material['parser_version'],'medic-extraction-v3')
        block=self.client.get('/materials/'+ident+'/blocks',headers=headers).json()['blocks'][0]
        self.assertIn('ocr_text_requires_review',block['quality_flags'])
        url='/materials/'+ident+'/preview'
        response=self.client.get(url,params={'page':1,'evidence_id':block['evidence_id']},headers=headers)
        self.assertEqual(response.status_code,200,response.text[:100] if response.status_code!=200 else '')
        info=json.loads(response.headers['X-Preview-Info'])
        self.assertEqual(info['highlight'],'ocr_page_only');self.assertIsNone(info['highlight_pixels'])
        self.assertEqual(self.client.get(url,headers=self.auth(self.other)).status_code,404)
        self.assertEqual(self.client.get('/materials/'+ident+'/source',headers=headers).content,data)
        rejected=self.client.post('/assessments',json={'employee_ref':'E1','period':'Q4','registry_id':self.registry,'material_ids':[ident],'competency_scope':['M2']},headers=self.auth(self.employee,'ocr-denied-0001'))
        self.assertEqual(rejected.status_code,409)
        self.assertIn('material_requires_review',rejected.text)

    def test_office_preview_cache_mapping_and_scope(self):
        from medic.tests.test_preview import pdf_fixture
        from medic.ingestion.office import ENGINE_MANIFEST_SHA256
        quote='Second page - distinct evidence.';original=word(quote)
        saved=self.upload(data=original).json();ident=saved['snapshot_id'];endpoint='/materials/'+ident+'/preview'
        span=self.client.get('/materials/'+ident+'/blocks',headers=self.auth(self.employee)).json()['blocks'][0]
        pdf=pdf_fixture();conversion={'source_sha256':saved['source_sha256'],'pdf_sha256':digest(pdf),'page_count':2,
            'preview_version':'synthetic-converter','engine':'synthetic-test','version':'test','engine_manifest_sha256':ENGINE_MANIFEST_SHA256,'system_fonts_sha256':'0'*64}
        with patch('medic.apps.api.office_preview.convert_file',return_value=(conversion,pdf)) as convert:
            self.assertEqual(self.client.get(endpoint,headers=self.auth(self.other)).status_code,404);convert.assert_not_called()
            response=self.client.get(endpoint,params={'evidence_id':span['evidence_id']},headers=self.auth(self.manager))
            self.assertEqual(response.status_code,200,response.text[:100] if response.status_code!=200 else '')
            info=json.loads(response.headers['X-Preview-Info']);self.assertTrue(info['derived']);self.assertEqual(info['page'],2)
            self.assertEqual(info['highlight'],'unique_text');self.assertEqual(info['source_sha256'],saved['source_sha256'])
            self.assertEqual(self.client.get(endpoint,params={'page':1},headers=self.auth(self.employee)).status_code,200)
            self.assertEqual(convert.call_count,1)
            self.assertEqual(self.client.get('/materials/'+ident+'/source',headers=self.auth(self.employee)).content,original)
            cached=next(self.service.vault.root.rglob('document.pdf'));cached.write_bytes(b'changed')
            self.assertEqual(self.client.get(endpoint,headers=self.auth(self.employee)).status_code,422)
            self.assertEqual(convert.call_count,1)

    def test_office_conversion_revocation_cannot_release_image(self):
        from medic.tests.test_preview import pdf_fixture
        from medic.ingestion.office import ENGINE_MANIFEST_SHA256
        saved=self.upload().json();pdf=pdf_fixture()
        info={'source_sha256':saved['source_sha256'],'pdf_sha256':digest(pdf),'page_count':2,'preview_version':'synthetic-converter','engine':'synthetic-test','version':'test','engine_manifest_sha256':ENGINE_MANIFEST_SHA256,'system_fonts_sha256':'0'*64}
        def revoke(*args,**kwargs):
            with self.service.db.transaction(self.tenant) as db:db.execute('DELETE FROM app_grants WHERE tenant=? AND user_id=?',(self.tenant,'manager'))
            return info,pdf
        with patch('medic.apps.api.office_preview.convert_file',side_effect=revoke):
            response=self.client.get('/materials/'+saved['snapshot_id']+'/preview',headers=self.auth(self.manager))
        self.assertEqual(response.status_code,404);self.assertNotEqual(response.headers['content-type'],'image/png')

    def test_revoked_authorization_during_preview_cannot_release_pixels(self):
        from medic.tests.test_preview import pdf_fixture
        saved=self.upload(data=pdf_fixture(),filename='synthetic.pdf').json()
        def rendered(*args,**kwargs):
            with self.service.db.transaction(self.tenant) as db:
                db.execute('DELETE FROM app_grants WHERE tenant=? AND user_id=?',(self.tenant,'manager'))
            return {'source_sha256':saved['source_sha256']},b'sensitive-pixels-must-not-release'
        with patch('medic.apps.api.preview.render_file',side_effect=rendered):
            response=self.client.get('/materials/'+saved['snapshot_id']+'/preview',headers=self.auth(self.manager))
        self.assertEqual(response.status_code,404);self.assertNotIn(b'sensitive-pixels',response.content)

    def test_cancelled_preview_keeps_render_slot_until_worker_returns(self):
        import asyncio
        import threading
        import httpx
        from medic.apps.api.preview import preview_slots
        from medic.tests.test_preview import pdf_fixture
        saved=self.upload(data=pdf_fixture(),filename='synthetic.pdf').json()
        started,release,finished=threading.Event(),threading.Event(),threading.Event()
        def rendered(*args,**kwargs):
            started.set()
            try:
                if not release.wait(10):raise TimeoutError('test_release_timeout')
                return {'source_sha256':saved['source_sha256']},b'synthetic-render-result'
            finally:finished.set()
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver') as client:
                task=asyncio.create_task(client.get('/materials/'+saved['snapshot_id']+'/preview',headers=self.auth(self.manager)))
                try:
                    for _ in range(200):
                        if started.is_set():break
                        await asyncio.sleep(.01)
                    self.assertTrue(started.is_set());task.cancel();await asyncio.sleep(.05)
                    with self.assertRaisesRegex(AppError,'preview_busy'):
                        with preview_slots(self.service.vault,self.tenant,'manager'):pass
                finally:
                    release.set()
                    try:await task
                    except asyncio.CancelledError:pass
        with patch('medic.apps.api.preview.render_file',side_effect=rendered):asyncio.run(scenario())
        self.assertTrue(finished.wait(5))
        deadline=time.monotonic()+5
        while True:
            try:
                with preview_slots(self.service.vault,self.tenant,'manager'):break
            except AppError:
                if time.monotonic()>deadline:raise
                time.sleep(.01)

    def test_review_basis_is_frozen_scoped_and_authorized(self):
        from medic.p0.common import CODES
        a=self.create()
        original=self.service.runtime.snapshot(a['run_id'])['source']
        updated=copy.deepcopy(self.catalog)
        updated['criteria']['M2'][0]['description']='New catalog must not rewrite the old assessment.'
        updated['competency_model_version']='synthetic-new-version'
        self.service.register_catalog(self.tenant,updated)
        with self.service.db.transaction(self.tenant) as db:
            db.execute('UPDATE app_employees SET job_key=? WHERE tenant=? AND employee_ref=?',('new-role',self.tenant,'E1'))
        endpoint='/assessments/'+a['assessment_id']
        for token in (self.employee,self.manager):
            detail=self.client.get(endpoint,headers=self.auth(token)).json()
            self.assertEqual(detail['job_key'],original['job_key'])
            self.assertEqual(detail['framework_scope'],list(CODES))
            self.assertEqual([r['competency_id'] for r in detail['rows']],['M2'])
            self.assertEqual(detail['rows'][0]['criteria'],original['criteria']['M2'])
            self.assertEqual(detail['rows'][0]['policy'],original['policies']['M2'])
            self.assertEqual(detail['versions'],original['versions'])
        self.assertEqual(self.client.get(endpoint,headers=self.auth(self.other)).status_code,404)
        with self.service.db.transaction(self.tenant) as db:
            db.execute('DELETE FROM app_grants WHERE tenant=? AND user_id=?',(self.tenant,'manager'))
        self.assertEqual(self.client.get(endpoint,headers=self.auth(self.manager)).status_code,404)

    def test_null_non_scoring_human_risk_and_coverage(self):
        catalog=copy.deepcopy(self.catalog)
        catalog['criteria']['C1']=[{'criterion_id':'C1.A1','grade':'A','description':'Synthetic risk standard'}]
        catalog['policies']['C1']={'mode':'risk_scan','cap':None,'signed_off':False}
        catalog['recommendations']['synthetic-role']['C1']='B'
        registry=self.service.register_catalog(self.tenant,catalog)
        a=self.create(registry=registry,codes=['M2','C1']); self.seed_report(a,grade=None)
        self.assertEqual(self.review(a,grade=None,disposition='insufficient_evidence').status_code,200)
        endpoint='/assessments/'+a['assessment_id']+'/finalize'
        body={'expected_revision':2,'limitation_disposition':'Retain missing values and investigate risk.'}
        self.assertEqual(self.client.post(endpoint,json=body,headers=self.auth(self.manager,'final-null')).json()['error'],'incomplete_human_review')
        self.assertEqual(self.review(a,revision=2,code='C1',grade=None,disposition='not_applicable',key='risk-0001',decision={'risk_verification':'requires_investigation'}).status_code,200)
        body['expected_revision']=3
        self.assertEqual(self.client.post(endpoint,json=body,headers=self.auth(self.manager,'final-risk')).json()['error'],'risk_verification_required')
        self.assertEqual(self.review(a,revision=3,code='C1',grade='D',disposition='assessed',key='risk-0002',decision={'risk_verification':'not_present'}).status_code,200)
        detail=self.service.detail(self.service.db.authenticate(self.employee),a['assessment_id'])
        rows={x['competency_id']:x for x in detail['rows']}
        self.assertIsNone(rows['C1']['ai']['grade']); self.assertIsNone(rows['M2']['difference_from_recommendation'])
        body['expected_revision']=4
        self.assertEqual(self.client.post(endpoint,json=body,headers=self.auth(self.manager,'final-risk-ok')).status_code,200)

    def test_evidence_exact_match_and_supplemental_quality_ack(self):
        a=self.create(); self.seed_report(a)
        mid=self.body['material_ids'][0]
        ref=self.client.get('/materials/'+mid+'/blocks',headers=self.auth(self.employee)).json()['blocks'][0]
        ref={k:ref[k] for k in ('evidence_id','quote','span_hash')}; ref['quote']='invented'
        self.assertEqual(self.review(a,decision={'evidence_refs':[ref]}).json()['error'],'invalid_evidence_reference')
        data=excel('<sheetData><row r="1"><c r="A1"><f>1+1</f><v/></c></row></sheetData>')
        s=self.upload(data=data,filename='supplement.xlsx').json()
        self.assertEqual(self.review(a,supplemental_material_ids=[s['snapshot_id']]).json()['error'],'quality_acknowledgement_required')
        acks=[s['snapshot_id']+':'+i['code'] for i in s['issues'] if i['blocking']]
        result=self.review(a,supplemental_material_ids=[s['snapshot_id']],acknowledged_quality_issues=acks)
        self.assertEqual(result.status_code,200,result.text)

    def test_input_errors_do_not_echo_secret_or_accept_role(self):
        response=self.client.post('/assessments',json={'role':'director','secret':'private-sentinel'},headers=self.auth(self.employee,'invalid-01'))
        self.assertEqual(response.status_code,422); self.assertNotIn('private-sentinel',response.text)
        self.assertEqual(self.client.get('/health',headers={'Host':'attacker.example'}).status_code,400)
        response=self.client.get('/me',headers={**self.auth(self.employee),'Origin':'https://attacker.example'})
        self.assertNotIn('access-control-allow-origin',response.headers)

    def test_whitespace_cannot_replace_human_reason_or_limitations(self):
        a=self.create(); self.seed_report(a)
        self.assertEqual(self.review(a,decision={'reason':'        '}).status_code,422)
        self.assertEqual(self.review(a).status_code,200)
        response=self.client.post('/assessments/'+a['assessment_id']+'/finalize',
            json={'expected_revision':2,'limitation_disposition':'        '},headers=self.auth(self.manager,'blank-final'))
        self.assertEqual(response.status_code,422)

    def test_local_web_assets_are_frozen_and_csp_restricts_external_content(self):
        page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertIn('MEDIC',page.text)
        self.assertIn("script-src 'self'",page.headers['content-security-policy'])
        self.assertNotIn('unsafe-inline',page.headers['content-security-policy'])
        for name in ('app.js','app.css','demo-theme.css','alignment.css','navigation.js','results.js','preview.js'):
            self.assertEqual(self.client.get('/assets/'+name).status_code,200)
        self.assertEqual(self.client.get('/assets/local-access.json').status_code,404)
        from medic.runtime import build
        self.assertTrue({'apps/web/index.html','apps/web/app.js','apps/web/app.css'}<=set(build.inventory()))

    def test_review_permission_and_material_ids_are_authoritative(self):
        a=self.create()
        for token,allowed in ((self.employee,False),(self.manager,True),(self.admin,False)):
            detail=self.client.get('/assessments/'+a['assessment_id'],headers=self.auth(token)).json()
            self.assertEqual(detail['permissions']['can_review'],allowed)
            self.assertEqual(detail['material_snapshots'],self.body['material_ids'])


class PostgreSQLApplication(ApplicationContracts):
    def database(self):
        from medic.tests.test_postgres import database
        return database()
