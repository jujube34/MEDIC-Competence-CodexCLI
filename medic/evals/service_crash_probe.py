"""Real Windows supervisor termination and real-model recovery, synthetic input only.

This probe owns a fresh private SQLite database and every process it terminates.
Lease expiry is explicitly injected after proving the restarted service defers;
the immutable run deadline and all model request reservations remain untouched.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from medic.evals.verify_run import verify
from medic.p0.common import digest, now, save
from medic.runtime import build
from medic.runtime.budget import BudgetLedger
from medic.runtime.service import Queue
from medic.runtime.service_process import ProcessJob
from medic.runtime.store import Denied, Store


class WindowsProcesses:
    """Observe descendants and retain handles so PID reuse cannot affect a kill."""
    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self.api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.api.CloseHandle.argtypes = [w.HANDLE]

    def descendants(self, root):
        class Entry(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                        ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                        ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                        ('exe', w.WCHAR * 260)]
        self.api.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        self.api.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        handle = self.api.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise RuntimeError('process_inventory_failed')
        rows = []
        try:
            entry = Entry(); entry.size = ctypes.sizeof(entry)
            ok = self.api.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                rows.append({'pid': entry.pid, 'parent': entry.parent, 'exe': entry.exe})
                ok = self.api.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(handle)
        owned = {root}
        while True:
            expanded = owned | {r['pid'] for r in rows if r['parent'] in owned}
            if expanded == owned:
                return [r for r in rows if r['pid'] in owned and r['pid'] != root]
            owned = expanded

    def open(self, pid, terminate=False):
        handle = self.api.OpenProcess(0x100000 | (1 if terminate else 0), False, pid)
        if not handle:
            raise RuntimeError('owned_process_handle_unavailable')
        return handle

    def exited(self, handle, timeout=0):
        return self.api.WaitForSingleObject(handle, timeout) == 0


def wait_until(predicate, timeout, *, process=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError('owned_service_exited_early')
        value = predicate()
        if value:
            return value
        time.sleep(.2)
    raise TimeoutError('probe_observation_deadline')


def probe(folder, binary, api_example):
    if os.name != 'nt':
        raise RuntimeError('windows_required')
    folder = Path(folder).resolve()
    if not any(folder.is_relative_to(build.ROOT / p) for p in ('artifacts', 'private', 'var')):
        raise ValueError('fresh_private_probe_directory_required')
    folder.mkdir(parents=True, exist_ok=False)
    identity = build.verify(build.VERSION)
    source = json.loads((build.ROOT / 'evals/fixtures/single_dimension_current.json').read_text(encoding='utf-8'))
    if source.get('synthetic') is not True:
        raise ValueError('synthetic_input_required')
    store = Store(folder / 'runtime.sqlite3')
    run = store.create_run(source)
    queue = Queue(store); ledger = BudgetLedger(store, run)
    original = ledger.snapshot()
    save(folder / 'probe-state.json', {'run_id': run, 'execution_build': identity})
    checks = {}; processes = []; logs = []; handles = []
    api = WindowsProcesses(); guard = ProcessJob()

    def start(number):
        logpath = folder / f'service-{number}.log'
        pidpath = folder / f'service-{number}.pid'
        log = logpath.open('xb'); logs.append(log)
        # The venv launcher may spawn another interpreter. Record the interpreter
        # that actually owns the supervisor, and join our cleanup guard first.
        wrapper = ('import os,sys,runpy; from pathlib import Path; '
                   'from medic.runtime.service_process import ProcessJob; '
                   'j=ProcessJob(sys.argv.pop(1)); j.close(); '
                   'Path(sys.argv.pop(1)).write_text(str(os.getpid())); '
                   'runpy.run_module("medic.runtime.service",run_name="__main__")')
        process = subprocess.Popen([sys.executable, '-c', wrapper, guard.name, str(pidpath),
            '--database-env', 'MEDIC_CRASH_PROBE_DB', '--binary', str(Path(binary).resolve()),
            '--api-example', str(Path(api_example).resolve()), '--output', str(folder / 'queue'),
            '--poll-seconds', '0.2'], cwd=build.ROOT.parent,
            env={**os.environ, 'MEDIC_CRASH_PROBE_DB': store.path, 'PYTHONUTF8': '1'},
            stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        processes.append(process); guard.assign(process)
        wait_until(lambda: pidpath.exists() and 'service_ready' in logpath.read_text(encoding='utf-8'),
                   30, process=process)
        return process, int(pidpath.read_text())

    def attempts():
        with store.connect(run) as db:
            return [dict(r) for r in db.execute('SELECT * FROM service_attempts WHERE run_id=? ORDER BY attempt', (run,))]

    try:
        first, pid = start(1)
        print(json.dumps({'phase': 'started', 'run_id': run}), flush=True)

        def in_flight_after_plan():
            snapshot = store.snapshot(run)
            completed = [t for t in snapshot['tasks'] if t['role'] == 'coordinator' and t['status'] == 'completed']
            active = [t for t in snapshot['tasks'] if t['role'] != 'coordinator' and t['status'] == 'running']
            protocols = list((folder / 'queue' / run).glob('attempt-1-*/workers/*/1/protocol-summary.json'))
            return completed and active and protocols and ledger.snapshot()['unsettled_requests'] > 0

        wait_until(in_flight_after_plan, 360, process=first)
        tree = api.descendants(pid)
        checks['real_codex_descendant_observed'] = any(r['exe'].lower() == 'codex.exe' for r in tree)
        supervisor = api.open(pid, terminate=True); handles.append(supervisor)
        descendants = [(row, api.open(row['pid'])) for row in tree]
        handles.extend(h for _, h in descendants)
        checks['supervisor_and_descendants_alive_before_kill'] = not api.exited(supervisor) and all(not api.exited(h) for _, h in descendants)
        if not all(checks.values()):
            raise RuntimeError('fault_preconditions_not_met')
        if not api.api.TerminateProcess(supervisor, 91):
            raise RuntimeError('owned_supervisor_termination_failed')
        checks['hard_kill_confirmed'] = api.exited(supervisor, 5000)
        checks['all_observed_descendants_exit_without_guard_cleanup'] = all(api.exited(h, 5000) for _, h in descendants)
        first.wait(timeout=10)
        if not all(checks.values()):
            raise RuntimeError('supervisor_did_not_clean_process_tree')
        before = store.snapshot(run); charged = ledger.snapshot()
        save(folder / 'after-crash.json', {'snapshot': before, 'budget': charged, 'process_tree': tree})
        interrupted = [t for t in before['tasks'] if t['status'] in ('running', 'submitted')]
        preserved = [t for t in before['tasks'] if t['status'] == 'completed']
        bindings = []
        for task in interrupted:
            path = next((folder / 'queue' / run).glob(f"attempt-1-*/workers/{task['task_id']}/{task['lease_epoch']}/binding.json"))
            binding = json.loads(path.read_text(encoding='utf-8'))
            with store.connect(run) as db:
                store.authorize(db, binding)
            bindings.append(binding)
        checks['live_leases_and_unsettled_request_survive_crash'] = bool(bindings) and charged['unsettled_requests'] > 0
        second, _ = start(2)
        time.sleep(2)  # Ten actual discovery polls while original leases remain valid.
        after_start = attempts()
        checks['restart_defers_unexpired_leases'] = len(after_start) == 1 and after_start[0]['status'] == 'interrupted'
        checks['restart_keeps_exact_budget'] = ledger.snapshot() == charged
        checks['restart_keeps_exact_tasks_and_results'] = store.snapshot(run)['tasks'] == before['tasks'] and store.snapshot(run)['results'] == before['results']
        if not all(checks.values()):
            raise RuntimeError('restart_changed_unexpired_work')
        # Controlled fault injection, scoped to this fresh synthetic run only.
        # This accelerates expiry; it is not proof of waiting the default 480s.
        expiry = time.time() - 1
        with store.connect(run) as db:
            db.execute("UPDATE tasks SET lease_until=? WHERE run_id=? AND status IN ('running','submitted')", (expiry, run))
            db.execute('UPDATE model_requests SET active_until=? WHERE run_id=? AND charged IS NULL', (expiry, run))
            store.event(db, run, None, 'probe_injected_expiry', {'task_count': len(interrupted), 'request_count': charged['unsettled_requests']})
        print(json.dumps({'phase': 'killed_and_restarted', 'run_id': run, 'checks': checks,
                          'observed_descendants': len(tree), 'lease_expiry_injected': True}), flush=True)

        def resumed():
            current = {t['task_id']: t for t in store.snapshot(run)['tasks']}
            return all(current[t['task_id']]['lease_epoch'] > t['lease_epoch'] for t in interrupted)
        wait_until(resumed, 40, process=second)
        denied = 0
        for binding in bindings:
            try:
                with store.connect(run) as db:
                    store.authorize(db, binding)
            except Denied as exc:
                if str(exc) != 'invalid_or_expired_task_lease':
                    raise
                denied += 1
        checks['old_bindings_fenced_after_reclaim'] = denied == len(bindings)
        wait_until(lambda: len(attempts()) == 2 and attempts()[-1]['status'] in ('completed', 'blocked'), 1200, process=second)
        final = store.snapshot(run); usage = ledger.snapshot(); final_attempts = attempts()
        current = {t['task_id']: t for t in final['tasks']}
        checks['same_run_and_frozen_identity'] = final['run_id'] == run and final['execution_build'] == identity
        checks['interrupted_then_completed_service_attempts'] = [a['status'] for a in final_attempts] == ['interrupted', 'completed']
        checks['completed_nodes_not_reexecuted'] = all(current[t['task_id']] == t and final['results'][t['task_id']] == before['results'][t['task_id']] for t in preserved)
        checks['deadline_and_limits_not_reset'] = usage['deadline'] == original['deadline'] and usage['limits'] == original['limits']
        prior_rows = {r['request_id']: r for r in charged['ledger']}
        new_rows = {r['request_id']: r for r in usage['ledger']}
        checks['old_request_charges_and_reservations_retained'] = all(
            {k: v for k, v in new_rows.get(key, {}).items() if k != 'active_until'} ==
            {k: v for k, v in row.items() if k != 'active_until'} for key, row in prior_rows.items())
        checks['one_publication'] = sum(e['kind'] == 'preview_published' for e in final['events']) == 1
        try:
            store.publish(run, final['report'])
        except Denied as exc:
            checks['duplicate_publication_denied'] = str(exc) == 'run_cancelled_failed_or_already_published'
        else:
            checks['duplicate_publication_denied'] = False
        checks['terminal_run_not_queued'] = run not in queue.pending()

        # Build a separate audit view. Originals and incomplete epoch protocols
        # remain untouched. Only exact current task/epoch files are selected.
        audit = folder / 'audit'; audit.mkdir()
        save(audit / 'run.json', final)
        provenance = []
        for task in final['tasks']:
            suffix = Path('workers') / task['task_id'] / str(task['lease_epoch']) / 'protocol-summary.json'
            candidates = [p / suffix for p in (folder / 'queue' / run).glob('attempt-*') if (p / suffix).is_file()]
            if len(candidates) != 1:
                raise RuntimeError('current_epoch_protocol_missing_or_ambiguous')
            target = audit / suffix; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidates[0], target)
            provenance.append({'relative_path': candidates[0].relative_to(folder).as_posix(), 'sha256': digest(target.read_bytes())})
        latest = list((folder / 'queue' / run).glob('attempt-2-*/gateway-summary.json'))
        if len(latest) != 1:
            raise RuntimeError('resumed_gateway_summary_missing')
        shutil.copyfile(latest[0], audit / 'gateway-summary.json')
        save(audit / 'protocol-origins.json', provenance)
        verification = verify(audit, folder / 'runtime-verification.json')
        checks['independent_protocol_and_hard_gate_verification'] = verification['status'] == 'pass'
        checks['frozen_source_unchanged'] = build.verify(build.VERSION) == identity
        summary = {'created_at': now(), 'status': 'pass' if all(checks.values()) else 'fail',
            'scope': 'windows_sqlite_real_supervisor_hard_kill_real_model_synthetic_recovery',
            'run_id': run, 'runtime_version': build.VERSION, 'manifest_sha256': identity['manifest_sha256'],
            'checks': checks, 'observed_descendants': len(tree), 'completed_nodes_preserved': len(preserved),
            'interrupted_tasks_recovered': len(interrupted), 'lease_expiry_injected': True,
            'default_lease_wait_duration_verified': False, 'run_total_requests': usage['requests'],
            'charged_or_reserved_tokens': usage['charged_or_reserved_tokens'],
            'unsettled_requests_retained': usage['unsettled_requests'],
            'gateway_trace_scope': 'second_service_attempt_only; durable_ledger_covers_both',
            'protocol_origins_sha256': digest((audit / 'protocol-origins.json').read_bytes()),
            'business_signoff': False, 'production_ha_verified': False}
        save(folder / 'verification.json', summary)
        print(json.dumps(summary), flush=True)
        return summary
    finally:
        guard.close()
        for process in processes:
            process.wait(timeout=10)
        for handle in handles:
            api.api.CloseHandle(handle)
        for log in logs:
            log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--api-example', required=True)
    args = parser.parse_args()
    result = probe(args.output, args.binary, args.api_example)
    raise SystemExit(0 if result['status'] == 'pass' else 1)
