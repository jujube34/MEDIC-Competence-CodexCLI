"""Transactional runtime store with SQLite and PostgreSQL backends."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from medic.runtime import build
from medic.domain.contracts import AssessmentInput, DispatchRequest
from medic.domain.graph import AGGREGATE, MATERIAL_ROLES, AGGREGATE_ROLES, required_graph, scoped_materials, scoped_spans, source_groups
from medic.domain.validation import InvalidResult, release_gate, validate_result, verify_input
from medic.p0.common import digest
from medic.runtime.budget import BudgetLimits

REQUIRED = MATERIAL_ROLES


class Denied(ValueError):
    pass


class Store:
    def __init__(self, path):
        from medic.runtime.database_location import classify
        backend, location = classify(path)
        self.postgres = backend == 'postgres'
        if self.postgres:
            from medic.runtime.postgres import initialize
            self.path = location
            initialize(self.path)
            return
        self.path = str(Path(location).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY,input_json TEXT NOT NULL,input_hash TEXT NOT NULL,
              state TEXT NOT NULL,mode TEXT NOT NULL,created REAL NOT NULL,report_json TEXT);
            CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,role TEXT NOT NULL,
              material_id TEXT NOT NULL,competency_id TEXT NOT NULL,dependencies TEXT NOT NULL,status TEXT NOT NULL,
              lease_epoch INTEGER NOT NULL DEFAULT 0,lease_until REAL,token_hash TEXT,context_hash TEXT,candidate_hash TEXT,
              context_json TEXT,execution_mode TEXT,thread_id TEXT,turn_id TEXT,result_hash TEXT,error TEXT,
              UNIQUE(run_id,role,material_id,competency_id));
            CREATE TABLE IF NOT EXISTS results(task_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,value_json TEXT NOT NULL,
              result_hash TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(task_id,lease_epoch));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,task_id TEXT,
              kind TEXT NOT NULL,data_json TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reads(task_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,evidence_id TEXT NOT NULL,
              PRIMARY KEY(task_id,lease_epoch,evidence_id));
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,kind TEXT NOT NULL,
              delivered INTEGER NOT NULL DEFAULT 0,UNIQUE(task_id,kind));
            CREATE TABLE IF NOT EXISTS run_limits(run_id TEXT PRIMARY KEY,limits_json TEXT NOT NULL,deadline REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS model_requests(request_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,estimate INTEGER NOT NULL,
              started REAL NOT NULL,active_until REAL NOT NULL,status TEXT NOT NULL,charged INTEGER,settled REAL);
            CREATE TABLE IF NOT EXISTS mailbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,material_id TEXT NOT NULL,
              competency_id TEXT NOT NULL,message_id TEXT NOT NULL,payload_hash TEXT NOT NULL,value_json TEXT NOT NULL,
              UNIQUE(run_id,message_id));
            CREATE TABLE IF NOT EXISTS task_cycles(task_id TEXT PRIMARY KEY,business_round INTEGER NOT NULL,attempts INTEGER NOT NULL,baseline_json TEXT);
            CREATE TABLE IF NOT EXISTS task_history(task_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,task_json TEXT NOT NULL,PRIMARY KEY(task_id,lease_epoch));
            CREATE TABLE IF NOT EXISTS rework_plans(run_id TEXT PRIMARY KEY,status TEXT NOT NULL,candidates_json TEXT NOT NULL,decisions_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_builds(run_id TEXT PRIMARY KEY,identity_json TEXT NOT NULL);
            ''')
            defaults = BudgetLimits()
            db.execute("INSERT OR IGNORE INTO run_limits SELECT run_id,?,created+? FROM runs",
                       (defaults.model_dump_json(), defaults.wall_seconds))

    @contextmanager
    def connect(self, run=None, task_id=None):
        if self.postgres:
            from medic.runtime.postgres import connect
            with connect(self.path, run=run, task_id=task_id) as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, run, task, kind, data):
        db.execute("INSERT INTO events(run_id,task_id,kind,data_json,created) VALUES(?,?,?,?,?)",
                   (run, task, kind, json.dumps(data, ensure_ascii=False), time.time()))

    @contextmanager
    def read(self, run=None):
        """Observe committed state without competing for SQLite's writer lock.

        A single deferred transaction keeps multi-query snapshots consistent.
        PostgreSQL retains its existing per-run transaction/locking semantics.
        Never use this transaction to authorize a later mutation: writers must
        recheck leases, deadlines and state within their own write transaction.
        """
        if self.postgres:
            with self.connect(run) as db:
                yield db
            return
        uri = Path(self.path).as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:
            db.rollback()
            db.close()

    def create_run(self, source, mode="live", *, limits=None, creation_key=None):
        limits = BudgetLimits.model_validate(limits or {})
        source = AssessmentInput.model_validate(source)
        verify_input(source)
        # Until production isolation and business signatures are integrated, fail closed.
        if not source.synthetic:
            raise Denied("production_inputs_not_enabled")
        if mode != "live":
            raise Denied("non_live_execution_disabled")
        execution_build = build.verify(source.versions.runtime_version)
        code = source.goal.competency_scope[0]
        if creation_key is not None and (not isinstance(creation_key, str) or not 1 <= len(creation_key) <= 200):
            raise Denied('invalid_run_creation_key')
        run_id = "run-" + (digest(['run-creation',creation_key])[:32] if creation_key is not None else uuid.uuid4().hex)
        task_id = "task-" + uuid.uuid4().hex
        payload = source.model_dump()
        with self.connect(run_id) as db:
            existing = db.execute('SELECT input_hash,mode FROM runs WHERE run_id=?',(run_id,)).fetchone()
            if existing:
                prior_limits=db.execute('SELECT limits_json FROM run_limits WHERE run_id=?',(run_id,)).fetchone()[0]
                if existing['input_hash']!=digest(payload) or existing['mode']!=mode or json.loads(prior_limits)!=limits.model_dump():
                    raise Denied('run_creation_key_conflict')
                self.verify_build(db,run_id)
                return run_id
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,NULL)",
                       (run_id, json.dumps(payload, ensure_ascii=False), digest(payload), "planning", mode, time.time()))
            db.execute("INSERT INTO run_limits VALUES(?,?,?)", (run_id, limits.model_dump_json(), time.time() + limits.wall_seconds))
            db.execute('INSERT INTO run_builds VALUES(?,?)', (run_id, json.dumps(execution_build, sort_keys=True)))
            db.execute("INSERT INTO tasks(task_id,run_id,role,material_id,competency_id,dependencies,status) VALUES(?,?,?,?,?,?,?)",
                       (task_id, run_id, "coordinator", source.materials[0].material_id, code, "[]", "queued"))
            db.execute("INSERT INTO outbox(task_id,kind) VALUES(?,?)", (task_id, "task_created"))
            self.event(db, run_id, task_id, "run_created", {"input_hash": digest(payload), "synthetic": True,
                                                          'execution_build': execution_build,
                                                          "budget_limits_hash": digest(limits.model_dump())})
            for competency, policy in source.policies.items():
                if policy.mode == "human_only":
                    self.event(db, run_id, None, "manual_assessment_required", {"competency_id": competency, "reason": "human_only_policy"})
        return run_id

    def snapshot(self, run):
        from medic.runtime.mailbox import messages
        from medic.runtime.rework import plan, cycle
        with self.read(run) as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run,)).fetchone()
            if not row:
                raise Denied("unknown_run")
            tasks = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (run,))]
            results = {r["task_id"]: json.loads(r["value_json"]) for r in db.execute(
                "SELECT r.* FROM results r JOIN tasks t ON r.task_id=t.task_id AND r.lease_epoch=t.lease_epoch WHERE t.run_id=? AND t.status='completed'", (run,))}
            for task in tasks:
                task['business_round'] = cycle(db, task)['business_round']
                task['attempt_in_round'] = cycle(db, task)['attempts']
                task.pop("token_hash", None)
                task["dependencies"] = json.loads(task["dependencies"])
                task["read_evidence_ids"] = [r[0] for r in db.execute("SELECT evidence_id FROM reads WHERE task_id=? AND lease_epoch=?",
                                                                   (task["task_id"], task["lease_epoch"]))]
            events = [{"seq": e["seq"], "task_id": e["task_id"], "kind": e["kind"], "data": json.loads(e["data_json"])}
                      for e in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run,))]
            limits = db.execute("SELECT * FROM run_limits WHERE run_id=?", (run,)).fetchone()
            build_row = db.execute('SELECT identity_json FROM run_builds WHERE run_id=?', (run,)).fetchone()
            return {"run_id": run, "source": json.loads(row["input_json"]), "input_hash": row["input_hash"],
                    'execution_build': json.loads(build_row[0]) if build_row else None,
                    "state": row["state"], "mode": row["mode"], "tasks": tasks, "results": results, "events": events,
                    "mailbox": messages(db, run),
                    'rework_plan': plan(db, run),
                    'task_history': [{**json.loads(r[0]), 'result': json.loads(r[1]) if r[1] else None} for r in db.execute(
                        'SELECT h.task_json,r.value_json FROM task_history h JOIN tasks t ON h.task_id=t.task_id '
                        'LEFT JOIN results r ON h.task_id=r.task_id AND h.lease_epoch=r.lease_epoch '
                        'WHERE t.run_id=? ORDER BY h.task_id,h.lease_epoch', (run,))],
                    "budget_limits": json.loads(limits["limits_json"]), "deadline": limits["deadline"],
                    "manual_items": [{"competency_id": c, "status": "awaiting_human_review", "reason": "human_only_policy", "grade": None}
                                     for c, p in json.loads(row["input_json"])["policies"].items() if p["mode"] == "human_only"],
                    "report": json.loads(row["report_json"]) if row["report_json"] else None}

    def lease(self, task_id, seconds=480):
        with self.connect(task_id=task_id) as db:
            return self._lease(db, task_id, seconds)

    def verify_build(self, db, run):
        row = db.execute('SELECT identity_json FROM run_builds WHERE run_id=?', (run,)).fetchone()
        if not row:
            raise Denied('legacy_run_has_no_frozen_build')
        identity = json.loads(row[0])
        try:
            actual = build.verify(identity['runtime_version'])
        except ValueError as exc:
            raise Denied(str(exc)) from None
        if identity != actual:
            raise Denied('run_execution_build_mismatch')
        return actual

    def claim_next(self, run, seconds=480, *, coordinator_only=False):
        """Consume one eligible outbox row and acquire its lease in one transaction."""
        with self.connect(run) as db:
            row = db.execute("SELECT state FROM runs WHERE run_id=?", (run,)).fetchone()
            if not row or row["state"] in ("failed", "cancelled", "ready_for_human_review", "awaiting_human_review"):
                return None
            self.check_deadline(db, run)
            limits = BudgetLimits.model_validate_json(db.execute("SELECT limits_json FROM run_limits WHERE run_id=?", (run,)).fetchone()[0])
            active = db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND status IN ('running','submitted')", (run,)).fetchone()[0]
            if active >= limits.concurrency:
                return None
            coordinator = db.execute("SELECT status FROM tasks WHERE run_id=? AND role='coordinator'", (run,)).fetchone()
            completed = {r[0] for r in db.execute("SELECT task_id FROM tasks WHERE run_id=? AND status='completed'", (run,))}
            candidates = db.execute("SELECT t.* FROM tasks t JOIN outbox o ON t.task_id=o.task_id "
                                    "WHERE t.run_id=? AND t.status='queued' AND o.delivered=0 ORDER BY o.id", (run,)).fetchall()
            for task in candidates:
                if coordinator_only and task['role'] != 'coordinator':
                    continue
                if task["role"] != "coordinator" and coordinator[0] != "completed":
                    continue
                if set(json.loads(task["dependencies"])) <= completed:
                    return self._lease(db, task["task_id"], seconds)
            return None

    def _lease(self, db, task_id, seconds):
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row or row["status"] != "queued":
            raise Denied("task_not_queued")
        run = db.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
        execution_build = self.verify_build(db, row['run_id'])
        self.check_deadline(db, row["run_id"])
        if run["state"] in ("cancelled", "failed", "ready_for_human_review", "awaiting_human_review"):
            raise Denied("run_not_active")
        dep_ids = json.loads(row["dependencies"])
        deps = [db.execute("SELECT * FROM tasks WHERE task_id=?", (ident,)).fetchone() for ident in dep_ids]
        if any(not d or d["status"] != "completed" for d in deps):
            raise Denied("dependencies_not_completed")
        source = AssessmentInput.model_validate_json(run["input_json"])
        from medic.runtime.rework import cycle, plan
        task_cycle = cycle(db, row)
        prior = self._prior(db, row)
        context = {"run_id": row["run_id"], "task_id": task_id, "role": row["role"],
                   'execution_build': execution_build,
                   "competency_id": row["competency_id"], "material_id": row["material_id"],
                   "input_hash": run["input_hash"], "goal": source.goal.model_dump(), 'business_round': task_cycle['business_round'],
                   "versions": source.versions.model_dump(), "policy": source.policies[row["competency_id"]].model_dump(),
                       "material_catalog": [{"material_id": m.material_id, "version": m.version, "title": m.title,
                                             "span_count": len(m.spans)} for m in source.materials
                                            if row["role"] == "coordinator" or row["material_id"] == AGGREGATE or m.material_id == row["material_id"]], "prior": prior}
        if task_cycle['baseline_json'] and row['role'] in ('challenger', 'reevaluator', 'judge'):
            baseline = json.loads(task_cycle['baseline_json'])
            context['rework_baseline_hash'] = digest(baseline)
            context['reuse_previous_assessment'] = baseline['use_as_evaluator']
        if row["material_id"] == AGGREGATE:
            context["aggregation"] = {"source_groups": source_groups(source), "independent_event_count": len(source_groups(source))}
            if row["role"] in ("aggregator", "reevaluator"):
                context["source_result_hashes"] = {m: digest(v) for m, v in prior["source_assessments"].items()}
        if row["role"] == "coordinator":
            rework = plan(db, row['run_id'])
            if rework and rework['status'] == 'planning':
                context['rework_plan'] = rework
            context["required_roles"] = REQUIRED
            context["required_scopes"] = [{"material_id": m.material_id, "competency_id": c, "mode": source.policies[c].mode}
                                          for m in source.materials for c in source.goal.competency_scope if source.policies[c].mode != "human_only"]
            context["policy_catalog"] = {c: p.model_dump() for c, p in source.policies.items()}
            context["required_graph"] = [{"material_id": m, "competency_id": c, "role": r,
                                           "dependencies": [{"material_id": dm, "competency_id": dc, "role": dr} for dm, dc, dr in sorted(deps)]}
                                          for (m, c, r), deps in required_graph(source).items()]
        candidate_hash = digest({"goal": source.goal.model_dump(), "input_hash": run["input_hash"], "prior": prior})
        if row['role'] in ('challenger', 'reevaluator', 'judge'):
            from medic.runtime.mailbox import messages, enabled
            if enabled(source):
                context['mailbox_participants'] = [dict(t) for t in db.execute(
                    "SELECT task_id,role FROM tasks WHERE run_id=? AND material_id=? AND competency_id=? AND role IN ('challenger','reevaluator') ORDER BY task_id",
                    (row['run_id'], row['material_id'], row['competency_id']))]
                context['mailbox'] = messages(db, row['run_id'], row['material_id'], row['competency_id'])
                if context['mailbox']:
                    candidate_hash = digest({'goal': source.goal.model_dump(), 'input_hash': run['input_hash'], 'prior': prior,
                                             'mailbox': context['mailbox']})
        if row["role"] == "judge":
            context["candidate_hash"] = candidate_hash
        if row["role"] == "challenger":
            context["independent_findings_hash"] = digest(prior["challenger_scan"])
        context_hash = digest(context)
        context["context_hash"] = context_hash
        token = secrets.token_urlsafe(32)
        epoch = row["lease_epoch"] + 1
        db.execute('INSERT OR IGNORE INTO task_cycles VALUES(?,?,?,?)',
                   (task_id, task_cycle['business_round'], task_cycle['attempts'], task_cycle['baseline_json']))
        db.execute('UPDATE task_cycles SET attempts=attempts+1 WHERE task_id=?', (task_id,))
        db.execute("UPDATE tasks SET status='running',lease_epoch=?,lease_until=?,token_hash=?,context_hash=?,candidate_hash=?,context_json=? WHERE task_id=?",
                   (epoch, time.time() + seconds, digest(token), context_hash, candidate_hash, json.dumps(context, ensure_ascii=False), task_id))
        db.execute("UPDATE outbox SET delivered=1 WHERE task_id=?", (task_id,))
        self.event(db, row["run_id"], task_id, "task_leased", {"role": row["role"], "lease_epoch": epoch, "context_hash": context_hash})
        return {"task_id": task_id, "run_id": row["run_id"], "lease_epoch": epoch, "token": token, "context": context}

    def _prior(self, db, task):
        aggregate = task["material_id"] == AGGREGATE
        allowed = set((AGGREGATE_ROLES if aggregate else REQUIRED).get(task["role"], ()))
        if task["role"] in ("coordinator", "evaluator", "challenger_scan"):
            return {}
        # Re-evaluation needs the independent findings as well as disclosed challenges.
        if task["role"] == "reevaluator":
            allowed.add("challenger_scan")
        rows = db.execute("SELECT t.role,r.value_json FROM tasks t JOIN results r ON t.task_id=r.task_id AND t.lease_epoch=r.lease_epoch "
                          "WHERE t.run_id=? AND t.material_id=? AND t.competency_id=? AND t.status='completed'",
                          (task["run_id"], task["material_id"], task["competency_id"]))
        prior = {"evaluator" if r["role"] == "aggregator" else r["role"]: json.loads(r["value_json"]) for r in rows if r["role"] in allowed}
        if aggregate:
            for role, bucket in (("reevaluator", "source_assessments"), ("judge", "source_reviews")):
                rows = db.execute("SELECT t.material_id,r.value_json FROM tasks t JOIN results r ON t.task_id=r.task_id AND t.lease_epoch=r.lease_epoch "
                                  "WHERE t.run_id=? AND t.competency_id=? AND t.material_id!=? AND t.role=? AND t.status='completed'",
                                  (task["run_id"], task["competency_id"], AGGREGATE, role))
                prior[bucket] = {r["material_id"]: json.loads(r["value_json"]) for r in rows}
        from medic.runtime.rework import cycle
        baseline = cycle(db, task)['baseline_json']
        if baseline and task['role'] in ('challenger', 'reevaluator', 'judge'):
            baseline = json.loads(baseline)
            prior['previous_assessment'] = baseline['assessment']
            prior['previous_challenger'] = baseline['challenge']
            prior['previous_challenge_result_hash'] = digest(baseline['challenge'])
            if baseline['use_as_evaluator']:
                prior['evaluator'] = baseline['assessment']
        return prior

    def authorize(self, db, binding):
        if self.postgres:
            db.lock_run(binding["run_id"])
        self.verify_build(db, binding['run_id'])
        row = db.execute("SELECT * FROM tasks WHERE task_id=? AND run_id=?", (binding["task_id"], binding["run_id"])).fetchone()
        if (not row or row["status"] not in ("running", "submitted") or row["lease_epoch"] != binding["lease_epoch"]
                or row["token_hash"] != digest(binding["token"]) or row["lease_until"] < time.time()):
            raise Denied("invalid_or_expired_task_lease")
        run = db.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
        self.check_deadline(db, row["run_id"])
        if run["state"] in ("cancelled", "failed", "ready_for_human_review", "awaiting_human_review"):
            raise Denied("run_not_active")
        return row, AssessmentInput.model_validate_json(run["input_json"])

    @staticmethod
    def check_deadline(db, run):
        limit = db.execute("SELECT deadline FROM run_limits WHERE run_id=?", (run,)).fetchone()
        if not limit or time.time() >= limit[0]:
            raise Denied("run_wall_clock_budget_exhausted")

    def expire_run(self, run):
        # Most supervisor polls only observe an unexpired run. Recheck under
        # the write lock before changing state if the read suggests expiry.
        with self.read(run) as db:
            row = db.execute("SELECT r.state,l.deadline FROM runs r JOIN run_limits l ON r.run_id=l.run_id WHERE r.run_id=?", (run,)).fetchone()
            if not row or row["deadline"] > time.time() or row["state"] in ("failed", "cancelled", "ready_for_human_review", "awaiting_human_review"):
                return False
        with self.connect(run) as db:
            row = db.execute("SELECT r.state,l.deadline FROM runs r JOIN run_limits l ON r.run_id=l.run_id WHERE r.run_id=?", (run,)).fetchone()
            if not row or row["deadline"] > time.time() or row["state"] in ("failed", "cancelled", "ready_for_human_review", "awaiting_human_review"):
                return False
            db.execute("UPDATE runs SET state='failed' WHERE run_id=?", (run,))
            db.execute("UPDATE tasks SET status='failed',token_hash=NULL,error='run_wall_clock_budget_exhausted' "
                       "WHERE run_id=? AND status IN ('queued','running','submitted')", (run,))
            self.event(db, run, None, "run_deadline_exhausted", {})
            return True

    def dispatch(self, binding, request):
        request = DispatchRequest.model_validate(request)
        with self.connect() as db:
            task, source = self.authorize(db, binding)
            return self._dispatch(db, task, source, request)

    def _dispatch(self, db, task, source, request):
        """Caller owns the authorized transaction; preserve single-node checks."""
        if task["role"] != "coordinator":
            raise Denied("dispatch_coordinator_only")
        graph = required_graph(source)
        key = (request.material_id, request.competency_id, request.role)
        if request.competency_id not in source.goal.competency_scope:
            raise Denied("dispatch_out_of_scope")
        if source.policies[request.competency_id].mode == "human_only":
            raise Denied("human_only_forbids_model_task")
        if key not in graph:
            raise Denied("dispatch_out_of_scope")
        if len(request.dependencies) != len(set(request.dependencies)):
            raise Denied("duplicate_dependencies")
        dependencies = [db.execute("SELECT * FROM tasks WHERE task_id=? AND run_id=?", (ident, task["run_id"])).fetchone()
                        for ident in request.dependencies]
        if any(not d or d["competency_id"] != request.competency_id for d in dependencies):
            raise Denied("dependency_scope_mismatch")
        if request.material_id != AGGREGATE and any(d["material_id"] != request.material_id for d in dependencies):
            raise Denied("dependency_scope_mismatch")
        if {(d["material_id"], d["competency_id"], d["role"]) for d in dependencies} != graph[key]:
            raise Denied("mandatory_dependencies_mismatch")
        existing = db.execute("SELECT * FROM tasks WHERE run_id=? AND role=? AND material_id=? AND competency_id=?",
                              (task["run_id"], request.role, request.material_id, request.competency_id)).fetchone()
        if existing:
            if set(json.loads(existing["dependencies"])) != set(request.dependencies):
                raise Denied("idempotent_dispatch_conflict")
            return {"task_id": existing["task_id"], "status": existing["status"]}
        ident = "task-" + uuid.uuid4().hex
        db.execute("INSERT INTO tasks(task_id,run_id,role,material_id,competency_id,dependencies,status) VALUES(?,?,?,?,?,?,?)",
                   (ident, task["run_id"], request.role, request.material_id, request.competency_id, json.dumps(request.dependencies), "queued"))
        db.execute("INSERT INTO outbox(task_id,kind) VALUES(?,?)", (ident, "task_created"))
        self.event(db, task["run_id"], task["task_id"], "agent_dispatched_task", {"child_task_id": ident, **request.model_dump()})
        return {"task_id": ident, "status": "queued"}

    def submit(self, binding, value):
        with self.connect() as db:
            task, source = self.authorize(db, binding)
            if task["role"] == "coordinator":
                from medic.runtime.rework import plan
                rework = plan(db, task['run_id'])
                if rework and rework['status'] == 'planning' and set(rework['decisions']) != {c['judge_task_id'] for c in rework['candidates']}:
                    raise InvalidResult('rework_decisions_incomplete')
                children = [(r["material_id"], r["competency_id"], r["role"]) for r in db.execute("SELECT material_id,competency_id,role FROM tasks WHERE run_id=? AND role!='coordinator'", (task["run_id"],))]
                expected = set(required_graph(source))
                if set(children) != expected or len(children) != len(expected):
                    raise InvalidResult("coordinator_plan_incomplete")
                value = {"planned_roles": sorted({role for _, _, role in children}), "planned_scopes": sorted(children), "context_hash": task["context_hash"]}
                if rework:
                    value['rework_decisions'] = rework['decisions']
            else:
                prior = self._prior(db, task)
                value = validate_result(task["role"], value, dict(task), source, prior)
                reads = {r[0] for r in db.execute("SELECT evidence_id FROM reads WHERE task_id=? AND lease_epoch=?", (task["task_id"], task["lease_epoch"]))}
                if reads != {s.evidence_id for s in scoped_spans(source, task["material_id"])}:
                    raise InvalidResult("evidence_coverage_incomplete_read_all_scoped_spans")
            existing = db.execute("SELECT * FROM results WHERE task_id=? AND lease_epoch=?", (task["task_id"], task["lease_epoch"])).fetchone()
            hash_value = digest(value)
            if existing:
                if existing["result_hash"] != hash_value:
                    raise Denied("immutable_result_conflict")
                return {"receipt": existing["receipt"], "result_hash": hash_value}
            receipt = "submitted-" + secrets.token_hex(12)
            db.execute("INSERT INTO results VALUES(?,?,?,?,?)", (task["task_id"], task["lease_epoch"], json.dumps(value, ensure_ascii=False), hash_value, receipt))
            db.execute("UPDATE tasks SET status='submitted',result_hash=? WHERE task_id=?", (hash_value, task["task_id"]))
            self.event(db, task["run_id"], task["task_id"], "result_submitted", {"result_hash": hash_value, "lease_epoch": task["lease_epoch"]})
            return {"receipt": receipt, "result_hash": hash_value}

    def complete(self, binding, receipt, thread, turn):
        with self.connect() as db:
            task, _ = self.authorize(db, binding)
            if task["turn_id"] and (task["thread_id"] != thread or task["turn_id"] != turn):
                raise Denied("execution_identity_mismatch")
            result = db.execute("SELECT * FROM results WHERE task_id=? AND lease_epoch=?", (task["task_id"], task["lease_epoch"])).fetchone()
            if task["status"] != "submitted" or not result or not secrets.compare_digest(result["receipt"], receipt):
                raise Denied("result_receipt_or_terminal_not_verified")
            db.execute("UPDATE tasks SET status='completed',execution_mode='live',thread_id=?,turn_id=? WHERE task_id=?",
                       (thread, turn, task["task_id"]))
            self.event(db, task["run_id"], task["task_id"], "task_completed", {"thread_id": thread, "turn_id": turn, "lease_epoch": task["lease_epoch"]})
            if task["role"] == "coordinator":
                db.execute("UPDATE rework_plans SET status='dispatched' WHERE run_id=? AND status='planning'", (task['run_id'],))
                db.execute("UPDATE runs SET state='evaluating' WHERE run_id=?", (task["run_id"],))

    def bind_execution(self, binding, thread, turn):
        with self.connect() as db:
            task, _ = self.authorize(db, binding)
            if task["turn_id"] and (task["thread_id"] != thread or task["turn_id"] != turn):
                raise Denied("execution_already_bound")
            db.execute("UPDATE tasks SET thread_id=?,turn_id=? WHERE task_id=?", (thread, turn, task["task_id"]))
            self.event(db, task["run_id"], task["task_id"], "execution_bound",
                       {"thread_id": thread, "turn_id": turn, "lease_epoch": task["lease_epoch"]})

    def request_finalization(self, binding, thread, turn):
        with self.connect() as db:
            task, _ = self.authorize(db, binding)
            if (task['thread_id'], task['turn_id']) != (thread, turn):
                raise Denied('execution_identity_mismatch')
            counts = {r['kind']: r['n'] for r in db.execute(
                "SELECT kind,count(*) AS n FROM events WHERE task_id=? AND json_extract(data_json,'$.lease_epoch')=? "
                "AND kind IN ('finalization_requested','finalization_bound','result_rejected') GROUP BY kind",
                (task['task_id'], task['lease_epoch']))}
            requested = counts.get('finalization_requested', 0)
            if requested > counts.get('finalization_bound', 0):
                raise Denied('finalization_already_pending')
            if requested + counts.get('result_rejected', 0) >= 2:
                raise Denied('finalization_repair_budget_exhausted')
            self.event(db, task['run_id'], task['task_id'], 'finalization_requested',
                       {'lease_epoch': task['lease_epoch'], 'previous_turn_id': turn, 'repair': requested + 1})

    def bind_finalization(self, binding, thread, previous_turn, turn):
        with self.connect() as db:
            task, _ = self.authorize(db, binding)
            if (task['thread_id'], task['turn_id']) != (thread, previous_turn) or turn == previous_turn:
                raise Denied('execution_identity_mismatch')
            counts = {r['kind']: r['n'] for r in db.execute(
                "SELECT kind,count(*) AS n FROM events WHERE task_id=? AND json_extract(data_json,'$.lease_epoch')=? "
                "AND kind IN ('finalization_requested','finalization_bound') GROUP BY kind", (task['task_id'], task['lease_epoch']))}
            if counts.get('finalization_requested', 0) != counts.get('finalization_bound', 0) + 1:
                raise Denied('finalization_not_reserved')
            db.execute('UPDATE tasks SET turn_id=? WHERE task_id=?', (turn, task['task_id']))
            self.event(db, task['run_id'], task['task_id'], 'finalization_bound',
                       {'lease_epoch': task['lease_epoch'], 'thread_id': thread, 'previous_turn_id': previous_turn, 'turn_id': turn})

    def fail(self, binding, error):
        with self.connect(binding["run_id"]) as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=? AND run_id=? AND lease_epoch=?",
                              (binding["task_id"], binding["run_id"], binding["lease_epoch"])).fetchone()
            # A late or mismatched failure callback must not affect another run,
            # another attempt, or a completed result. Expired matching leases may
            # still report failure so the dispatcher can recover them explicitly.
            if task and task["token_hash"] == digest(binding["token"]) and task["status"] in ("running", "submitted"):
                db.execute("UPDATE tasks SET status='failed',error=? WHERE task_id=?", (error, task["task_id"]))
                db.execute("UPDATE runs SET state='failed' WHERE run_id=? AND state!='cancelled'", (task["run_id"],))
                self.event(db, task["run_id"], task["task_id"], "task_failed", {"error_code": error})

    def recover(self, run, max_attempts=3):
        """Operator/dispatcher recovery. Never infer success from a saved partial result.

        Completed nodes remain immutable. Expired/failed attempts are fenced before
        redispatch, retaining their results and events for audit. Models may execute
        again; only a verified current attempt can advance dependencies.
        """
        if not 1 <= max_attempts <= 3:
            raise Denied("invalid_attempt_budget")
        with self.connect(run) as db:
            self.verify_build(db, run)
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run,)).fetchone()
            if not row or row["state"] in ("cancelled", "ready_for_human_review", "awaiting_human_review"):
                raise Denied("run_not_recoverable")
            self.check_deadline(db, run)
            tasks = db.execute("SELECT * FROM tasks WHERE run_id=? AND (status='failed' OR "
                               "(status IN ('running','submitted') AND lease_until<?))", (run, time.time())).fetchall()
            from medic.runtime.rework import cycle, archive
            if any(cycle(db, t)['attempts'] >= max_attempts for t in tasks):
                raise Denied("task_attempt_budget_exhausted")
            for task in tasks:
                archive(db, task)
                # Nulling the token fences immediately; lease() assigns the next epoch.
                db.execute("UPDATE tasks SET status='queued',token_hash=NULL,lease_until=NULL,context_hash=NULL,"
                           "candidate_hash=NULL,context_json=NULL,result_hash=NULL,execution_mode=NULL,error=NULL,thread_id=NULL,turn_id=NULL "
                           "WHERE task_id=?", (task["task_id"],))
                db.execute("UPDATE outbox SET delivered=0 WHERE task_id=?", (task["task_id"],))
                self.event(db, run, task["task_id"], "attempt_requeued", {"previous_lease_epoch": task["lease_epoch"]})
            if tasks:
                coordinator = db.execute("SELECT status FROM tasks WHERE run_id=? AND role='coordinator'", (run,)).fetchone()
                db.execute("UPDATE runs SET state=? WHERE run_id=?", ("evaluating" if coordinator[0] == "completed" else "planning", run))
            return [t["task_id"] for t in tasks]

    def cancel(self, run):
        with self.connect(run) as db:
            db.execute("UPDATE runs SET state='cancelled' WHERE run_id=? AND report_json IS NULL", (run,))
            db.execute("UPDATE tasks SET status='cancelled',lease_epoch=lease_epoch+1 WHERE run_id=? AND status IN ('queued','running','submitted')", (run,))
            self.event(db, run, None, "run_cancelled", {})

    def publish(self, run, report):
        from medic.runtime.mailbox import messages
        from medic.runtime.rework import plan
        with self.connect(run) as db:
            execution_build = self.verify_build(db, run)
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run,)).fetchone()
            if not row or row["report_json"] is not None or row["state"] in ("cancelled", "failed"):
                raise Denied("run_cancelled_failed_or_already_published")
            self.check_deadline(db, run)
            tasks = [dict(t) for t in db.execute("SELECT * FROM tasks WHERE run_id=?", (run,))]
            if not tasks or any(t["status"] != "completed" for t in tasks):
                raise Denied("cannot_publish_incomplete_execution")
            results = {}
            for task in tasks:
                result = db.execute("SELECT value_json FROM results WHERE task_id=? AND lease_epoch=?",
                                    (task["task_id"], task["lease_epoch"])).fetchone()
                if result:
                    results[task["task_id"]] = json.loads(result[0])
                task["read_evidence_ids"] = [r[0] for r in db.execute("SELECT evidence_id FROM reads WHERE task_id=? AND lease_epoch=?",
                                                                     (task["task_id"], task["lease_epoch"]))]
            mailbox = messages(db, run)
            rework = plan(db, run)
            actual_gate = release_gate(AssessmentInput.model_validate_json(row["input_json"]), results, tasks, row["mode"], mailbox, rework)
            if report["gate"] != actual_gate:
                raise Denied("publication_gate_mismatch")
            report = {**report, 'execution_build': execution_build, 'mailbox': mailbox, 'rework_plan': rework, "human_review_required": True, "final_employee_decision": False}
            state = "ready_for_human_review" if report["gate"]["passed"] else "awaiting_human_review"
            updated = db.execute("UPDATE runs SET state=?,report_json=? WHERE run_id=? AND report_json IS NULL AND state NOT IN ('cancelled','failed')",
                                 (state, json.dumps(report, ensure_ascii=False), run))
            if updated.rowcount != 1:
                raise Denied("run_cancelled_failed_or_already_published")
            self.event(db, run, None, "preview_published", {"candidate_hash": digest(report), "state": state})
