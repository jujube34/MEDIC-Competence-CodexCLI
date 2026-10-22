"""Execution identity for immutable application releases and restart admission.

This is an integrity check against accidental mixed deployments, not a signature
or a substitute for read-only deployment directories and a trusted release image.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys


VERSION = 'medic-runtime-p4-v16'
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'runtime/build-manifest.json'
CODE_DIRS = ('adapters', 'apps', 'domain', 'ingestion', 'p0', 'prompts', 'runtime', 'tools', 'schemas')


def sha(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def inventory(root=ROOT):
    """Explicit executable/resource roots; never walk private artifacts or data."""
    root = Path(root)
    paths = list(root.glob('requirements*.txt')) + list(root.glob('*.py'))
    for name in CODE_DIRS:
        paths.extend(p for p in (root / name).rglob('*') if p.is_file()
                     and p.suffix in ('.py', '.sql') and '__pycache__' not in p.parts)
    paths.extend((root / 'prompts').glob('*.json'))
    paths.extend((root / 'schemas').glob('*.json'))
    paths.extend(p for p in (root / 'apps/web').rglob('*') if p.is_file() and p.suffix in ('.html','.css','.js'))
    return {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in sorted(set(paths))}


def environment():
    names = ('httpx', 'httpcore', 'h11', 'anyio', 'sniffio', 'idna', 'certifi', 'jsonschema',
             'attrs', 'jsonschema-specifications', 'referencing', 'rpds-py', 'pydantic',
             'pydantic_core', 'annotated-types', 'typing_extensions', 'openpyxl', 'et_xmlfile',
             'psycopg', 'psycopg-binary', 'PyMuPDF', 'pypdf', 'defusedxml', 'fastapi', 'starlette',
             'uvicorn', 'annotated-doc', 'typing-inspection', 'opentelemetry-api')
    packages = {}
    for name in names:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {'python': platform.python_version(), 'implementation': platform.python_implementation(),
            'platform': sys.platform, 'executable_sha256': sha(Path(sys.executable).read_bytes()),
            'packages': packages}


# Capture before Store imports the execution modules. A long-lived process must
# not bless changed source files merely because a new manifest was installed.
BOOT_FILES = inventory()
BOOT_ENVIRONMENT = environment()


def verify(version, *, root=ROOT, manifest_path=MANIFEST, boot_files=None, execution_environment=None):
    if version != VERSION:
        raise ValueError('runtime_release_not_supported')
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    files = inventory(root)
    if manifest.get('runtime_version') != version or manifest.get('files') != files or not files:
        raise ValueError('runtime_release_files_changed')
    if files != (BOOT_FILES if boot_files is None else boot_files):
        raise ValueError('runtime_process_requires_restart')
    return {'runtime_version': version, 'manifest_sha256': sha(manifest),
            'environment': BOOT_ENVIRONMENT if execution_environment is None else execution_environment}
