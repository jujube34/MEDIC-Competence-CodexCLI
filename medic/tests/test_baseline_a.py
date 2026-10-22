import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import jsonschema

from medic.evals import baseline_a as a
from medic.evals.comparison import freeze,digest
from medic.tests.test_comparison_runtime import fixture
from medic.runtime import build


def setup_inputs():
    source,data,design,limits=fixture()
    design['prompt_sha256']['single_agent']=digest(a.PROMPT)
    design['implementation_sha256']['single_agent']=a.implementation()
    return source,freeze(data,design)


def result_for(source,grade=None):
    ref={k:source['materials'][0]['spans'][0][k] for k in ('evidence_id','span_hash','quote')}
    return {'items':[{'code':code,'grade':grade,'criterion_checks':[{'criterion_id':c['criterion_id'],
        'finding':'met' if grade and c['grade']<=grade else 'not_observed','evidence_refs':[ref] if grade and c['grade']<=grade else [],'reason':'Synthetic test judgment'} for c in criteria],
        'supporting_evidence':[ref] if grade else [],'reason':'Synthetic limited evidence','limitations':['Synthetic case only']} for code,criteria in source['criteria'].items()]}


class BaselineValidation(unittest.TestCase):
    def setUp(self): self.source,self.experiment=setup_inputs()

    def test_all_scope_and_all_criteria_required(self):
        value=result_for(self.source,'B');self.assertEqual(a.check_output(self.source,value),value)
        changed=copy.deepcopy(value);changed['items'].append(changed['items'][0])
        with self.assertRaisesRegex(ValueError,'scope'): a.check_output(self.source,changed)
        changed=copy.deepcopy(value);changed['items'][0]['criterion_checks'].pop()
        with self.assertRaisesRegex(ValueError,'criterion_coverage'): a.check_output(self.source,changed)

    def test_bound_schema_requires_all_criteria_even_for_non_scoring_rows(self):
        source=copy.deepcopy(self.source)
        source['goal']['competency_scope'].append('C1')
        source['criteria']['C1']=[{**c,'criterion_id':c['criterion_id'].replace('M2.','C1.')} for c in source['criteria']['M2']]
        source['policies']['C1']={'mode':'risk_scan','cap':None,'signed_off':False}
        schema=a.output_schema(source)
        jsonschema.Draft202012Validator.check_schema(schema)
        validator=jsonschema.Draft202012Validator(schema)
        valid=result_for(source);validator.validate(valid)
        for change in ('omit_row','omit_high_criterion','foreign_code','foreign_criterion'):
            value=copy.deepcopy(valid)
            if change=='omit_row': value['items'].pop()
            elif change=='omit_high_criterion': value['items'][-1]['criterion_checks'].pop()
            elif change=='foreign_code': value['items'][-1]['code']='M99'
            else: value['items'][-1]['criterion_checks'][0]['criterion_id']='M2.A1'
            with self.subTest(change=change),self.assertRaises(jsonschema.ValidationError): validator.validate(value)

    def test_wrong_quote_id_or_hash_rejected(self):
        for field in ('quote','evidence_id','span_hash'):
            value=result_for(self.source,'B');value['items'][0]['supporting_evidence'][0][field]='fabricated'
            with self.subTest(field=field),self.assertRaises(ValueError): a.check_output(self.source,value)

    def test_non_scoring_cap_and_anchor_checks_not_repaired(self):
        for grade in ('D','C'):
            source=copy.deepcopy(self.source);source['policies']['M2']['cap']='B'
            with self.assertRaisesRegex(ValueError,'policy_violation'): a.check_output(source,result_for(source,grade))
        source=copy.deepcopy(self.source);source['policies']['M2'].update(mode='clue_only',cap=None)
        with self.assertRaisesRegex(ValueError,'policy_violation'): a.check_output(source,result_for(source,'A'))
        value=result_for(self.source,'B');value['items'][0]['criterion_checks'][0]['finding']='partial'
        with self.assertRaisesRegex(ValueError,'anchor_coverage'): a.check_output(self.source,value)

    def test_abstention_stays_null_but_met_still_requires_reference(self):
        value=result_for(self.source)
        self.assertIsNone(a.check_output(self.source,value)['items'][0]['grade'])
        value['items'][0]['criterion_checks'][0]['finding']='met'
        with self.assertRaisesRegex(ValueError,'met_without_reference'): a.check_output(self.source,value)

    def test_blank_reason_and_unknown_fields_rejected(self):
        for change in ({'reason':' '},{'extra':'unexpected'}):
            value=result_for(self.source);value['items'][0].update(change)
            with self.assertRaises(ValueError): a.check_output(self.source,value)

    def test_a_slot_identity_binds_complete_input_and_actual_implementation(self):
        sealed=a.bind(self.experiment,self.source,'synthetic-runtime',1)
        self.assertEqual(sealed['payload']['slot']['strategy'],'single_agent')
        changed=copy.deepcopy(self.source);changed['period']+='changed'
        with self.assertRaisesRegex(ValueError,'business_input'): a.bind(self.experiment,changed,'synthetic-runtime',1)
        source,data,design,_=fixture()
        with self.assertRaisesRegex(ValueError,'implementation'): a.bind(freeze(data,design),source,'synthetic-runtime',1)


class BaselineProtocol(unittest.TestCase):
    def setUp(self):
        self.source,self.experiment=setup_inputs()
        self.sealed=a.bind(self.experiment,self.source,'synthetic-runtime',1)
        self.result=result_for(self.source,'B');self.raw={'text':json.dumps(self.result)}
        self.protocol={'thread_id':'t1','turn_id':'u1','prompt_sha256':digest(a.PROMPT),
            'input_sha256':digest(self.sealed['payload']['source']),'result_sha256':digest(self.result),
            'events':[{'method':'turn/started','params':{'threadId':'t1','turn':{'id':'u1','status':'inProgress'}}},
                      {'method':'item/completed','params':{'threadId':'t1','turnId':'u1','item':{'type':'agentMessage','text':self.raw['text']}}},
                      {'method':'turn/completed','params':{'threadId':'t1','turn':{'id':'u1','status':'completed'}}}]}

    def test_complete_bound_output_is_verified(self):
        self.assertEqual(a.check_protocol(self.sealed,self.result,self.raw,self.protocol),digest(self.protocol))

    def test_changed_output_or_input_not_accepted_as_completed(self):
        for change in ('input','result','raw','message'):
            protocol=copy.deepcopy(self.protocol);raw=copy.deepcopy(self.raw)
            if change in ('input','result'): protocol[change+'_sha256']='e'*64
            elif change=='raw': raw['text']='{}'
            else: protocol['events'][1]['params']['item']['text']='{}'
            with self.subTest(change=change),self.assertRaises(ValueError): a.check_protocol(self.sealed,self.result,raw,protocol)

    def test_missing_failed_foreign_or_tool_lifecycle_rejected(self):
        for change in ('missing','failed','foreign_thread','foreign_turn','tool','duplicate'):
            protocol=copy.deepcopy(self.protocol);events=protocol['events']
            if change=='missing': events.pop()
            elif change=='failed': events[-1]['params']['turn']['status']='failed'
            elif change=='foreign_thread': events[1]['params']['threadId']='other'
            elif change=='foreign_turn': events[1]['params']['turnId']='other'
            elif change=='tool': events[1]['params']['item']['type']='commandExecution'
            else: events.append(copy.deepcopy(events[-1]))
            with self.subTest(change=change),self.assertRaises(ValueError): a.check_protocol(self.sealed,self.result,self.raw,protocol)


class BaselineBudget(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=build.ROOT/'artifacts',prefix='baseline-ledger-');self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'ledger.sqlite3'
        self.sealed={'sha256':'a'*64,'payload':{'limits':{'total_tokens':100,'requests':3,'concurrency':1,'wall_seconds':900}}}
        self.ledger=a.Ledger(self.path,self.sealed)

    def test_restart_preserves_reservation_and_deadline(self):
        self.ledger.start();request=self.ledger.reserve(30);before=self.ledger.snapshot()
        restarted=a.Ledger(self.path,self.sealed);self.assertFalse(restarted.start())
        after=restarted.snapshot();self.assertEqual(after['session']['state'],'interrupted')
        self.assertEqual(after['session']['deadline'],before['session']['deadline']);self.assertEqual(after['requests'],before['requests'])
        with self.assertRaisesRegex(ValueError,'not_active'): restarted.reserve(1)

    def test_concurrent_reservations_have_one_winner(self):
        self.ledger.start()
        def attempt(_):
            try: return self.ledger.reserve(40)
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=4) as pool: result=list(pool.map(attempt,range(4)))
        self.assertEqual(sum(r is not None for r in result),1);self.assertEqual(len(self.ledger.snapshot()['requests']),1)

    def test_unknown_and_reported_settlements_preserve_budgets(self):
        self.ledger.start();first=self.ledger.reserve(60);self.ledger.settle(first)
        with self.assertRaisesRegex(ValueError,'budget'): self.ledger.reserve(41)
        second=self.ledger.reserve(40);self.ledger.settle(second,25)
        self.ledger.settle(second,25)
        with self.assertRaisesRegex(ValueError,'conflicting_settlement'): self.ledger.settle(second,26)
        summary=a.accounting(self.ledger.snapshot(),[])
        self.assertEqual(summary['charged_or_reserved_tokens'],85);self.assertIsNone(summary['total_tokens'])

    def test_receipt_exact_usage_and_missing_receipts(self):
        self.ledger.start();ident=self.ledger.reserve(60);self.ledger.settle(ident,25)
        receipt={'reservation_id':ident,'usage_source':'reported','finished':True,'usage':{'input_tokens':20,'output_tokens':5,'total_tokens':25}}
        summary=a.accounting(self.ledger.snapshot(),[receipt,receipt]);self.assertEqual(summary['total_tokens'],25)
        self.assertIsNone(a.accounting(self.ledger.snapshot(),[])['total_tokens'])
        bad=copy.deepcopy(receipt);bad['usage']['total_tokens']=26
        with self.assertRaisesRegex(ValueError,'usage_mismatch'): a.accounting(self.ledger.snapshot(),[bad])

    def test_terminal_is_not_restarted_and_binding_not_replaced(self):
        self.ledger.start();self.ledger.finish('failed','synthetic_failure');self.assertFalse(self.ledger.start())
        with self.assertRaisesRegex(ValueError,'binding'): a.Ledger(self.path,self.sealed|{'sha256':'b'*64})
        for bad in (True,-1,0):
            with self.assertRaises(ValueError): self.ledger.reserve(bad)

    def test_failed_initialization_does_not_mock_or_retry_on_replay(self):
        source,experiment=setup_inputs()
        with patch.object(a.Config,'load',side_effect=RuntimeError('synthetic_private_failure')) as load:
            first=a.run(experiment,source,'synthetic-runtime',1,binary='unused',api_example='unused',root=Path(self.temp.name)/'runs')
            second=a.run(experiment,source,'synthetic-runtime',1,binary='unused',api_example='unused',root=Path(self.temp.name)/'runs')
        self.assertEqual(load.call_count,1);self.assertEqual(first,second)
        self.assertEqual(first['status'],'failed');self.assertEqual(first['observation']['predictions'],[])
        self.assertEqual(first['costs']['total_tokens'],0)
        self.assertNotIn('synthetic_private_failure',(Path(first['folder'])/'failure.json').read_text())


if __name__=='__main__': unittest.main()
