from __future__ import annotations

import argparse
import json
from pathlib import Path

from .common import save


def main():
    parser = argparse.ArgumentParser(description="MEDIC P0: synthetic feasibility and read-only local asset audit")
    sub = parser.add_subparsers(dest="command", required=True)
    assets = sub.add_parser("assets")
    assets.add_argument("--root", required=True)
    assets.add_argument("--out", required=True)
    provider = sub.add_parser("provider")
    provider.add_argument("--api-example", help="Local private curl example; only URL/model/Bearer token are parsed")
    provider.add_argument("--out", required=True)
    schema = sub.add_parser("schema")
    schema.add_argument("--binary", required=True)
    schema.add_argument("--generated", required=True)
    schema.add_argument("--out", required=True)
    baseline = sub.add_parser("baseline")
    baseline.add_argument("--out", required=True)
    appserver = sub.add_parser("appserver")
    appserver.add_argument("--binary", required=True)
    appserver.add_argument("--api-example")
    appserver.add_argument("--namespace-gateway", action="store_true", help="Use the P0 loopback namespace adapter")
    appserver.add_argument("--threads", type=int, default=4, choices=(2, 3, 4))
    appserver.add_argument("--out", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--directory", required=True)
    build = sub.add_parser("build")
    build.add_argument("--cargo", default="cargo")
    build.add_argument("--directory", required=True)
    args = parser.parse_args()
    if args.command == "assets":
        from .assets import audit
        report = audit(args.root, args.out)
    elif args.command == "provider":
        from .provider import Config, suite
        report = suite(Config.load(args.api_example), args.out)
    elif args.command == "appserver":
        from .provider import Config
        from .appserver import suite
        config = Config.load(args.api_example)
        if args.namespace_gateway:
            from .namespace_gateway import Gateway
            from .common import digest
            with Gateway(config) as gateway:
                report = suite(args.binary, gateway.config, args.out, args.threads)
            report["upstream_endpoint_sha256"] = digest(config.base_url)
            report["route"] = "responses_namespace_gateway"
            report["gateway_trace"] = gateway.trace
            save(args.out, report)
        else:
            report = suite(args.binary, config, args.out, args.threads)
            report["route"] = "responses_direct"
            save(args.out, report)
    elif args.command == "schema":
        from .baseline import generate
        report = generate(args.binary, args.generated)
        save(args.out, report)
    elif args.command == "baseline":
        from .baseline import inspect
        report = inspect()
        save(args.out, report)
    elif args.command == "build":
        from .baseline import build
        report = build(args.cargo, args.directory)
    else:
        from .gate import evaluate
        directory = Path(args.directory)
        reports = {name: json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
                   for name in ("baseline", "provider", "assets", "appserver")}
        report = evaluate(reports)
        save(directory / "gate.json", report)
    # Avoid printing private paths, credentials or model text, even on failure.
    print(json.dumps({k: v for k, v in report.items() if k in
                      ("status", "checks", "route", "blockers", "p0_accepted", "deployment_ready")}, ensure_ascii=False))
    return 0 if report.get("status") == "pass" or report.get("p0_accepted") else 2


if __name__ == "__main__":
    raise SystemExit(main())
