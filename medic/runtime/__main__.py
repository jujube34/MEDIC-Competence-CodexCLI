import argparse
import json
import os
from pathlib import Path

from medic.runtime import build  # Capture source identity before loading execution modules.
from medic.adapters.codex.worker import CodexWorker
from medic.p0.common import save
from medic.p0.namespace_gateway import Gateway
from medic.p0.provider import Config
from medic.runtime.runner import execute
from medic.runtime.store import Store
from medic.runtime.budget import BudgetLedger
from medic.runtime.admission import verify_execution


def main():
    parser = argparse.ArgumentParser(description="MEDIC synthetic assessment runtime")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--input")
    action.add_argument("--resume-run")
    action.add_argument("--cancel-run")
    parser.add_argument("--binary")
    parser.add_argument("--api-example")
    parser.add_argument("--limits", help="JSON budget limits for a new run; never accepted during resume")
    parser.add_argument("--plan-only", action="store_true", help="Run the real Coordinator, persist its graph, and stop before child execution")
    parser.add_argument("--database-env", help="Environment variable containing a PostgreSQL DSN; omitted uses output/runtime.sqlite3")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = os.environ.get(args.database_env) if args.database_env else output / "runtime.sqlite3"
    if not database:
        parser.error("configured database environment variable is empty")
    store = Store(database)
    if args.cancel_run:
        store.cancel(args.cancel_run)
        print(json.dumps({"run_id": args.cancel_run, "state": store.snapshot(args.cancel_run)["state"]}), flush=True)
        return 0
    if not args.binary:
        parser.error("--binary is required to execute an assessment")
    provider = Config.load(args.api_example)
    source = store.snapshot(args.resume_run)['source'] if args.resume_run else json.loads(Path(args.input).read_text(encoding='utf-8'))
    verify_execution(source, provider)
    if args.resume_run:
        if args.limits:
            parser.error("--limits cannot change a resumed run's frozen budget")
        run = args.resume_run
        store.recover(run)
    else:
        limits = json.loads(Path(args.limits).read_text(encoding="utf-8")) if args.limits else None
        run = store.create_run(source, limits=limits)
    print(json.dumps({"run_id": run, "state": store.snapshot(run)["state"]}), flush=True)
    ledger = BudgetLedger(store, run)
    limits = ledger.snapshot()["limits"]
    with Gateway(provider, max_requests=limits["requests"], max_output_tokens=4096, request_timeout=210,
                 max_total_tokens=limits["total_tokens"], max_concurrency=limits["concurrency"], ledger=ledger) as gateway:
        worker = CodexWorker(args.binary, gateway.config, store, output / "workers")
        final = execute(store, run, worker, concurrency=limits["concurrency"], progress=lambda x: print(json.dumps(x), flush=True), stop_after_plan=args.plan_only)
    # Keep each gateway attempt rather than overwriting previous restart evidence.
    usage = ledger.snapshot()
    save(output / ("gateway-" + str(usage["requests"]) + ".json"), {"requests": gateway.trace, "charged_tokens": gateway.charged_tokens,
                                                                 "admission_rejections": gateway.admission_rejections})
    save(output / "gateway-summary.json", {"requests": gateway.trace, "charged_tokens": gateway.charged_tokens, "durable_budget": usage,
                                           "admission_rejections": gateway.admission_rejections})
    save(output / "run.json", final)
    from medic.runtime.report import render
    render(final, output / "review.html")
    print(json.dumps({"run_id": run, "state": final["state"], "gate": (final.get("report") or {}).get("gate")}), flush=True)
    planned = args.plan_only and any(t["role"] == "coordinator" and t["status"] == "completed" for t in final["tasks"])
    return 0 if planned or final["state"] in ("ready_for_human_review", "awaiting_human_review") else 2


if __name__ == "__main__":
    raise SystemExit(main())
