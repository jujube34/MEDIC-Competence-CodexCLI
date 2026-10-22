"""Deterministic transport/SQLite contention; never sends model requests."""
from contextlib import closing, contextmanager
import json
import sqlite3
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

from medic.adapters.codex.worker import wait_for_task
from medic.p0.namespace_gateway import Gateway
from medic.p0.provider import Config
from medic.runtime.budget import BudgetLedger
from medic.runtime.store import Store
from medic.runtime.service import Queue, error_details
from medic.tests.test_runtime import fixture


class ContentionContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'runtime.sqlite3')
        self.run=self.store.create_run(fixture(),limits={'requests':10,'total_tokens':100000,'concurrency':1})

    def under_writer(self, action):
        original=sqlite3.connect
        with closing(original(self.store.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            def short_timeout(*args,**kwargs):
                kwargs['timeout']=.05
                return original(*args,**kwargs)
            try:
                # Same SQLite locking behavior, shorter wait to make the failure
                # loop fast; the external writer is a real SQLite transaction.
                with patch('medic.runtime.store.sqlite3.connect',side_effect=short_timeout):
                    return action()
            finally:
                writer.rollback()

    def test_unexpired_supervisor_poll_does_not_require_writer_lock(self):
        self.assertFalse(self.under_writer(lambda:self.store.expire_run(self.run)))

    def test_snapshot_can_observe_committed_state_while_writer_is_active(self):
        self.assertEqual(self.under_writer(lambda:self.store.snapshot(self.run))['state'],'planning')

    def test_budget_and_queue_observers_do_not_require_writer_lock(self):
        queue=Queue(self.store)
        ledger=BudgetLedger(self.store,self.run)
        ledger.reserve(1234)
        self.assertEqual(self.under_writer(ledger.snapshot)['charged_or_reserved_tokens'],1234)
        self.assertEqual(self.under_writer(queue.pending),[self.run])

    def test_read_snapshot_is_consistent_and_rejects_writes(self):
        with self.store.read(self.run) as reader:
            sql='SELECT state FROM runs WHERE run_id=?'
            self.assertEqual(reader.execute(sql,(self.run,)).fetchone()[0],'planning')
            self.store.cancel(self.run)
            self.assertEqual(reader.execute(sql,(self.run,)).fetchone()[0],'planning')
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute('DELETE FROM runs')
        self.assertEqual(self.store.snapshot(self.run)['state'],'cancelled')

    def test_expiry_rechecks_cancellation_before_writing(self):
        with self.store.connect(self.run) as db:
            db.execute('UPDATE run_limits SET deadline=? WHERE run_id=?',(time.time()-1,self.run))
        original=self.store.read
        @contextmanager
        def cancel_after_observation(run):
            with original(run) as db: yield db
            self.store.cancel(run)
        with patch.object(self.store,'read',side_effect=cancel_after_observation):
            self.assertFalse(self.store.expire_run(self.run))
        snapshot=self.store.snapshot(self.run)
        self.assertEqual(snapshot['state'],'cancelled')
        self.assertFalse(any(e['kind']=='run_deadline_exhausted' for e in snapshot['events']))

    def test_sqlite_diagnostics_keep_code_without_exception_text(self):
        try:
            with self.store.read() as db:
                db.execute('SELECT * FROM private_employee_or_credential')
        except sqlite3.Error as exc:
            details=error_details(exc)
        self.assertEqual(details,{'error_type':'OperationalError','sqlite_errorcode':sqlite3.SQLITE_ERROR})
        self.assertNotIn('private_employee',json.dumps(details))

    def test_worker_completion_poll_does_not_take_writer_lock(self):
        binding=self.store.claim_next(self.run)
        class Client:
            events=[{'method':'turn/completed','params':{'threadId':'synthetic-thread','turn':{'id':'synthetic-turn','status':'completed'}}}]
        self.under_writer(lambda:wait_for_task(Client(),self.store,binding,'synthetic-thread','synthetic-turn'))

    def test_slow_durable_settlement_does_not_reject_next_completed_model_step(self):
        class SlowLedger(BudgetLedger):
            first=True
            def settle(self,ident,reported_total=None):
                if self.first:
                    self.first=False; time.sleep(2.5)
                return super().settle(ident,reported_total)
        ledger=SlowLedger(self.store,self.run)
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"total_tokens":7}}}\n\n'
        def upstream(request):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream())
        with Gateway(Config('https://example.invalid','synthetic'),httpx.MockTransport(upstream),
                     max_concurrency=1,ledger=ledger) as gateway:
            with httpx.Client(trust_env=False,timeout=8) as client:
                url=gateway.config.base_url+'/responses'; headers={'Authorization':'Bearer '+gateway.token}
                body={'model':'deepseek-v4-flash','input':'synthetic','tools':[],'stream':True}
                with client.stream('POST',url,json=body,headers=headers) as first:
                    for line in first.iter_lines():
                        if line.startswith('data:'): break
                    second=client.post(url,json=body,headers=headers)
                    self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(ledger.snapshot()['requests'],2)
        self.assertEqual(ledger.snapshot()['charged_or_reserved_tokens'],14)
        self.assertEqual(gateway.admission_rejections,[])

    def test_slow_upstream_close_finishes_before_terminal_delivery(self):
        calls=[]
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"type":"response.completed","response":{"status":"completed","usage":{"total_tokens":7}}}\n\n'
            def close(self):
                if len(calls)==1: time.sleep(2.5)
        def upstream(request):
            calls.append(request)
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream())
        with Gateway(Config('https://example.invalid','synthetic'),httpx.MockTransport(upstream),max_concurrency=1) as gateway:
            with httpx.Client(trust_env=False,timeout=8) as client:
                url=gateway.config.base_url+'/responses'; headers={'Authorization':'Bearer '+gateway.token}
                body={'model':'deepseek-v4-flash','input':'synthetic','stream':True}
                with client.stream('POST',url,json=body,headers=headers) as first:
                    for line in first.iter_lines():
                        if line.startswith('data:'): break
                    self.assertEqual(client.post(url,json=body,headers=headers).status_code,200)
        self.assertEqual(len(calls),2)
        self.assertEqual(gateway.charged_tokens,14)

    def test_failed_settlement_withholds_terminal_and_keeps_reservation(self):
        class FailingLedger(BudgetLedger):
            def settle(self,*args): raise sqlite3.OperationalError('private database location')
        ledger=FailingLedger(self.store,self.run)
        for stream in (True,False):
            with self.subTest(stream=stream):
                def upstream(request):
                    obj={'status':'completed','usage':{'total_tokens':7}}
                    if stream:
                        return httpx.Response(200,headers={'content-type':'text/event-stream'},
                            content=('data: '+json.dumps({'type':'response.completed','response':obj})+'\n\n').encode())
                    return httpx.Response(200,json=obj)
                with Gateway(Config('https://example.invalid','synthetic'),httpx.MockTransport(upstream),ledger=ledger) as gateway:
                    with httpx.Client(trust_env=False,timeout=8) as client:
                        response=client.post(gateway.config.base_url+'/responses',headers={'Authorization':'Bearer '+gateway.token},
                            json={'model':'deepseek-v4-flash','input':'synthetic','stream':stream})
                    self.assertEqual(response.status_code,200 if stream else 502)
                    self.assertNotIn('response.completed',response.text)
                    self.assertNotIn('private database',response.text)
                self.assertFalse(gateway.trace[0].get('terminal_delivered',False))
                self.assertEqual(gateway.trace[0]['accounting_error'],'OperationalError')
                self.assertGreater(gateway.charged_tokens,7)
                self.assertTrue(gateway.slots.acquire(blocking=False)); gateway.slots.release()
                snap=ledger.snapshot()
                self.assertEqual(snap['unsettled_requests'],1)
                self.assertEqual(snap['charged_or_reserved_tokens'],gateway.charged_tokens+(0 if stream else 7))
                # Settle only between independent test cases, as an explicit
                # real ledger operation; failure itself must retain the charge.
                BudgetLedger(self.store,self.run).settle(snap['ledger'][-1]['request_id'],7)
