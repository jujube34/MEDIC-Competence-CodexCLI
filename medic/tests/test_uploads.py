"""Upload admission under contention, process death, cancellation and HTTP errors."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from medic.apps.api.app import create_app
from medic.apps.api.database import AppError
from medic.apps.api.uploads import UploadAdmission, UploadPolicy
from medic.ingestion.contracts import ParseLimits
from medic.ingestion.vault import Vault


class AdmissionContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.vault=Vault(Path(self.temp.name)/'vault')

    def usage(self):
        with closing(sqlite3.connect(self.vault.database)) as db:
            return db.execute('SELECT bytes,settled FROM upload_attempts ORDER BY created').fetchall()

    def test_multiple_instances_share_global_and_stable_account_slots(self):
        a,b=UploadAdmission(self.vault),UploadAdmission(self.vault)
        first=a.acquire('T','U');self.addCleanup(first.cancel_pending)
        with self.assertRaisesRegex(AppError,'concurrency'): b.acquire('T','U')
        second=b.acquire('T','V');self.addCleanup(second.cancel_pending)
        with self.assertRaisesRegex(AppError,'concurrency'): a.acquire('OTHER','U')
        self.assertEqual(len(self.usage()),2)
        first.cancel_pending()
        third=b.acquire('OTHER','U');third.cancel_pending()

    def test_attempt_quota_survives_restart_and_does_not_charge_other_tenant(self):
        policy=replace(UploadPolicy(),user_attempts=1,tenant_attempts=1)
        a=UploadAdmission(self.vault,policy)
        a.acquire('T','U').cancel_pending()
        restarted=UploadAdmission(self.vault,policy)
        with self.assertRaisesRegex(AppError,'quota'): restarted.acquire('T','U')
        with self.assertRaisesRegex(AppError,'quota'): restarted.acquire('T','V')
        restarted.acquire('OTHER','U').cancel_pending()

    def test_full_file_reservation_then_actual_byte_settlement(self):
        size=ParseLimits().input_bytes
        a=UploadAdmission(self.vault,replace(UploadPolicy(),user_bytes=size))
        p=a.acquire('T','U');self.assertEqual(self.usage(),[(size,0)])
        p.received(10);p.process(lambda:None)
        self.assertEqual(self.usage(),[(10,1)])
        with self.assertRaisesRegex(AppError,'quota'): a.acquire('T','U')
        # Window expiry only; no reset on restart or account-token renewal.
        with closing(sqlite3.connect(self.vault.database)) as db,db:
            db.execute('UPDATE upload_attempts SET created=?',(time.time()-86401,))
        a.acquire('T','U').cancel_pending()

    def test_conflicting_policy_is_rejected(self):
        UploadAdmission(self.vault)
        with self.assertRaisesRegex(ValueError,'policy_mismatch'):
            UploadAdmission(self.vault,replace(UploadPolicy(),global_concurrency=3))
        for changes in ({'global_concurrency':0},{'user_attempts':True},{'user_bytes':1}):
            with self.assertRaises(ValueError): UploadPolicy(**changes)

    def test_cancellation_before_worker_start_prevents_file_use(self):
        a=UploadAdmission(self.vault);p=a.acquire('T','U')
        path=p.temporary_path('sample.docx');path.write_bytes(b'x');p.received(1)
        p.cancel_pending();self.assertFalse(path.exists())
        with self.assertRaisesRegex(AppError,'cancelled'): p.process(path.read_bytes)
        a.acquire('T','U').cancel_pending()

    def test_failed_parser_and_settlement_keep_conservative_accounting(self):
        a=UploadAdmission(self.vault);p=a.acquire('T','U');p.received(12)
        def fail():raise ValueError('parser_failure')
        with self.assertRaisesRegex(ValueError,'parser_failure'):p.process(fail)
        self.assertEqual(self.usage(),[(12,1)])
        p=a.acquire('T','U')
        with patch('medic.apps.api.uploads.sqlite3.connect',side_effect=sqlite3.OperationalError('fixture')):
            with self.assertRaises(sqlite3.OperationalError):p.cancel_pending()
        self.assertTrue(p.closed)
        self.assertEqual(self.usage()[-1],(ParseLimits().input_bytes,0))
        a.acquire('T','U').cancel_pending()

    def test_real_process_death_releases_slot_but_not_byte_reservation(self):
        a=UploadAdmission(self.vault)
        marker=Path(self.temp.name)/'ready'
        from medic.runtime.service_process import ProcessJob
        job=ProcessJob() if os.name=='nt' else None
        code="""import sys,time
from pathlib import Path
from medic.ingestion.vault import Vault
from medic.apps.api.uploads import UploadAdmission
if len(sys.argv)>3:
 from medic.runtime.service_process import ProcessJob
 job=ProcessJob(sys.argv[3]);job.close()
a=UploadAdmission(Vault(sys.argv[1]));p=a.acquire('T','U')
Path(sys.argv[2]).write_text('ready');time.sleep(120)
"""
        child=subprocess.Popen([sys.executable,'-c',code,str(self.vault.root),str(marker),*([job.name] if job else [])],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+15
            while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            self.assertTrue(marker.exists());self.assertIsNone(child.poll())
            with self.assertRaisesRegex(AppError,'concurrency'):a.acquire('T','U')
        finally:
            if job:job.close()
            elif child.poll() is None:child.kill()
            child.wait(timeout=10)
        self.assertEqual(self.usage(),[(ParseLimits().input_bytes,0)])
        a.acquire('T','U').cancel_pending()


class UploadHTTPContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.app=create_app(root/'app.sqlite3',root/'vault',upload_policy=replace(UploadPolicy(),receive_seconds=1))
        db=self.app.state.service.db;db.provision_employee('T','E','job',synthetic=True)
        token=db.provision_user('T','U',role='employee',employee_ref='E')
        self.headers={'Authorization':'Bearer '+token}
        self.url='/employees/E/materials?filename=sample.docx&period=Q4'
        self.client=TestClient(self.app,raise_server_exceptions=False);self.addCleanup(self.client.close)

    def test_authorization_precedes_reservation_and_empty_upload_cleans_up(self):
        a=self.app.state.uploads
        self.assertEqual(self.client.post(self.url,content=b'x').status_code,401)
        self.assertEqual(self.client.post(self.url.replace('/E/','/OTHER/'),headers=self.headers,content=b'x').status_code,404)
        with closing(sqlite3.connect(a.database)) as db:self.assertEqual(db.execute('select count(*) from upload_attempts').fetchone()[0],0)
        self.assertEqual(self.client.post(self.url,headers=self.headers,content=b'').status_code,400)
        self.assertEqual(list(a.incoming.iterdir()),[])
        a.acquire('T','U').cancel_pending()

    def test_size_and_declared_length_rejections_release_slots(self):
        a=self.app.state.uploads
        for body,length,status in [(b'x','2',400),(b'x','a',413),(b'x','9'*500,413)]:
            response=self.client.post(self.url,headers={**self.headers,'Content-Length':length},content=body)
            self.assertEqual(response.status_code,status,response.text)
        self.assertEqual(list(a.incoming.iterdir()),[])
        a.acquire('T','U').cancel_pending()

    def test_http_concurrency_and_quota_statuses(self):
        a=self.app.state.uploads;p=a.acquire('T','U')
        try:self.assertEqual(self.client.post(self.url,headers=self.headers,content=b'x').json()['error'],'upload_concurrency_limit')
        finally:p.cancel_pending()
        with closing(sqlite3.connect(a.database)) as db,db:
            db.execute('UPDATE upload_attempts SET bytes=?',(a.policy.user_bytes,))
        response=self.client.post(self.url,headers=self.headers,content=b'x')
        self.assertEqual(response.status_code,429);self.assertEqual(response.json()['error'],'upload_daily_quota')

    def test_slow_receipt_times_out_and_releases_slot(self):
        async def scenario():
            async def body():
                yield b'x'
                await asyncio.sleep(5)
                yield b'y'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver') as client:
                response=await client.post(self.url,headers=self.headers,content=body())
                self.assertEqual(response.status_code,408,response.text)
        asyncio.run(scenario())
        self.assertEqual(list(self.app.state.uploads.incoming.iterdir()),[])
        self.app.state.uploads.acquire('T','U').cancel_pending()

    def test_cancelled_http_request_does_not_release_running_parser(self):
        started,release,finished=threading.Event(),threading.Event(),threading.Event()
        a=self.app.state.uploads
        def ingest(p,employee,path,period):
            started.set()
            try:
                if not release.wait(10):raise TimeoutError('test_release_timeout')
                self.assertEqual(path.read_bytes(),b'x')
                return {'state':'ready'}
            finally:finished.set()
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver') as client:
                task=asyncio.create_task(client.post(self.url,headers=self.headers,content=b'x'))
                try:
                    for _ in range(200):
                        if started.is_set():break
                        await asyncio.sleep(.01)
                    self.assertTrue(started.is_set());task.cancel()
                    await asyncio.sleep(.05)
                    with self.assertRaisesRegex(AppError,'concurrency'):a.acquire('T','U')
                    self.assertEqual(len(list(a.incoming.iterdir())),1)
                finally:
                    release.set()
                    try:await task
                    except asyncio.CancelledError:pass
        with patch.object(self.app.state.service,'ingest',side_effect=ingest):asyncio.run(scenario())
        self.assertTrue(finished.wait(5))
        # Worker finishes settlement after returning from ingest.
        deadline=time.monotonic()+5
        while list(a.incoming.iterdir()) and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(list(a.incoming.iterdir()),[])
        a.acquire('T','U').cancel_pending()


if __name__=='__main__':unittest.main()
