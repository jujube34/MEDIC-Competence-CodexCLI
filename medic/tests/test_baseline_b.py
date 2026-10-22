import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch,MagicMock
import jsonschema

from medic.evals import baseline_b as b
from medic.evals.comparison import freeze,digest
from medic.evals.comparison_runtime import content_identity
from medic.tests.test_comparison_runtime import fixture
from medic.tests.test_baseline_a import result_for
from medic.runtime import build


def inputs():
    source,data,design,_=fixture();criteria=copy.deepcopy(source['criteria']['M2'])
    source['goal']['competency_scope']=[c for _,codes in b.BATCHES for c in codes]
    for code in source['goal']['competency_scope']:
        source['criteria'][code]=[{**row,'criterion_id':row['criterion_id'].replace('M2.',code+'.')} for row in criteria]
        mode='risk_scan' if code.startswith('C') else 'human_only' if code=='E1' else 'clue_only' if code=='M3' else 'suggest'
        source['policies'][code]={'mode':mode,'cap':'C' if mode=='suggest' else None,'signed_off':False}
    sample=data['samples'][1];sample['assessment_content_sha256']=content_identity(source)
    sample['labels']=[{'code':code,'mode':p['mode'],'cap':p['cap'],'observable':code=='M2','grade':'B' if code=='M2' else None} for code,p in source['policies'].items()]
    data['samples'][0]['labels']=copy.deepcopy(sample['labels'])
    design['prompt_sha256']['linear_workflow']=digest(b.PROMPT);design['implementation_sha256']['linear_workflow']=b.implementation()
    return source,freeze(data,design)


def signals():
    return [{'signal':key,'hit':False,'affected_codes':[],'evidence_refs':[],'reason':'Synthetic no signal'} for key in b.SIGNALS]


def outputs(source):
    ref={key:source['materials'][0]['spans'][0][key] for key in ('evidence_id','span_hash','quote')}
    extraction={'items':[{'code':code,'has_evidence':code=='M2','evidence':[{'ref':ref,'evidence_type':'personal_output',
        'cross_person':False,'mechanism_running':False,'coverage_ratio':0.5}] if code=='M2' else [],
        'missing_required':['Synthetic missing evidence'],'confidence':'low'} for code in source['goal']['competency_scope']]}
    reverse={'probes':{key:{'present':False,'evidence_refs':[],'reason':'Synthetic not present'} for key in b.PROBES},
        'absence_all_three':True,'items':[{'code':code,'not_appeared':{key:['Synthetic missing'] for key in b.PROBES},
        'strength':'weak','source_event_ids':[],'evidence_refs':[],'reason':'Synthetic insufficient'} for code in source['goal']['competency_scope']],
        'signals':signals()}
    assessment=result_for(source)
    single=copy.deepcopy(source);single['criteria']={'M2':source['criteria']['M2']}
    assessment['items']=[result_for(single,'B')['items'][0] if r['code']=='M2' else r for r in assessment['items']]
    grading={'assessment':assessment,'redlines':[{'code':code,'pattern':pattern,'hit':False,'evidence_refs':[],'reason':'Synthetic no signal'}
        for code,p in source['policies'].items() if p['mode']=='risk_scan' for pattern in b.REDLINES]}
    qc={'verdict':'pass','signals':signals(),'findings':[],'adjustments':[]}
    return extraction,reverse,grading,qc


def fake_invoke(source,seen=None,withdraw=False):
    extraction,reverse,grading,qc=outputs(source)
    if withdraw:
        qc.update(verdict='revise',adjustments=[{'code':'M2','from_grade':'B','to_grade':None,'reason':'Synthetic QC withdraws unsupported attribution','evidence_refs':[]}])
    def invoke(key,request):
        if seen is not None: seen[key]=copy.deepcopy(request)
        if key.startswith('s2-'):
            codes=request['payload']['goal']['competency_scope']
            return {'items':[copy.deepcopy(row) for row in extraction['items'] if row['code'] in codes]}
        return copy.deepcopy({'s3':reverse,'s4':grading,'s5':qc,'s6':{'summary':'Synthetic coverage and gaps; requires human review.'}}[key])
    return invoke


class WorkflowContract(unittest.TestCase):
    def setUp(self): self.source,self.experiment=inputs();self.evidence,self.reverse,self.grading,self.qc=outputs(self.source)

    def test_six_steps_three_batches_and_qc_receives_no_step2_history(self):
        seen={};result=b.workflow(self.source,fake_invoke(self.source,seen))
        self.assertEqual(set(seen),{'s2-1','s2-2','s2-3','s3','s4','s5','s6'})
        self.assertEqual(len(result['assessment']['items']),17)
        self.assertEqual(len(result['preprocess']['anchors']),4)
        self.assertFalse(result['preprocess']['truncated']);self.assertNotIn('step2',seen['s5']['payload'])
        self.assertIn('step3',seen['s5']['payload']);self.assertIn('step4',seen['s5']['payload'])
        for key,request in seen.items():
            self.assertNotIn('targets',request['payload'])
            jsonschema.Draft202012Validator.check_schema(request['schema'])
            jsonschema.Draft202012Validator(request['schema']).validate(fake_invoke(self.source)(key,request))

    def test_qc_null_adjustment_survives_aggregation(self):
        result=b.workflow(self.source,fake_invoke(self.source,withdraw=True))
        row=next(r for r in result['assessment']['items'] if r['code']=='M2')
        self.assertIsNone(row['grade']);self.assertEqual(row['supporting_evidence'],[])
        self.assertIn('QC:',row['reason']);self.assertEqual(next(r for r in result['grading']['assessment']['items'] if r['code']=='M2')['grade'],'B')

    def test_missing_or_duplicate_extraction_rows_rejected(self):
        for mode in ('missing','duplicate'):
            changed=copy.deepcopy(self.evidence)
            if mode=='missing': changed['items'].pop()
            else: changed['items'][-1]=copy.deepcopy(changed['items'][0])
            with self.assertRaisesRegex(ValueError,'scope'): b.check_extraction(self.source,changed,self.source['goal']['competency_scope'])

    def test_requested_schema_binds_competency_codes_not_employee_or_criterion_ids(self):
        codes=list(b.BATCHES[1][1]);value={'items':[copy.deepcopy(r) for r in self.evidence['items'] if r['code'] in codes]}
        schema=b.step_schema('evidence',self.source,codes);validator=jsonschema.Draft202012Validator(schema)
        validator.validate(value)
        for wrong in ('E3.A1','SYN-A17','M1'):
            changed=copy.deepcopy(value);changed['items'][0]['code']=wrong
            with self.subTest(wrong=wrong),self.assertRaises(jsonschema.ValidationError): validator.validate(changed)
        changed=copy.deepcopy(value);changed['items'][-1]=copy.deepcopy(changed['items'][0])
        with self.assertRaises(jsonschema.ValidationError): validator.validate(changed)

    def test_citation_choices_cover_long_original_without_repairing_quotes(self):
        source=copy.deepcopy(self.source);span=source['materials'][0]['spans'][0]
        span['quote']='前缀“原文”'+''.join(str(i%10) for i in range(410))+'结尾'
        choices=[r for r in b.citation_choices(source) if r['evidence_id']==span['evidence_id']]
        self.assertGreater(len(choices),1)
        self.assertTrue(all(0<len(r['quote'])<=200 and r['quote'] in span['quote'] and r['span_hash']==span['span_hash'] for r in choices))
        self.assertTrue(choices[0]['quote'].startswith('前缀“原文”'));self.assertTrue(choices[-1]['quote'].endswith('结尾'))
        combined=choices[0]['quote']
        for row in choices[1:]: combined+=row['quote'][20:]
        self.assertEqual(combined,span['quote'])

    def test_extraction_is_evidence_only_and_refs_are_exact(self):
        for mode in ('grade','quote','flag','empty_missing'):
            value=copy.deepcopy(self.evidence);row=next(r for r in value['items'] if r['code']=='M2')
            if mode=='grade': row['grade']='B'
            elif mode=='quote': row['evidence'][0]['ref']['quote']='invented'
            elif mode=='flag': row['has_evidence']=False
            else: value['items'][0]['missing_required']=[]
            with self.subTest(mode=mode),self.assertRaises(ValueError): b.check_extraction(self.source,value,self.source['goal']['competency_scope'])

    def test_probe_coverage_boolean_and_source_events_checked(self):
        for mode in ('missing','summary','present','event'):
            value=copy.deepcopy(self.reverse)
            if mode=='missing': value['probes'].pop('rework')
            elif mode=='summary': value['absence_all_three']=False
            elif mode=='present': value['probes']['rework']['present']=True;value['absence_all_three']=False
            else: value['items'][0]['source_event_ids']=['unknown']
            with self.subTest(mode=mode),self.assertRaises(ValueError): b.check_reverse(self.source,value)

    def test_reverse_schema_requires_references_for_medium_and_strong(self):
        validator=jsonschema.Draft202012Validator(b.step_schema('reverse_check',self.source))
        ref=b.citation_choices(self.source)[0]
        for index in range(len(self.reverse['items'])):
            for strength in ('medium','strong'):
                value=copy.deepcopy(self.reverse);value['items'][index]['strength']=strength
                with self.subTest(index=index,strength=strength):
                    with self.assertRaisesRegex(ValueError,'strength_without_evidence'): b.check_reverse(self.source,value)
                    with self.assertRaises(jsonschema.ValidationError): validator.validate(value)
                    value['items'][index]['evidence_refs']=[ref]
                    validator.validate(value);b.check_reverse(self.source,value)
        validator.validate(self.reverse)

    def test_six_signals_require_unique_ids_scope_and_hit_evidence(self):
        for mode in ('duplicate','scope','reference','unhit'):
            value=copy.deepcopy(self.reverse);signal=value['signals'][0]
            if mode=='duplicate': value['signals'][1]=copy.deepcopy(signal)
            elif mode=='scope': signal['affected_codes']=['M99']
            elif mode=='reference': signal.update(hit=True,affected_codes=['M2'])
            else: signal['affected_codes']=['M2']
            with self.subTest(mode=mode),self.assertRaises(ValueError): b.check_reverse(self.source,value)

    def test_repeated_same_signal_does_not_count_twice(self):
        hit=signals();hit[0].update(hit=True,affected_codes=['M2'])
        self.assertEqual(b.manual_codes(hit,hit),set())
        other=signals();other[1].update(hit=True,affected_codes=['M2'])
        self.assertEqual(b.manual_codes(hit,other),{'M2'})

    def test_two_signals_require_manual_not_silent_grade_clamp(self):
        reverse=copy.deepcopy(self.reverse)
        for signal in reverse['signals'][:2]: signal.update(hit=True,affected_codes=['M2'])
        with self.assertRaisesRegex(ValueError,'signals_require_manual'): b.check_grading(self.source,self.grading,reverse)
        self.assertEqual(next(r for r in self.grading['assessment']['items'] if r['code']=='M2')['grade'],'B')

    def test_all_risk_patterns_and_policy_rules_required(self):
        changed=copy.deepcopy(self.grading);changed['redlines'].pop()
        with self.assertRaisesRegex(ValueError,'redline_coverage'): b.check_grading(self.source,changed,self.reverse)
        changed=copy.deepcopy(self.grading);changed['redlines'][0]['hit']=True
        with self.assertRaisesRegex(ValueError,'redline_without_evidence'): b.check_grading(self.source,changed,self.reverse)
        changed=copy.deepcopy(self.grading);next(r for r in changed['assessment']['items'] if r['code']=='C1')['grade']='A'
        with self.assertRaisesRegex(ValueError,'policy'): b.check_grading(self.source,changed,self.reverse)

    def test_qc_rejects_stale_upgrade_and_duplicate_adjustments(self):
        for mode in ('stale','upgrade','duplicate','null_to_grade'):
            qc=copy.deepcopy(self.qc);adj={'code':'M2','from_grade':'B','to_grade':'A','reason':'Synthetic downgrade','evidence_refs':[]}
            if mode=='stale': adj['from_grade']='C'
            elif mode=='upgrade': adj['to_grade']='C'
            elif mode=='null_to_grade': adj.update(code='M1',from_grade=None,to_grade='A')
            qc.update(verdict='revise',adjustments=[adj,copy.deepcopy(adj)] if mode=='duplicate' else [adj])
            with self.subTest(mode=mode),self.assertRaises(ValueError): b.apply_qc(self.source,self.grading,self.reverse,qc)

    def test_qc_block_and_unresolved_findings_cannot_keep_grade(self):
        for mode in ('block','pass_changes','revise_empty','manual_finding'):
            qc=copy.deepcopy(self.qc)
            if mode=='block': qc['verdict']='block'
            elif mode=='revise_empty': qc['verdict']='revise'
            else:
                qc['findings']=[{'check':'optimistic','codes':['M2'],'detail':'Synthetic finding','action':'downgrade_to_manual'}]
                if mode=='manual_finding': qc['verdict']='revise'
            with self.subTest(mode=mode),self.assertRaises(ValueError): b.apply_qc(self.source,self.grading,self.reverse,qc)

    def test_qc_new_signals_need_explicit_withdrawal(self):
        qc=copy.deepcopy(self.qc);ref=self.evidence['items'][1]['evidence'][0]['ref']
        for signal in qc['signals'][:2]: signal.update(hit=True,affected_codes=['M2'],evidence_refs=[ref])
        with self.assertRaisesRegex(ValueError,'qc_unresolved_grade'): b.apply_qc(self.source,self.grading,self.reverse,qc)
        qc.update(verdict='block',adjustments=[{'code':'M2','from_grade':'B','to_grade':None,'reason':'Two risk signals','evidence_refs':[ref]}])
        _,result=b.apply_qc(self.source,self.grading,self.reverse,qc)
        self.assertTrue(all(row['grade'] is None for row in result['items']))

    def test_failure_stops_dependent_stages_and_no_mock_summary(self):
        seen={};original=fake_invoke(self.source,seen)
        def invoke(key,request):
            value=original(key,request)
            if key=='s3': value['items'].pop()
            return value
        with self.assertRaises(ValueError): b.workflow(self.source,invoke)
        self.assertNotIn('s4',seen);self.assertNotIn('s6',seen)

    def test_three_extraction_batches_actually_overlap(self):
        barrier=threading.Barrier(3);threads=set();fake=fake_invoke(self.source)
        def invoke(key,request):
            if key.startswith('s2-'):
                threads.add(threading.get_ident());barrier.wait(timeout=5)
            return fake(key,request)
        b.workflow(self.source,invoke);self.assertEqual(len(threads),3)

    def test_summary_blank_oversize_or_score_field_rejected(self):
        for summary in ({'summary':' '},{'summary':'x'*301},{'summary':'Coverage','grade':'A'}):
            with self.assertRaises(ValueError): b.checked(b.Summary,summary,self.source)


class BindingAndProtocol(unittest.TestCase):
    def setUp(self):
        self.source,self.experiment=inputs()
        self.temp=tempfile.TemporaryDirectory(dir=build.ROOT/'artifacts',prefix='b-check-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_complete_source_and_real_implementation_bound(self):
        bound=b.bind(self.experiment,self.source,'synthetic-runtime',1)
        self.assertEqual(bound['payload']['slot']['strategy'],'linear_workflow')
        self.assertEqual(bound['payload']['limits']['concurrency'],3)
        for mode in ('input','implementation','repeat'):
            source=copy.deepcopy(self.source);experiment=copy.deepcopy(self.experiment);repeat=1
            if mode=='input': source['period']='changed'
            elif mode=='implementation':
                design=experiment['payload']['design'];design['implementation_sha256']['linear_workflow']='f'*64
                experiment=freeze(experiment['payload']['dataset'],design)
            else: repeat=3
            with self.subTest(mode=mode),self.assertRaises(ValueError): b.bind(experiment,source,'synthetic-runtime',repeat)

    def test_failed_initialization_replay_preserves_budget_without_model(self):
        with patch.object(b.Config,'load',side_effect=RuntimeError('private details')) as load:
            first=b.run(self.experiment,self.source,'synthetic-runtime',1,binary='unused',api_example='unused',root=self.root/'runs')
            second=b.run(self.experiment,self.source,'synthetic-runtime',1,binary='unused',api_example='unused',root=self.root/'runs')
        self.assertEqual(load.call_count,1);self.assertEqual(first,second)
        self.assertEqual(first['status'],'failed');self.assertEqual(first['observation']['predictions'],[])
        self.assertEqual(first['costs']['total_tokens'],0)
        self.assertNotIn('private details',(Path(first['folder'])/'failure.json').read_text())

    def test_real_call_builder_exposes_same_schema_in_prompt_and_protocol(self):
        from types import SimpleNamespace
        from medic.p0.provider import Config
        request=b.request('aggregate',{'items':[{'code':'M2','grade':None}]},b.Summary.model_json_schema())
        client=MagicMock();client.request.side_effect=[{}, {'thread':{'id':'t'}}, {'turn':{'id':'u'}}]
        client.events=[];client.trace=[]
        client.final_text.return_value=json.dumps({'summary':'Synthetic coverage'})
        provider=Config('https://example.invalid','synthetic-only')
        ledger=MagicMock();ledger.snapshot.return_value={'session':{'deadline':b.time.time()+120}}
        with patch.object(b,'Client',return_value=client),patch.object(b,'ProcessJob'):
            b.call_model(self.root/'wire',request,provider,SimpleNamespace(config=provider),ledger,'unused')
        turn=client.request.call_args_list[2].args[1]
        prompt=turn['input'][0]['text']
        self.assertIn('OUTPUT_SCHEMA_JSON:\n',prompt)
        schema_part,data_part=prompt.split('OUTPUT_SCHEMA_JSON:\n',1)[1].split('\nINPUT_DATA_JSON:\n',1)
        self.assertEqual(json.loads(schema_part),turn['outputSchema'])
        self.assertEqual(json.loads(data_part),request['payload'])

    def record(self,key,request,value,thread):
        folder=self.root/key;text=json.dumps(value)
        attempt={'thread_id':thread,'turn_id':'turn-'+thread,'request_sha256':digest(request),'trace':[],
            'events':[{'method':'turn/started','params':{'threadId':thread,'turn':{'id':'turn-'+thread,'status':'inProgress'}}},
                {'method':'item/completed','params':{'threadId':thread,'turnId':'turn-'+thread,'item':{'type':'agentMessage','text':text}}},
                {'method':'turn/completed','params':{'threadId':thread,'turn':{'id':'turn-'+thread,'status':'completed'}}}]}
        for name,content in [('request',request),('result',value),('raw-final',{'text':text}),('attempt',attempt)]: b.a._save(folder/(name+'.json'),content)
        return value

    def test_full_workflow_replay_checks_every_stage_and_independent_qc(self):
        fake=fake_invoke(self.source,withdraw=True)
        result=b.workflow(self.source,lambda key,request:self.record(key,request,fake(key,request),key))
        b.a._save(self.root/'result.json',result)
        replay,records=b.audit_workflow(self.root,self.source)
        self.assertEqual(replay,result);self.assertEqual(len(records),7)
        path=self.root/'s5/attempt.json';attempt=json.loads(path.read_text());attempt['thread_id']='s4'
        path.write_text(json.dumps(attempt))
        with self.assertRaisesRegex(ValueError,'foreign_thread'): b.audit_workflow(self.root,self.source)

    def test_protocol_tampering_and_tool_use_rejected(self):
        request=b.request('aggregate',{},b.Summary.model_json_schema());value={'summary':'Synthetic summary'}
        self.record('s6',request,value,'t');path=self.root/'s6/attempt.json';original=json.loads(path.read_text())
        for change in ('failed','tool','output','request','duplicate'):
            attempt=copy.deepcopy(original)
            if change=='failed': attempt['events'][-1]['params']['turn']['status']='failed'
            elif change=='tool': attempt['events'][1]['params']['item']['type']='commandExecution'
            elif change=='output': attempt['events'][1]['params']['item']['text']='{}'
            elif change=='request': attempt['request_sha256']='e'*64
            else: attempt['events'].append(copy.deepcopy(attempt['events'][-1]))
            path.write_text(json.dumps(attempt))
            with self.subTest(change=change),self.assertRaises(ValueError): b.audit_call(self.root/'s6',request)

    def test_qc_cannot_reuse_grade_thread_even_with_consistent_events(self):
        fake=fake_invoke(self.source)
        result=b.workflow(self.source,lambda key,request:self.record(key,request,fake(key,request),'s4' if key=='s5' else key))
        b.a._save(self.root/'result.json',result)
        with self.assertRaisesRegex(ValueError,'reused_thread'): b.audit_workflow(self.root,self.source)

    def test_expired_shared_deadline_does_not_reset(self):
        sealed=b.bind(self.experiment,self.source,'synthetic-runtime',1)
        with patch.object(b.a.time,'time',return_value=100): ledger=b.a.Ledger(self.root/'ledger.sqlite3',sealed)
        with patch.object(b.time,'time',return_value=1001):
            with self.assertRaisesRegex(ValueError,'deadline'): b.remaining(ledger,400)
        self.assertEqual(ledger.snapshot()['session']['deadline'],1000)

    def test_longer_request_window_is_bound_and_cannot_extend_run_deadline(self):
        sealed=b.bind(self.experiment,self.source,'synthetic-runtime',1)
        limits=sealed['payload']['limits']
        self.assertEqual(limits['request_timeout_seconds'],300)
        self.assertEqual(limits['wall_seconds'],900)
        with patch.object(b.a.time,'time',return_value=100):
            ledger=b.a.Ledger(self.root/'ledger.sqlite3',sealed);ledger.start()
        with patch.object(b.a.time,'time',return_value=900):
            ledger.reserve(100,limits['request_timeout_seconds']+45)
            self.assertEqual(b.remaining(ledger,400),100)
        state=ledger.snapshot()
        self.assertEqual(state['session']['deadline'],1000)
        self.assertEqual(state['requests'][0]['active_until'],1000)


if __name__=='__main__': unittest.main()
