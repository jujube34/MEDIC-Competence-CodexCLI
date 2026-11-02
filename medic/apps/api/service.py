"""Application boundary: authoritative scope, durable creation, append-only review."""
import json
import re
import time
import uuid
from pathlib import Path

from medic.apps.api.contracts import Catalog
from medic.apps.api.database import ApplicationDB, AppError
from medic.domain.contracts import AssessmentInput
from medic.ingestion.vault import Vault
from medic.p0.common import digest
from medic.runtime import build


class Service:
    def __init__(self, database, vault):
        self.db = ApplicationDB(database)
        self.runtime = self.db.runtime
        self.vault = Vault(vault)

    def register_catalog(self, tenant, catalog):
        """Operator only. Importing a customer candidate never enables execution."""
        catalog = Catalog.model_validate(catalog)
        payload = catalog.model_dump()
        # Empty optional display metadata must not change legacy catalog IDs.
        for field in ('job_profiles','competency_names'):
            if not payload[field]: payload.pop(field)
        ident = digest(payload)
        with self.db.transaction(tenant) as db:
            db.execute('INSERT OR IGNORE INTO app_catalogs VALUES(?,?,?)', (tenant,ident,json.dumps(payload,ensure_ascii=False)))
        return ident

    @staticmethod
    def key(key):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}',key):
            raise AppError('valid_idempotency_key_required')
        return key

    def employees(self, p):
        with self.db.transaction(p.tenant) as db:
            user = self.db.user(db,p)
            return [dict(r) for r in db.execute('SELECT e.* FROM app_employees e WHERE e.tenant=? AND (e.employee_ref=? OR EXISTS (SELECT 1 FROM app_grants g WHERE g.tenant=e.tenant AND g.employee_ref=e.employee_ref AND g.user_id=?)) ORDER BY employee_ref', (p.tenant,user['employee_ref'],p.user_id))]

    def catalogs(self, p):
        with self.db.transaction(p.tenant) as db:
            self.db.user(db,p)
            return [{'registry_id':r['registry_id'],**json.loads(r['payload'])} for r in db.execute('SELECT * FROM app_catalogs WHERE tenant=? ORDER BY registry_id',(p.tenant,))]

    def ingest(self, p, employee, path, period):
        if not period.strip() or len(period)>64: raise AppError('invalid_period')
        with self.db.transaction(p.tenant) as db: self.db.authorize(db,p,employee)
        snapshot=self.vault.ingest(p.tenant,employee,path,period=period,owner_claim='self-reported '+employee)
        # Parsing is outside the transaction. A revoked grant cannot publish its result.
        with self.db.transaction(p.tenant) as db:
            self.db.authorize(db,p,employee)
            db.execute('INSERT INTO app_materials VALUES(?,?,?,?,?)',(snapshot['snapshot_id'],p.tenant,employee,period,time.time()))
            self.db.event(db,p.tenant,p.user_id,'material_uploaded',{'snapshot_id':snapshot['snapshot_id'],'source_sha256':snapshot['source_sha256']})
        return self.material_summary(snapshot)

    @staticmethod
    def material_summary(s):
        from medic.runtime import lite
        if lite.enabled():
            preview={'supported':False,'derived':False,
                     'page_count':sum(u.get('kind')=='page' for u in s['extraction']['units']) if s['format']=='pdf' else None}
        else:
            from medic.ingestion.office import engine_available
            preview={'supported':s['format']=='pdf' or engine_available(),'derived':s['format']!='pdf',
                     'page_count':sum(u.get('kind')=='page' for u in s['extraction']['units']) if s['format']=='pdf' else None}
        return {**{k:s[k] for k in ('snapshot_id','original_name','period','source_sha256','format','created_at')},
                'state':s['extraction']['state'],'parser_version':s['extraction']['parser_version'],
                'preview':preview,
                'block_count':len(s['extraction']['blocks']),'issues':s['extraction']['issues']}

    def _material(self, db, p, ident, employee=None, period=None):
        r=db.execute('SELECT * FROM app_materials WHERE snapshot_id=? AND tenant=?',(ident,p.tenant)).fetchone()
        if not r: raise AppError('resource_not_found',404)
        self.db.authorize(db,p,r['employee_ref'])
        if (employee is not None and employee!=r['employee_ref']) or (period is not None and period!=r['period']):
            raise AppError('material_scope_mismatch')
        return self.vault.get(p.tenant,r['employee_ref'],ident)

    def material(self, p, ident):
        with self.db.transaction(p.tenant) as db: return self._material(db,p,ident)

    def materials(self, p, employee):
        with self.db.transaction(p.tenant) as db:
            self.db.authorize(db,p,employee)
            return [self.material_summary(self.vault.get(p.tenant,employee,r[0])) for r in db.execute('SELECT snapshot_id FROM app_materials WHERE tenant=? AND employee_ref=? ORDER BY created',(p.tenant,employee))]

    @staticmethod
    def spans(s):
        material='material-'+s['snapshot_id'].removeprefix('snapshot-')
        return [{'evidence_id':material+'-'+b['block_id'],'quote':b['quote'],
                 'span_hash':digest([s['snapshot_id'],json.dumps(b['locator'],ensure_ascii=False,sort_keys=True,separators=(',',':')),b['quote']]),
                 'locator':b['locator'],'quality_flags':b['quality_flags']} for b in s['extraction']['blocks'] if b['quote'].strip()]

    def _assessment(self, db, p, ident, review=False):
        row=db.execute('SELECT * FROM app_assessments WHERE assessment_id=? AND tenant=?',(ident,p.tenant)).fetchone()
        if not row: raise AppError('resource_not_found',404)
        self.db.authorize(db,p,row['employee_ref'],review=review)
        return dict(row)

    def create(self, p, request, key):
        self.key(key)
        body={'operation':'create','request':request.model_dump()}
        with self.db.transaction(p.tenant) as db:
            employee=self.db.authorize(db,p,request.employee_ref)
            prior=self.db.replay(db,p,key,body)
            if prior:
                row=self._assessment(db,p,prior['assessment_id'])
            else:
                catalog_row=db.execute('SELECT payload FROM app_catalogs WHERE tenant=? AND registry_id=?',(p.tenant,request.registry_id)).fetchone()
                if not catalog_row: raise AppError('resource_not_found',404)
                catalog=Catalog.model_validate_json(catalog_row[0])
                if not employee['synthetic'] or not catalog.synthetic: raise AppError('production_inputs_not_enabled',409)
                codes=request.competency_scope
                if len(set(codes))!=len(codes) or not set(codes)<=set(catalog.criteria): raise AppError('invalid_competency_scope')
                if employee['job_key'] not in catalog.recommendations: raise AppError('job_not_in_catalog')
                if len(set(request.material_ids))!=len(request.material_ids): raise AppError('duplicate_material')
                snapshots=[self._material(db,p,i,request.employee_ref,request.period) for i in request.material_ids]
                if any(s['extraction']['state']!='ready' for s in snapshots): raise AppError('material_requires_review',409)
                parsers={s['extraction']['parser_version'] for s in snapshots}
                if len(parsers)!=1: raise AppError('parser_version_mismatch')
                baseline=json.loads((build.ROOT/'p0/results/baseline.lock.json').read_text(encoding='utf-8'))
                provider=json.loads((build.ROOT/'p0/results/provider-profile.json').read_text(encoding='utf-8'))
                source=AssessmentInput.model_validate({'tenant_id':p.tenant,'employee_ref':request.employee_ref,'period':request.period,
                    'job_key':employee['job_key'],'synthetic':True,'versions':{
                        **{k:getattr(catalog,k) for k in ('competency_model_version','job_matrix_version','ai_policy_version')},
                        'prompt_bundle_version':'medic-p2-roles-v8','parser_version':next(iter(parsers)),
                        'runtime_version':build.VERSION,'codex_revision':baseline['codex_revision'],
                        'provider_profile_version':provider['provider_profile_version']},
                    'goal':{'goal_id':'application-preassessment','version':'application-goal-v1','competency_scope':codes,
                            'description':'完成固定范围的证据检查、独立反驳、二评与目标审查，保留不足和人工项。仅合成测试，不是正式员工评价。'},
                    'materials':[self.vault.material(p.tenant,request.employee_ref,i).model_dump() for i in request.material_ids],
                    'criteria':{c:[x.model_dump() for x in catalog.criteria[c]] for c in codes},
                    'policies':{c:catalog.policies[c].model_dump() for c in codes}}).model_dump()
                # Verify admission before committing an intent; recommendations stay outside agent input.
                from medic.domain.validation import verify_input
                verify_input(AssessmentInput.model_validate(source))
                build.verify_once(build.VERSION)
                ident='assessment-'+uuid.uuid4().hex
                db.execute('INSERT INTO app_assessments VALUES(?,?,?,?,?,?,?,NULL,1,?,NULL,?)',
                    (ident,p.tenant,request.employee_ref,request.period,request.registry_id,json.dumps(source,ensure_ascii=False),
                     json.dumps({c:catalog.recommendations[employee['job_key']][c] for c in codes}),'pending',time.time()))
                self.db.remember(db,p,key,body,{'assessment_id':ident})
                self.db.event(db,p.tenant,p.user_id,'assessment_requested',{'input_hash':digest(source)},ident,1)
                row=self._assessment(db,p,ident)
        self._ensure_run(p,row)
        return self.detail(p,row['assessment_id'])

    def _ensure_run(self, p, row):
        if row['run_id']: return row['run_id']
        # A crash after runtime creation is recovered with the same run and budget.
        source=json.loads(row['source_json'])
        from medic.domain.graph import required_graph
        nodes=len(required_graph(AssessmentInput.model_validate(source)))
        # A full 17-item graph cannot fit the historic 80-request smoke budget.
        # Freeze the full-scope technical budget at creation, never on resume.
        limits=({'requests':2000,'total_tokens':18000000,'concurrency':3,'wall_seconds':7200}
                if nodes>10 else None)
        run=self.runtime.create_run(source,creation_key=row['assessment_id'],limits=limits)
        with self.db.transaction(p.tenant) as db:
            current=self._assessment(db,p,row['assessment_id'])
            if current['run_id'] and current['run_id']!=run: raise AppError('run_link_conflict',409)
            if not current['run_id']:
                db.execute('UPDATE app_assessments SET run_id=? WHERE assessment_id=?',(run,row['assessment_id']))
                self.db.event(db,p.tenant,p.user_id,'runtime_linked',{'run_id':run},row['assessment_id'],1)
        return run

    def _snapshot(self, row):
        if not row['run_id']: return None
        snap=self.runtime.snapshot(row['run_id'])
        if digest(json.loads(row['source_json']))!=snap['input_hash'] or digest(snap['source'])!=snap['input_hash']:
            raise AppError('assessment_integrity_failure',409)
        return snap

    @staticmethod
    def decisions(db, ident):
        latest={}
        for r in db.execute('SELECT competency_id,value_json FROM app_decisions WHERE assessment_id=? ORDER BY revision',(ident,)):
            latest[r['competency_id']]=json.loads(r['value_json'])
        return latest

    @staticmethod
    def ai_rows(source, snap):
        values={a['competency_id']:a for a in ((snap or {}).get('report') or {}).get('assessments',[])}
        return {c:{'mode':policy['mode'],'grade':values.get(c,{}).get('grade') if policy['mode']=='suggest' else None,
                   'status':values.get(c,{}).get('status','awaiting_human_review' if policy['mode']=='human_only' else 'pending')}
                for c,policy in source['policies'].items()}

    def detail(self, p, ident):
        with self.db.transaction(p.tenant) as db: row=self._assessment(db,p,ident)
        snap=self._snapshot(row)
        with self.db.transaction(p.tenant) as db:
            row=self._assessment(db,p,ident)
            decisions=self.decisions(db,ident)
            try:
                self.db.authorize(db,p,row['employee_ref'],review=True)
                can_review=True
            except AppError as error:
                if error.status!=403: raise
                can_review=False
        source=json.loads(row['source_json']); recs=json.loads(row['recommendations_json'])
        rows=[]
        for c,ai in self.ai_rows(source,snap).items():
            human=decisions.get(c); grade=human['decision']['grade'] if human else None
            rows.append({'competency_id':c,'ai':ai,'recommendation':recs[c],'human':human,
                         'criteria':source['criteria'][c],'policy':source['policies'][c],
                         'difference_from_recommendation':None if grade is None else 'ABCD'.index(grade)-'ABCD'.index(recs[c])})
        report=snap['report'] if snap else None
        public_report={k:report[k] for k in ('gate','assessments','material_assessments','manual_items','versions','goal','human_review_required','final_employee_decision') if k in report} if report else None
        from medic.p0.common import CODES
        return {'assessment_id':ident,'employee_ref':row['employee_ref'],'period':row['period'],'run_id':row['run_id'],
                'job_key':source['job_key'],'framework_scope':list(CODES),
                'revision':row['revision'],'human_status':row['human_status'],'runtime_state':snap['state'] if snap else 'creation_pending',
                'synthetic':source['synthetic'],'final_employee_decision':False,'versions':source['versions'],'rows':rows,
                'material_snapshots':[m['snapshot_ref'] for m in source['materials'] if m.get('snapshot_ref')],
                'permissions':{'can_review':can_review},
                'ai_report':public_report,'final':json.loads(row['final_json']) if row['final_json'] else None}

    def assessments(self, p):
        with self.db.transaction(p.tenant) as db:
            user=self.db.user(db,p)
            return [dict(r) for r in db.execute('SELECT a.assessment_id,a.employee_ref,a.period,a.revision,a.human_status,a.run_id,COALESCE(r.state,\'creation_pending\') AS runtime_state FROM app_assessments a LEFT JOIN runs r ON r.run_id=a.run_id WHERE a.tenant=? AND (a.employee_ref=? OR EXISTS (SELECT 1 FROM app_grants g WHERE g.tenant=a.tenant AND g.employee_ref=a.employee_ref AND g.user_id=?)) ORDER BY a.created',(p.tenant,user['employee_ref'],p.user_id))]

    def review(self, p, ident, request, key):
        self.key(key); body={'operation':'review','assessment':ident,'request':request.model_dump()}
        with self.db.transaction(p.tenant) as db: row=self._assessment(db,p,ident,review=True)
        snap=self._snapshot(row)
        if not snap or not snap['report'] or snap['state'] not in ('ready_for_human_review','awaiting_human_review'):
            raise AppError('ai_package_not_ready',409)
        source=json.loads(row['source_json']); ai=self.ai_rows(source,snap)
        with self.db.transaction(p.tenant) as db:
            row=self._assessment(db,p,ident,review=True)
            prior=self.db.replay(db,p,key,body)
            if prior: return prior
            self.editable(row,request.expected_revision)
            codes=[d.competency_id for d in request.decisions]
            if len(set(codes))!=len(codes) or not set(codes)<=set(ai): raise AppError('invalid_competency_scope')
            spans={s['evidence_id']:s for m in source['materials'] for s in m['spans']}
            for mid in request.supplemental_material_ids:
                s=self._material(db,p,mid,row['employee_ref'],row['period'])
                if s['extraction']['state'] not in ('ready','needs_review'): raise AppError('supplemental_material_unreadable')
                required={mid+':'+i['code'] for i in s['extraction']['issues'] if i['blocking']}
                if not required<=set(request.acknowledged_quality_issues): raise AppError('quality_acknowledgement_required')
                spans.update({s['evidence_id']:s for s in self.spans(s)})
            previous=self.decisions(db,ident); revision=row['revision']+1
            for decision in request.decisions:
                c=decision.competency_id; original=ai[c]['grade']
                if decision.disposition=='confirmed' and (original is None or decision.grade!=original): raise AppError('confirmation_mismatch')
                if decision.disposition=='adjusted' and (original is None or decision.grade==original): raise AppError('adjustment_mismatch')
                if decision.disposition=='assessed' and original is not None: raise AppError('assessment_requires_no_ai_grade')
                for ref in decision.evidence_refs:
                    span=spans.get(ref.evidence_id)
                    if not span or span['span_hash']!=ref.span_hash or span['quote']!=ref.quote: raise AppError('invalid_evidence_reference')
                value={'decision':decision.model_dump(),'actor':p.user_id,'created':time.time(),'revision':revision,'ai_grade':original,
                       'previous_human_grade':previous.get(c,{}).get('decision',{}).get('grade'),
                       'supplemental_material_ids':request.supplemental_material_ids,'acknowledged_quality_issues':request.acknowledged_quality_issues}
                db.execute('INSERT INTO app_decisions VALUES(?,?,?,?)',(ident,revision,c,json.dumps(value,ensure_ascii=False)))
            db.execute('UPDATE app_assessments SET revision=?,human_status=? WHERE assessment_id=?',(revision,'in_review',ident))
            response={'assessment_id':ident,'revision':revision,'human_status':'in_review'}
            self.db.event(db,p.tenant,p.user_id,'human_review_saved',request.model_dump(),ident,revision)
            self.db.remember(db,p,key,body,response)
            return response

    @staticmethod
    def editable(row, expected):
        if row['revision']!=expected: raise AppError('revision_conflict',409)
        if row['human_status']=='finalized': raise AppError('review_already_finalized',409)

    def finalize(self, p, ident, request, key):
        self.key(key); body={'operation':'finalize','assessment':ident,'request':request.model_dump()}
        with self.db.transaction(p.tenant) as db: row=self._assessment(db,p,ident,review=True)
        snap=self._snapshot(row)
        if not snap or not snap['report']: raise AppError('ai_package_not_ready',409)
        with self.db.transaction(p.tenant) as db:
            row=self._assessment(db,p,ident,review=True)
            prior=self.db.replay(db,p,key,body)
            if prior: return prior
            self.editable(row,request.expected_revision)
            source=json.loads(row['source_json']); decisions=self.decisions(db,ident)
            if set(decisions)!=set(source['goal']['competency_scope']): raise AppError('incomplete_human_review',409)
            for c,policy in source['policies'].items():
                if policy['mode']=='risk_scan' and decisions[c]['decision']['risk_verification'] not in ('not_present','confirmed'):
                    raise AppError('risk_verification_required',409)
            revision=row['revision']+1
            final={'assessment_id':ident,'revision':revision,'synthetic':True,'final_employee_decision':False,
                   'reviewer':p.user_id,'created':time.time(),'ai_gate':snap['report']['gate'],'ai_report_hash':digest(snap['report']),
                   'human_decisions':decisions,'recommendations':json.loads(row['recommendations_json']),
                   'limitation_disposition':request.limitation_disposition}
            db.execute('UPDATE app_assessments SET revision=?,human_status=?,final_json=? WHERE assessment_id=?',
                       (revision,'finalized',json.dumps(final,ensure_ascii=False),ident))
            self.db.event(db,p.tenant,p.user_id,'human_review_finalized',final,ident,revision)
            response={'assessment_id':ident,'revision':revision,'human_status':'finalized'}
            self.db.remember(db,p,key,body,response)
            return response

    def history(self, p, ident):
        with self.db.transaction(p.tenant) as db:
            self._assessment(db,p,ident)
            return [{**{k:r[k] for k in ('event_id','actor','kind','revision','created')},'body':json.loads(r['body_json'])}
                    for r in db.execute('SELECT * FROM app_events WHERE assessment_id=? AND tenant=? ORDER BY created,event_id',(ident,p.tenant))]
