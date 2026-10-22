from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .common import REVISION, ROOT, SCHEMAS, digest, now, save


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def inspect(binary=None, generated=None):
    paths = ["codex-rs", "Cargo.lock", ".cargo", "third_party", "patches"]
    # Verify both committed and uncommitted upstream code against the selected revision.
    changed = git("diff", "--name-only", REVISION, "--", *paths).splitlines()
    schema_files = sorted(SCHEMAS.rglob("*.json"))
    schemas = {str(p.relative_to(SCHEMAS)).replace("\\", "/"): digest(p.read_bytes()) for p in schema_files}
    lock = {"created_at": now(), "codex_revision": REVISION, "workspace_head": git("rev-parse", "HEAD"),
            "source_matches_revision": not changed, "changed_upstream_files": changed,
            "rust_toolchain_sha256": digest((ROOT / "codex-rs/rust-toolchain.toml").read_bytes()),
            "cargo_lock_sha256": digest((ROOT / "codex-rs/Cargo.lock").read_bytes()),
            "committed_schema_count": len(schemas), "committed_schema_manifest_sha256": digest(schemas),
            "binary": None, "generated_schema_matches": False, "deployment_ready": False}
    if binary:
        binary = Path(binary).resolve()
        lock["binary"] = {"sha256": digest(binary.read_bytes()), "version": subprocess.check_output(
            [str(binary), "--version"], text=True, timeout=20).strip()}
    if generated:
        generated = Path(generated)
        comparisons = {}
        for relative in ("v1/InitializeParams.json", "v1/InitializeResponse.json", "v2/ThreadStartParams.json",
                         "v2/ThreadStartResponse.json", "v2/ThreadResumeParams.json", "v2/ThreadResumeResponse.json",
                         "v2/TurnStartParams.json", "v2/TurnStartResponse.json", "v2/TurnInterruptParams.json",
                         "v2/TurnInterruptResponse.json", "v2/TurnCompletedNotification.json"):
            other = generated / relative
            comparisons[relative] = other.exists() and json.loads(other.read_text()) == json.loads((SCHEMAS / relative).read_text())
        lock["generated_contracts"] = comparisons
        lock["generated_schema_matches"] = all(comparisons.values())
    return lock


def generate(binary, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(Path(binary).resolve()), "app-server", "generate-json-schema", "--out", str(output.resolve())],
                   check=True, capture_output=True, timeout=120)
    return inspect(binary, output)


def build(cargo, directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    before = inspect()
    if not before["source_matches_revision"]:
        raise ValueError("upstream_source_modified")
    # Force Cargo to invoke the pinned rustup toolchain; leave user settings unchanged.
    env = os.environ.copy()
    # Preserve the cargo shim basename: resolving its link to rustup changes argv[0] dispatch.
    cargo = Path(cargo).absolute() if Path(cargo).is_file() else cargo
    if isinstance(cargo, Path):
        env["PATH"] = str(cargo.parent) + os.pathsep + env.get("PATH", "")
    env["RUSTUP_TOOLCHAIN"] = "1.95.0"
    with (directory / "build.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run([str(cargo), "build", "--locked", "-p", "codex-cli", "--bin", "codex"],
                                   cwd=ROOT / "codex-rs", env=env, stdout=log, stderr=log, timeout=3600)
    binary = ROOT / "codex-rs/target/debug" / ("codex.exe" if os.name == "nt" else "codex")
    lock = generate(binary, directory / "generated-schema") if completed.returncode == 0 else inspect()
    lock["build"] = {"exit_code": completed.returncode, "profile": "debug", "command": "cargo build --locked -p codex-cli --bin codex",
                     "log_sha256": digest((directory / "build.log").read_bytes()), "rust_toolchain": "1.95.0"}
    lock["status"] = "pass" if completed.returncode == 0 and lock["source_matches_revision"] and lock["generated_schema_matches"] else "fail"
    lock["deployment_ready"] = False  # A debug feasibility build is not a production deployment release.
    save(directory / "baseline.json", lock)
    return lock
