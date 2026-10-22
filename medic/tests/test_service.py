"""Queue ownership/restart contracts; seeded state is not real model evidence."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from medic.runtime.budget import BudgetLedger
from medic.runtime.service import Queue, SupervisorLock, run_child
from medic.runtime.service_process import ProcessJob
from medic.runtime.store import Store
from medic.tests.test_runtime import fixture


class QueueContracts(unittest.TestCase):
    def database(self): return Path(self.temp.name)/'queue.sqlite3'

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(self.database()); self.queue=Queue(self.store)
        self.run=self.store.create_run(fixture()); self.addCleanup(self.store.cancel,self.run)

    def test_single_supervisor_and_release(self):
        with SupervisorLock(self.store) as first:
            first.check()
            with self.assertRaisesRegex(RuntimeError,'already_running'):
                with SupervisorLock(Store(self.store.path)): pass
        with SupervisorLock(Store(self.store.path)) as second: second.check()

    def test_restart_preserves_budget_deadline_and_limits_and_is_bounded(self):
        ledger=BudgetLedger(self.store,self.run); ledger.reserve(1200)
        original=ledger.snapshot()
        for expected in range(1,4):
            self.assertIn(self.run,self.queue.pending())
            self.assertEqual(self.queue.start(self.run),expected)
            self.assertIsNone(self.queue.start(self.run))
            self.queue.abandon_previous()
        self.assertNotIn(self.run,self.queue.pending())
        self.assertIsNone(self.queue.start(self.run))
        self.assertEqual(ledger.snapshot(),original)

    def test_live_task_deferred_then_expired_lease_can_resume(self):
        binding=self.store.claim_next(self.run)
        self.assertNotIn(self.run,self.queue.pending())
        self.assertIsNone(self.queue.start(self.run))
        with self.store.connect(self.run) as db:
            db.execute('UPDATE tasks SET lease_until=? WHERE task_id=?',(time.time()-1,binding['task_id']))
        self.assertIn(self.run,self.queue.pending())
        self.assertEqual(self.queue.start(self.run),1)
        self.store.recover(self.run)
        fresh=self.store.claim_next(self.run)
        self.assertEqual(fresh['lease_epoch'],binding['lease_epoch']+1)
        with self.assertRaises(Exception):
            with self.store.connect(self.run) as db: self.store.authorize(db,binding)

    def test_build_mismatch_is_quarantined_without_rebinding(self):
        with self.store.connect(self.run) as db:
            identity=json.loads(db.execute('SELECT identity_json FROM run_builds WHERE run_id=?',(self.run,)).fetchone()[0])
            identity['manifest_sha256']='different'
            db.execute('UPDATE run_builds SET identity_json=? WHERE run_id=?',(json.dumps(identity),self.run))
        self.assertIsNone(self.queue.start(self.run))
        self.assertNotIn(self.run,self.queue.pending())
        self.assertEqual(self.store.snapshot(self.run)['execution_build'],identity)
        with self.store.connect(self.run) as db:
            self.assertEqual(db.execute('SELECT status,error FROM service_attempts WHERE run_id=?',(self.run,)).fetchone()[0],'blocked')
            self.assertEqual(db.execute('SELECT count(*) FROM model_requests WHERE run_id=?',(self.run,)).fetchone()[0],0)

    def test_deadline_and_terminal_states_do_not_restart(self):
        with self.store.connect(self.run) as db:
            db.execute('UPDATE run_limits SET deadline=? WHERE run_id=?',(time.time()-1,self.run))
        self.assertIsNone(self.queue.start(self.run))
        self.assertEqual(self.store.snapshot(self.run)['state'],'failed')
        self.assertNotIn(self.run,self.queue.pending())
        self.store.cancel(self.run)
        self.assertIsNone(self.queue.start(self.run))

    def test_unknown_failure_blocked_and_completion_idempotent(self):
        number=self.queue.start(self.run)
        self.queue.finish(self.run,number,'blocked','runtime_process_incomplete')
        self.queue.finish(self.run,number,'completed')
        self.assertNotIn(self.run,self.queue.pending())
        events=[e for e in self.store.snapshot(self.run)['events'] if e['kind']=='service_attempt_finished']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['data']['status'],'blocked')


@unittest.skipUnless(os.name=='nt','Windows process ownership')
class ProcessOwnership(unittest.TestCase):
    def test_job_close_kills_real_interpreter_and_descendant(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/'pids.json'
            job=ProcessJob(); proc=None
            try:
                script=('import sys,os,json,subprocess,time; from pathlib import Path; '
                    'from medic.runtime.service_process import ProcessJob; '
                    'j=ProcessJob(sys.argv[1]); j.close(); '
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                    'Path(sys.argv[2]).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)')
                proc=subprocess.Popen([sys.executable,'-c',script,job.name,str(state)],creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign(proc)
                for _ in range(200):
                    if state.exists(): break
                    if proc.poll() is not None: self.fail('child_exited_before_join')
                    time.sleep(.05)
                self.assertTrue(state.exists())
                pids=json.loads(state.read_text())
                from ctypes import wintypes as w
                api=ctypes.WinDLL('kernel32',use_last_error=True)
                api.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD]; api.OpenProcess.restype=w.HANDLE
                api.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]; api.CloseHandle.argtypes=[w.HANDLE]
                handles=[api.OpenProcess(0x100000,False,pid) for pid in pids]
                self.assertTrue(all(handles))
                try:
                    job.close()
                    for handle in handles: self.assertEqual(api.WaitForSingleObject(handle,5000),0)
                finally:
                    for handle in handles:
                        if handle: api.CloseHandle(handle)
                proc.wait(timeout=10)
            finally:
                job.close()
                if proc is not None and proc.poll() is None: proc.kill(); proc.wait(timeout=5)

    def test_child_never_executes_without_parent_start(self):
        job=ProcessJob()
        try:
            child=subprocess.Popen([sys.executable,'-m','medic.runtime.service_child',job.name,'--invalid'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(child)
            out,err=child.communicate(b'',timeout=10)
            self.assertEqual(child.returncode,3,err.decode(errors='replace'))
        finally: job.close()


class PostgreSQLQueue(QueueContracts):
    def database(self):
        from medic.tests.test_postgres import database
        from urllib.parse import quote
        import psycopg
        from psycopg import sql
        import uuid
        dsn=database(); schema='service_test_'+uuid.uuid4().hex
        with psycopg.connect(dsn,autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        def cleanup():
            with psycopg.connect(dsn,autocommit=True) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        return dsn+('&' if '?' in dsn else '?')+'options='+quote('-c search_path='+schema)

    def test_connection_loss_does_not_silently_reacquire_lock(self):
        with SupervisorLock(self.store) as lock:
            pid=lock.connection.info.backend_pid
            with self.store.connect() as db:
                db.connection.execute('SELECT pg_terminate_backend(%s)',(pid,))
            with self.assertRaises(Exception): lock.check()
        with SupervisorLock(self.store) as new: new.check()
