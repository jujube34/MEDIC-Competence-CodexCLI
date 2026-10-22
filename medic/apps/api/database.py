"""Server-owned identities, scope grants and immutable human review history."""
import json
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from medic.p0.common import digest
from medic.runtime.store import Store


class AppError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code); self.code, self.status = code, status


@dataclass(frozen=True)
class Principal:
    tenant: str
    user_id: str
    token_hash: str


SCHEMA = '''
CREATE TABLE IF NOT EXISTS app_users(tenant TEXT NOT NULL,user_id TEXT NOT NULL,employee_ref TEXT,role TEXT NOT NULL,active INTEGER NOT NULL,PRIMARY KEY(tenant,user_id));
CREATE TABLE IF NOT EXISTS app_tokens(token_hash TEXT PRIMARY KEY,tenant TEXT NOT NULL,user_id TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS app_employees(tenant TEXT NOT NULL,employee_ref TEXT NOT NULL,job_key TEXT NOT NULL,synthetic INTEGER NOT NULL,PRIMARY KEY(tenant,employee_ref));
CREATE TABLE IF NOT EXISTS app_grants(tenant TEXT NOT NULL,user_id TEXT NOT NULL,employee_ref TEXT NOT NULL,can_review INTEGER NOT NULL,PRIMARY KEY(tenant,user_id,employee_ref));
CREATE TABLE IF NOT EXISTS app_catalogs(tenant TEXT NOT NULL,registry_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(tenant,registry_id));
CREATE TABLE IF NOT EXISTS app_materials(snapshot_id TEXT PRIMARY KEY,tenant TEXT NOT NULL,employee_ref TEXT NOT NULL,period TEXT NOT NULL,created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS app_assessments(assessment_id TEXT PRIMARY KEY,tenant TEXT NOT NULL,employee_ref TEXT NOT NULL,period TEXT NOT NULL,registry_id TEXT NOT NULL,source_json TEXT NOT NULL,recommendations_json TEXT NOT NULL,run_id TEXT,revision INTEGER NOT NULL,human_status TEXT NOT NULL,final_json TEXT,created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS app_idempotency(tenant TEXT NOT NULL,user_id TEXT NOT NULL,request_key TEXT NOT NULL,body_hash TEXT NOT NULL,response_json TEXT NOT NULL,PRIMARY KEY(tenant,user_id,request_key));
CREATE TABLE IF NOT EXISTS app_decisions(assessment_id TEXT NOT NULL,revision INTEGER NOT NULL,competency_id TEXT NOT NULL,value_json TEXT NOT NULL,PRIMARY KEY(assessment_id,revision,competency_id));
CREATE TABLE IF NOT EXISTS app_events(event_id TEXT PRIMARY KEY,tenant TEXT NOT NULL,assessment_id TEXT,actor TEXT NOT NULL,kind TEXT NOT NULL,revision INTEGER,body_json TEXT NOT NULL,created DOUBLE PRECISION NOT NULL);
'''


class ApplicationDB:
    def __init__(self, database):
        self.runtime = Store(database)
        with self.runtime.connect() as db:
            if self.runtime.postgres:
                db.connection.execute('SELECT pg_advisory_xact_lock(719031428)')
            for statement in SCHEMA.split(';'):
                if statement.strip(): db.execute(statement)

    @contextmanager
    def transaction(self, tenant=None):
        scope = 'run-'+digest(['application',tenant])[:32] if tenant else None
        with self.runtime.connect(scope) as db: yield db

    @staticmethod
    def event(db, tenant, actor, kind, body, assessment=None, revision=None):
        db.execute('INSERT INTO app_events VALUES(?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex,tenant,assessment,actor,kind,revision,json.dumps(body,ensure_ascii=False),time.time()))

    def provision_user(self, tenant, user_id, *, role, employee_ref=None, token_seconds=86400):
        """Operator-only provisioning, deliberately not exposed as an HTTP route."""
        if role not in ('employee','manager','director','admin') or not 1 <= token_seconds <= 604800:
            raise ValueError('invalid_provisioning')
        token = 'medic-'+secrets.token_urlsafe(32)
        with self.transaction(tenant) as db:
            db.execute('INSERT OR IGNORE INTO app_users VALUES(?,?,?,?,?)',(tenant,user_id,employee_ref,role,1))
            row=db.execute('SELECT * FROM app_users WHERE tenant=? AND user_id=?',(tenant,user_id)).fetchone()
            if row['role']!=role or row['employee_ref']!=employee_ref or not row['active']:
                raise ValueError('existing_identity_cannot_be_redefined')
            db.execute('INSERT INTO app_tokens VALUES(?,?,?,?)',(digest(token),tenant,user_id,time.time()+token_seconds))
        return token

    def provision_employee(self, tenant, employee, job_key, *, synthetic):
        with self.transaction(tenant) as db:
            db.execute('INSERT INTO app_employees VALUES(?,?,?,?)',(tenant,employee,job_key,int(synthetic)))

    def grant(self, tenant, user_id, employee, *, can_review=False):
        with self.transaction(tenant) as db:
            if not db.execute('SELECT 1 FROM app_users WHERE tenant=? AND user_id=?',(tenant,user_id)).fetchone():
                raise ValueError('unknown_grantee')
            if not db.execute('SELECT 1 FROM app_employees WHERE tenant=? AND employee_ref=?',(tenant,employee)).fetchone():
                raise ValueError('unknown_employee')
            db.execute('INSERT INTO app_grants VALUES(?,?,?,?)',(tenant,user_id,employee,int(can_review)))

    def authenticate(self, token):
        token_hash=digest(token)
        with self.transaction() as db:
            row=db.execute('SELECT * FROM app_tokens WHERE token_hash=?',(token_hash,)).fetchone()
            if not row: raise AppError('authentication_required',401)
            principal=Principal(row['tenant'],row['user_id'],token_hash)
            self.user(db,principal)
            return principal

    @staticmethod
    def user(db, principal):
        row=db.execute('SELECT u.* FROM app_users u JOIN app_tokens t ON t.tenant=u.tenant AND t.user_id=u.user_id WHERE t.token_hash=? AND u.tenant=? AND u.user_id=? AND u.active=1 AND t.expires>?',
                       (principal.token_hash,principal.tenant,principal.user_id,time.time())).fetchone()
        if not row: raise AppError('authentication_required',401)
        return dict(row)

    def authorize(self, db, principal, employee, *, review=False):
        user=self.user(db,principal)
        target=db.execute('SELECT * FROM app_employees WHERE tenant=? AND employee_ref=?',(principal.tenant,employee)).fetchone()
        own=user['employee_ref']==employee
        grant=db.execute('SELECT * FROM app_grants WHERE tenant=? AND user_id=? AND employee_ref=?',(principal.tenant,principal.user_id,employee)).fetchone()
        if not target or not (own or grant): raise AppError('resource_not_found',404)
        if review and (own or user['role'] not in ('manager','director') or not grant or not grant['can_review']):
            raise AppError('review_not_authorized',403)
        return dict(target)

    @staticmethod
    def replay(db, principal, key, body):
        row=db.execute('SELECT body_hash,response_json FROM app_idempotency WHERE tenant=? AND user_id=? AND request_key=?',
                       (principal.tenant,principal.user_id,key)).fetchone()
        if row:
            if row['body_hash']!=digest(body): raise AppError('idempotency_conflict',409)
            return json.loads(row['response_json'])

    @staticmethod
    def remember(db, principal, key, body, response):
        db.execute('INSERT INTO app_idempotency VALUES(?,?,?,?,?)',
                   (principal.tenant,principal.user_id,key,digest(body),json.dumps(response)))
