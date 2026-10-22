import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
import hashlib
from contextlib import closing

from medic.evals.comparison import digest
from medic.evals.runtime_measurements import measure,read_database,verify_archive


def fixture():
    source={'versions':{'runtime_version':'fixture-v1'},'materials':[{'material_id':'fixture-material'}]}
    identity={'runtime_version':'fixture-v1','manifest_sha256':'a'*64}
    limits={'total_tokens':100,'requests':10,'wall_seconds':60,'concurrency':2}
    result={'task_id':'task-1','competency_id':'M2','grade':'B'}
    task={'task_id':'task-1','role':'reevaluator','material_id':'fixture-material','competency_id':'M2',
        'status':'completed','lease_epoch':1,'execution_mode':'live','thread_id':'fixture-thread','turn_id':'fixture-turn','result_hash':digest(result)}
    report={'run_id':'run-fixture','input_hash':digest(source),'execution_build':identity,
        'versions':source['versions'],'budget_limits':limits,'deadline':160.0,'final_employee_decision':False,
        'human_review_required':True,'assessments':[result],'gate':{'passed':False,'failures':['fixture-human-review']}}
    events=[{'seq':1,'task_id':'task-1','kind':'run_created','data_json':json.dumps({'input_hash':digest(source),'execution_build':identity,'budget_limits_hash':digest(limits)}),'created':100.0},
        {'seq':2,'task_id':None,'kind':'preview_published','data_json':json.dumps({'candidate_hash':digest(report),'state':'awaiting_human_review'}),'created':120.0}]
    ledger=[{'request_id':f'request-{i}','run_id':'run-fixture','estimate':20,'started':101.0+i,'active_until':140.0,
             'status':'reported','charged':charge,'settled':104.0+i} for i,charge in enumerate((7,11))]
    database={'backend':'sqlite','run':{'run_id':'run-fixture','input_json':json.dumps(source),'input_hash':digest(source),
        'state':'awaiting_human_review','mode':'live','created':100.0,'report_json':json.dumps(report)},
        'limits':{'run_id':'run-fixture','limits_json':json.dumps(limits),'deadline':160.0},
        'build':{'run_id':'run-fixture','identity_json':json.dumps(identity)},'events':events,'ledger':ledger,'tasks':[task],
        'results':[{'task_id':'task-1','lease_epoch':1,'value_json':json.dumps(result)}]}
    snapshot={'run_id':'run-fixture','source':source,'input_hash':digest(source),'execution_build':identity,'budget_limits':limits,
        'deadline':160.0,'state':'awaiting_human_review','mode':'live','report':report,
        'events':[{'seq':e['seq'],'task_id':e['task_id'],'kind':e['kind'],'data':json.loads(e['data_json'])} for e in events],
        'tasks':[task],'results':{'task-1':result}}
    gateway={'durable_budget':{'limits':limits,'deadline':160.0,'requests':2,'charged_or_reserved_tokens':18,
        'unsettled_requests':0,'ledger':ledger},'requests':[{'reservation_id':r['request_id'],'usage_source':'reported','finished':True,
            'usage':{'input_tokens':r['charged']-2,'output_tokens':2,'total_tokens':r['charged']}} for r in ledger]}
    # No aliasing between pretend files and database authority in tamper tests.
    return tuple(copy.deepcopy(v) for v in (snapshot,gateway,database))


def seed_database(db,data,placeholder='?'):
    tables={'runs':[data['run']],'run_limits':[data['limits']],'run_builds':[data['build']],
        'events':[e|{'run_id':'run-fixture'} for e in data['events']],
        'model_requests':data['ledger'],'tasks':[t|{'run_id':'run-fixture'} for t in data['tasks']],'results':data['results']}
    for name,rows in tables.items():
        columns=list(rows[0]);types=[]
        for col in columns:
            value=next((r[col] for r in rows if r[col] is not None),None)
            types.append('BIGINT' if type(value) is int else 'DOUBLE PRECISION' if type(value) is float else 'TEXT')
        db.execute('CREATE TABLE '+name+'('+','.join(c+' '+t for c,t in zip(columns,types))+')')
        for row in rows: db.execute('INSERT INTO '+name+' VALUES('+','.join([placeholder]*len(columns))+')',tuple(row[c] for c in columns))


class RuntimeMeasurements(unittest.TestCase):
    def setUp(self): self.snapshot,self.gateway,self.data=fixture()
    def measure(self): return measure(self.snapshot,self.gateway,[],self.data)

    def test_reported_usage_and_failed_gate_remain_distinct(self):
        result=self.measure()
        self.assertEqual(result['total_tokens'],18);self.assertEqual(result['elapsed_seconds'],20)
        self.assertEqual(result['stored_gate'],self.snapshot['report']['gate'])
        self.assertFalse(result['stored_gate']['passed'])
        self.assertIsNone(result['reported_predictions'][0]['anchors_supported'])
        self.assertIsNone(result['reported_predictions'][0]['evidence_valid'])
        self.assertTrue(result['comparison_admission'].startswith('not_admitted'))

    def test_missing_receipt_does_not_turn_db_charge_into_actual_usage(self):
        self.gateway['requests'].pop()
        result=self.measure()
        self.assertIsNone(result['total_tokens']);self.assertEqual(result['provider_reported_token_subtotal'],7)
        self.assertEqual(result['charged_or_reserved_tokens'],18);self.assertEqual(result['missing_receipts'],1)

    def test_prior_gateway_receipts_deduplicate_and_conflicts_reject(self):
        previous=copy.deepcopy(self.gateway['requests']);self.gateway['requests']=previous[1:]
        result=measure(self.snapshot,self.gateway,[previous],self.data)
        self.assertEqual(result['total_tokens'],18);self.assertEqual(result['receipt_verified_requests'],2)
        conflicting=copy.deepcopy(previous);conflicting[1]['finished']=False
        with self.assertRaisesRegex(ValueError,'conflicting_duplicate'): measure(self.snapshot,self.gateway,[conflicting],self.data)

    def test_estimated_and_unsettled_are_not_reported_tokens(self):
        for state in ('estimated','reserved'):
            with self.subTest(state=state):
                self.setUp();row=self.data['ledger'][1]
                row.update(status=state,charged=20 if state=='estimated' else None,settled=105.0 if state=='estimated' else None)
                self.gateway['durable_budget'].update(ledger=copy.deepcopy(self.data['ledger']),charged_or_reserved_tokens=27,unsettled_requests=int(state=='reserved'))
                self.gateway['requests']=self.gateway['requests'][:1]
                result=self.measure();self.assertIsNone(result['total_tokens']);self.assertEqual(result['charged_or_reserved_tokens'],27)
                self.assertEqual(result['provider_reported_token_subtotal'],7)

    def test_aggregate_or_ledger_tampering_rejected(self):
        for field,value in [('requests',3),('charged_or_reserved_tokens',0),('unsettled_requests',1),('deadline',161.0)]:
            self.setUp();self.gateway['durable_budget'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): self.measure()
        self.setUp();self.gateway['durable_budget']['ledger'].pop()
        with self.assertRaisesRegex(ValueError,'database_mismatch'): self.measure()

    def test_negative_boolean_nonfinite_or_inconsistent_usage_rejected(self):
        for field,value in [('total_tokens',True),('input_tokens',-1),('output_tokens',float('nan')),('total_tokens',8)]:
            self.setUp();self.gateway['requests'][0]['usage'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'invalid_reported_usage'): self.measure()
        self.setUp();self.gateway['requests'][0]['finished']=False
        with self.assertRaisesRegex(ValueError,'receipt_settlement'): self.measure()

    def test_foreign_receipt_and_changed_source_result_or_task_rejected(self):
        self.gateway['requests'][0]['reservation_id']='foreign-run'
        with self.assertRaisesRegex(ValueError,'durable_reservation'): self.measure()
        for target in ('source','task','result'):
            self.setUp()
            if target=='source': self.snapshot['source']['versions']['runtime_version']='changed'
            elif target=='task': self.snapshot['tasks'][0]['thread_id']='different'
            else: self.snapshot['results']['task-1']['grade']='C'
            with self.subTest(target=target),self.assertRaises(ValueError): self.measure()

    def test_report_cannot_substitute_other_result(self):
        self.snapshot['report']=copy.deepcopy(self.snapshot['report'])
        self.snapshot['report']['assessments'][0]['grade']='C'
        self.data['run']['report_json']=json.dumps(self.snapshot['report'])
        event={'candidate_hash':digest(self.snapshot['report']),'state':'awaiting_human_review'}
        self.data['events'][1]['data_json']=json.dumps(event);self.snapshot['events'][1]['data']=event
        with self.assertRaisesRegex(ValueError,'report_final_result'): self.measure()

    def test_cancelled_missing_time_is_unknown_not_inferred(self):
        self.data['run'].update(state='cancelled',report_json=None);self.snapshot.update(state='cancelled',report=None)
        self.data['events']=self.data['events'][:1];self.snapshot['events']=self.snapshot['events'][:1]
        result=self.measure();self.assertIsNone(result['elapsed_seconds']);self.assertEqual(result['reported_predictions'],[])
        self.assertEqual(result['total_tokens'],18)

    def test_readonly_sqlite_roundtrip_and_missing_path_not_created(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'database with space.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                seed_database(db,self.data);db.commit()
            before=path.read_bytes();data=read_database(path,'run-fixture')
            self.assertEqual(measure(self.snapshot,self.gateway,[],data)['total_tokens'],18)
            self.assertEqual(path.read_bytes(),before)
            with self.assertRaises(sqlite3.OperationalError): read_database(Path(temp)/'missing.sqlite3','run-fixture')
            self.assertFalse((Path(temp)/'missing.sqlite3').exists())

    def test_archive_members_and_bytes_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.zip';content=b'fixture'
            manifest={'runtime_version':'fixture-v1','files':{'file.py':hashlib.sha256(content).hexdigest()}}
            identity={'runtime_version':'fixture-v1','manifest_sha256':digest(manifest)}
            with zipfile.ZipFile(path,'w') as archive: archive.writestr('medic/file.py',content)
            self.assertEqual(verify_archive(identity,manifest,path),1)
            with zipfile.ZipFile(path,'w') as archive: archive.writestr('medic/file.py',b'changed')
            with self.assertRaisesRegex(ValueError,'file_mismatch'): verify_archive(identity,manifest,path)
            with zipfile.ZipFile(path,'a') as archive: archive.writestr('extra',b'other')
            with self.assertRaisesRegex(ValueError,'members_mismatch'): verify_archive(identity,manifest,path)


@unittest.skipUnless(os.environ.get('MEDIC_TEST_POSTGRES_DSN'),'explicit PostgreSQL test DSN required')
class PostgreSQLMeasurements(unittest.TestCase):
    def test_real_postgresql_read_transaction_matches_sqlite_measurement(self):
        import uuid
        import psycopg
        from psycopg.conninfo import conninfo_to_dict,make_conninfo
        from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode,quote
        dsn=os.environ['MEDIC_TEST_POSTGRES_DSN'];parts=conninfo_to_dict(dsn)
        self.assertIn(parts.get('host'),('127.0.0.1','localhost'));self.assertTrue(parts['dbname'].endswith('_test'))
        schema='measurement_test_'+uuid.uuid4().hex
        snapshot,gateway,data=fixture()
        with psycopg.connect(dsn,autocommit=True) as db:
            db.execute('CREATE SCHEMA '+schema)
            try:
                db.execute('SET search_path TO '+schema);seed_database(db,data,'%s')
                parsed=urlsplit(dsn);query=dict(parse_qsl(parsed.query));query['options']='-c search_path='+schema
                isolated=urlunsplit(parsed._replace(query=urlencode(query,quote_via=quote)))
                actual=read_database(isolated,'run-fixture')
                self.assertEqual(actual['backend'],'postgres')
                self.assertEqual(measure(snapshot,gateway,[],actual)['total_tokens'],18)
                self.assertEqual(db.execute('SELECT count(*) FROM model_requests').fetchone()[0],2)
            finally: db.execute('DROP SCHEMA '+schema+' CASCADE')


if __name__=='__main__': unittest.main()
