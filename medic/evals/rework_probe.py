"""Prepare an explicit seeded protocol fixture for real rework-role execution.

The initial results are test fixtures, NOT model outputs. They are marked fixture
and deliberately cannot satisfy the full live gate. Only subsequent actual model
turns can establish rework/mailbox execution evidence.
"""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from medic.p0.common import ROOT, save
from medic.runtime.store import Store
from medic.tests.test_rework import ReworkContracts


def prepare(output):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / 'medic/artifacts').resolve()):
        raise ValueError('probe_output_requires_private_artifacts_directory')
    output.mkdir(parents=True, exist_ok=True)
    target = output / 'runtime.sqlite3'
    if target.exists():
        raise ValueError('probe_database_already_exists')
    harness = ReworkContracts()
    harness.setUp()
    try:
        harness.initial_review()
        with harness.store.connect(harness.run) as db:
            db.execute("UPDATE tasks SET execution_mode='fixture' WHERE run_id=?", (harness.run,))
            harness.store.event(db, harness.run, None, 'seeded_protocol_fixture',
                                {'initial_results_are_model_outputs': False, 'quality_claim': False})
        with closing(sqlite3.connect(harness.store.path)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
        snapshot = Store(target).snapshot(harness.run)
        save(output / 'seeded-initial.json', snapshot)
        record = {'run_id': harness.run, 'seeded_initial_task_count': len(snapshot['tasks']),
                  'initial_results_are_model_outputs': False, 'expected_full_live_gate': False,
                  'scope': 'real_rework_and_mailbox_protocol_probe_on_seeded_initial_state'}
        save(output / 'probe.json', record)
        return record
    finally:
        harness.tearDown()


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output)))
