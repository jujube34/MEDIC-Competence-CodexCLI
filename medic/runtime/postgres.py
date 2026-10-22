"""PostgreSQL adapter for the runtime's small, parameterized SQL vocabulary.

Short transactions serialize within each run using transaction-scoped advisory
locks, keeping graph/lease/outbox/budget invariants atomic. Different runs have
different locks. Model execution never holds a database transaction open.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


class Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return (Row(row) for row in self.cursor)


def dialect(sql):
    """Translate only application-owned SQL. Values always remain bound params."""
    sql = sql.strip().rstrip(";")
    ignore = sql.startswith("INSERT OR IGNORE INTO ")
    if ignore:
        sql = sql.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1)
        sql += " ON CONFLICT DO NOTHING"
    sql = sql.replace("ORDER BY rowid", "ORDER BY created_seq")
    sql = sql.replace("json_extract(data_json,'$.lease_epoch')", "(data_json::jsonb->>'lease_epoch')::integer")
    return sql.replace("?", "%s")


class Connection:
    def __init__(self, connection):
        self.connection = connection
        self.scope = None

    def execute(self, sql, params=()):
        return Cursor(self.connection.execute(dialect(sql), params))

    def lock_run(self, scope):
        if not re.fullmatch(r"run-[0-9a-f]{32}", scope):
            raise ValueError("invalid_transaction_scope")
        if self.scope != scope:
            if self.scope is not None:
                raise ValueError("cross_run_transaction_not_allowed")
            lock = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
            self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            self.scope = scope


@contextmanager
def connect(dsn, run=None, task_id=None):
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute("SET LOCAL statement_timeout='15s'")
        connection.execute("SET LOCAL lock_timeout='10s'")
        db = Connection(connection)
        if task_id is not None:
            # task.run_id is immutable. Resolve its scope before reading status,
            # then lock the run before the caller reads any mutable task state.
            row = connection.execute("SELECT run_id FROM tasks WHERE task_id=%s", (task_id,)).fetchone()
            run = row["run_id"] if row else run
        if run is not None:
            db.lock_run(run)
        yield db


def initialize(dsn):
    with connect(dsn) as db:
        db.connection.execute("SELECT pg_advisory_xact_lock(719031427)")
        for statement in Path(__file__).with_suffix(".sql").read_text(encoding="utf-8").split(";"):
            if statement.strip():
                db.connection.execute(statement)
