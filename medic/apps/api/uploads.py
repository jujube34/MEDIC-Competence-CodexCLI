"""Cross-process local upload admission; ingress quotas are not storage quotas.

Locks cover receiving and parsing. They have no lease timeout: a live parser
must never lose its slot simply because an HTTP client goes away. The OS
releases locks after process death; unsettled byte reservations stay charged.
"""
from contextlib import closing
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

from medic.apps.api.database import AppError
from medic.ingestion.contracts import ParseLimits
from medic.p0.common import digest


@dataclass(frozen=True)
class UploadPolicy:
    global_concurrency: int = 2
    user_concurrency: int = 1
    user_attempts: int = 100
    tenant_attempts: int = 1000
    user_bytes: int = 250 * 1024 * 1024
    tenant_bytes: int = 1024 * 1024 * 1024
    window_seconds: int = 86400
    receive_seconds: int = 60

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError('invalid_upload_policy')
        if not 1 <= self.user_concurrency <= self.global_concurrency <= 16:
            raise ValueError('invalid_upload_policy')
        if not ParseLimits().input_bytes <= self.user_bytes <= self.tenant_bytes:
            raise ValueError('invalid_upload_policy')
        if self.user_attempts > self.tenant_attempts or self.receive_seconds > 600:
            raise ValueError('invalid_upload_policy')


def lock_slot(path):
    """Return an owned lock handle or None on contention; never unlink lock files."""
    handle = open(path, 'a+b')
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as error:
        handle.close()
        import errno
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return None
        raise
    except BaseException:
        handle.close(); raise


class UploadAdmission:
    def __init__(self, vault, policy=None):
        self.policy = policy or UploadPolicy()
        self.root, self.database = vault.root, vault.database
        self.locks = self.root/'upload-locks'
        self.locks.mkdir(exist_ok=True)
        self.incoming = self.root/'incoming'
        self.incoming.mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS upload_policy(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS upload_attempts(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,subject TEXT NOT NULL,created REAL NOT NULL,bytes INTEGER NOT NULL,settled INTEGER NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS upload_usage ON upload_attempts(tenant,created)')
            encoded = json.dumps(asdict(self.policy), sort_keys=True)
            db.execute('INSERT OR IGNORE INTO upload_policy VALUES(1,?)', (encoded,))
            if db.execute('SELECT payload FROM upload_policy WHERE id=1').fetchone()[0] != encoded:
                raise ValueError('upload_policy_mismatch')

    def _slots(self, prefix, count):
        for index in range(count):
            handle = lock_slot(self.locks/(prefix+'-'+str(index)+'.lock'))
            if handle is not None: return handle
        raise AppError('upload_concurrency_limit', 429)

    def acquire(self, tenant, user):
        # Stable account scope, independent of token renewal, filename and employee selection.
        subject = digest([tenant, user])
        tenant = digest(tenant)
        handles = []
        try:
            handles.append(self._slots(subject, self.policy.user_concurrency))
            handles.append(self._slots('global', self.policy.global_concurrency))
            ident, instant = uuid.uuid4().hex, time.time()
            reserved = ParseLimits().input_bytes
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                rows = db.execute('SELECT subject,COUNT(*),SUM(bytes) FROM upload_attempts WHERE tenant=? AND created>? GROUP BY subject',
                                  (tenant, instant-self.policy.window_seconds)).fetchall()
                attempts, used = sum(r[1] for r in rows), sum(r[2] for r in rows)
                own = next((r[1:] for r in rows if r[0] == subject), (0, 0))
                if (attempts >= self.policy.tenant_attempts or own[0] >= self.policy.user_attempts
                        or used+reserved > self.policy.tenant_bytes or own[1]+reserved > self.policy.user_bytes):
                    raise AppError('upload_daily_quota', 429)
                db.execute('INSERT INTO upload_attempts VALUES(?,?,?,?,?,0)', (ident,tenant,subject,instant,reserved))
            return UploadPermit(self, ident, handles)
        except BaseException:
            for handle in handles: handle.close()
            raise


class UploadPermit:
    def __init__(self, admission, ident, handles):
        self.admission, self.ident, self.handles = admission, ident, handles
        self.mutex = threading.Lock()
        self.processing = self.closed = False
        self.size = 0
        self.directory = None

    def temporary_path(self, filename):
        self.directory = tempfile.TemporaryDirectory(dir=self.admission.incoming)
        return Path(self.directory.name)/filename

    def received(self, size):
        self.size = min(size, ParseLimits().input_bytes)

    def _finish(self):
        # Called with mutex held. If settlement fails, retain full durable reservation.
        try:
            with closing(sqlite3.connect(self.admission.database)) as db, db:
                db.execute('UPDATE upload_attempts SET bytes=?,settled=1 WHERE id=? AND settled=0', (self.size,self.ident))
        finally:
            try:
                if self.directory is not None: self.directory.cleanup()
            finally:
                for handle in self.handles: handle.close()
                self.closed = True

    def cancel_pending(self):
        with self.mutex:
            if not self.closed and not self.processing: self._finish()

    def process(self, function, *args):
        # Cancellation before the worker starts prevents it opening a removed temp
        # file. Cancellation after this handoff leaves ownership with the worker.
        with self.mutex:
            if self.closed: raise AppError('upload_cancelled', 409)
            self.processing = True
        try:
            return function(*args)
        finally:
            with self.mutex: self._finish()
