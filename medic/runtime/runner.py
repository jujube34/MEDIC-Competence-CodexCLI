from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from medic.domain.contracts import AssessmentInput
from medic.domain.validation import release_gate
from medic.domain.graph import AGGREGATE
from medic.runtime.store import Denied


def execute(store, run_id, worker, concurrency=2, progress=None, stop_after_plan=False):
    """Execute the graph dispatched by the real Coordinator, never manufacture nodes."""
    if concurrency not in (1, 2, 3, 4):
        raise ValueError("invalid_concurrency")
    with store.connect(run_id) as db:
        store.verify_build(db, run_id)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        while True:
            store.expire_run(run_id)
            snapshot = store.snapshot(run_id)
            if snapshot["state"] in ("failed", "cancelled") or snapshot["report"] is not None:
                # Running workers observe the same durable state and interrupt turns.
                break
            if stop_after_plan and any(t["role"] == "coordinator" and t["status"] == "completed" for t in snapshot["tasks"]):
                break
            while len(futures) < concurrency:
                binding = store.claim_next(run_id, coordinator_only=stop_after_plan)
                if binding is None:
                    break
                future = pool.submit(worker.run, binding["task_id"], binding=binding)
                futures[future] = binding
                if stop_after_plan:
                    break
            if futures:
                done, _ = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
            else:
                snapshot = store.snapshot(run_id)
                if stop_after_plan and any(t['role'] == 'coordinator' and t['status'] == 'completed' for t in snapshot['tasks']):
                    break
                active = [t for t in snapshot["tasks"] if t["status"] in ("running", "submitted")]
                if not active:
                    if any(t["status"] == "queued" for t in snapshot["tasks"]):
                        raise Denied("no_ready_task_in_incomplete_graph")
                    from medic.runtime.rework import prepare
                    if prepare(store, run_id):
                        continue
                    break
                # Another dispatcher may own these leases. Reclaim only expired work.
                if any(t["lease_until"] < time.time() for t in active):
                    store.recover(run_id)
                time.sleep(0.25)
                done = ()
            for future in done:
                binding = futures.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    store.fail(binding, type(exc).__name__)
                    outcome = {"task_id": binding["task_id"], "status": "failed", "error": type(exc).__name__}
                if progress:
                    progress(outcome)
    # Context manager has joined our workers; take a fresh snapshot so terminal
    # files include late cancellation/failure events rather than an earlier view.
    for future, binding in futures.items():
        try:
            outcome = future.result()
        except Exception as exc:
            store.fail(binding, type(exc).__name__)
            outcome = {"task_id": binding["task_id"], "status": "failed", "error": type(exc).__name__}
        if progress:
            progress(outcome)
    snapshot = store.snapshot(run_id)
    if snapshot["state"] in ("failed", "cancelled") or snapshot["report"] is not None:
        return snapshot
    if stop_after_plan:
        return snapshot
    source = AssessmentInput.model_validate(snapshot["source"])
    gate = release_gate(source, snapshot["results"], snapshot["tasks"], snapshot["mode"], snapshot.get('mailbox'), snapshot.get('rework_plan'))
    report = {"run_id": run_id, "synthetic": source.synthetic, "input_hash": snapshot["input_hash"],
              "budget_limits": snapshot["budget_limits"], "deadline": snapshot["deadline"],
              "manual_items": snapshot["manual_items"], 'mailbox': snapshot.get('mailbox', []),
              'rework_plan': snapshot.get('rework_plan'), 'task_history': snapshot.get('task_history', []),
              "versions": source.versions.model_dump(), "goal": source.goal.model_dump(), "gate": gate,
              "assessments": [snapshot["results"][t["task_id"]] for t in snapshot["tasks"] if t["role"] == "reevaluator"
                              and (len(source.materials) == 1 or t["material_id"] == AGGREGATE)],
              "material_assessments": [snapshot["results"][t["task_id"]] for t in snapshot["tasks"]
                                       if t["role"] == "reevaluator" and t["material_id"] != AGGREGATE],
              "human_review_required": True, "final_employee_decision": False}
    try:
        store.publish(run_id, report)
    except Denied:
        # Concurrent dispatchers may race to publish the same completed graph.
        if store.snapshot(run_id)["report"] is None:
            raise
    return store.snapshot(run_id)
