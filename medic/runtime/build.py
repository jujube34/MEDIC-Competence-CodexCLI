"""Execution identity for immutable application releases and restart admission.

This is an integrity check against accidental mixed deployments, not a signature
or a substitute for read-only deployment directories and a trusted release image.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys


VERSION = 'medic-runtime-p4-v18'
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
    paths.extend((root / 'p0').glob('*.json'))
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


# Verification is an optional audit gate, not a hot-path requirement. Local
# development can set MEDIC_BUILD_VERIFY=off to iterate without re-freezing the
# whole runtime for every prompt or web-asset change.
_VERIFICATION_DISABLED = os.environ.get('MEDIC_BUILD_VERIFY', '').strip().lower() in ('off', '0', 'false', 'disabled')

# Capture before Store imports the execution modules. A long-lived process must
# not bless changed source files merely because a new manifest was installed.
BOOT_FILES = None if _VERIFICATION_DISABLED else inventory()
BOOT_ENVIRONMENT = environment()
_VERIFIED = {}


def verify(version, *, root=ROOT, manifest_path=MANIFEST, boot_files=None, execution_environment=None):
    if version != VERSION:
        raise ValueError('runtime_release_not_supported')
    if _VERIFICATION_DISABLED:
        return {'runtime_version': version, 'manifest_sha256': None,
                'environment': BOOT_ENVIRONMENT if execution_environment is None else execution_environment,
                'verification': 'disabled'}
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    files = inventory(root)
    if manifest.get('runtime_version') != version or manifest.get('files') != files or not files:
        raise ValueError('runtime_release_files_changed')
    expected = BOOT_FILES if boot_files is None else boot_files
    if expected is None:
        expected = files
    if files != expected:
        raise ValueError('runtime_process_requires_restart')
    return {'runtime_version': version, 'manifest_sha256': sha(manifest),
            'environment': BOOT_ENVIRONMENT if execution_environment is None else execution_environment}


def verify_once(version=VERSION, **kwargs):
    """Return the process-wide verification result, hashing at most once."""
    if version != VERSION:
        raise ValueError('runtime_release_not_supported')
    if version not in _VERIFIED:
        if _VERIFICATION_DISABLED:
            _VERIFIED[version] = {'runtime_version': version, 'manifest_sha256': None,
                                  'environment': BOOT_ENVIRONMENT, 'verification': 'disabled'}
        else:
            _VERIFIED[version] = verify(version, **kwargs)
    return _VERIFIED[version]


def reset_verification_cache():
    """Test/development hook; the next verify_once() re-runs the audit."""
    _VERIFIED.clear()
