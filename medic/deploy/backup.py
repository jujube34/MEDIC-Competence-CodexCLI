"""Consistent local SQLite application/vault backups and restore to a new directory.

Runtime metadata is captured before the append-only vault. Every referenced blob
is verified. Restore never replaces a deployment and invalidates copied sessions.
PostgreSQL, external protocol archives and production retention are separate work.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat

from medic.ingestion.contracts import Extraction
from medic.p0.common import digest, now
from medic.runtime import build

FORMAT = 'medic-sqlite-backup-v1'
HEX = re.compile(r'[0-9a-f]{64}')
BLOB = re.compile(r'vault/[0-9a-f]{64}/blobs/[0-9a-f]{64}')


def plain_path(path):
    """Do not follow symlinks or Windows junctions in any supplied path."""
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('backup_reparse_path_rejected')
    return path


def fresh_private(path):
    path = plain_path(path)
    if not any(path.is_relative_to(build.ROOT / p) for p in ('artifacts', 'private', 'var')):
        raise ValueError('backup_output_requires_private_directory')
    path.mkdir(parents=True, exist_ok=False)
    return path


def file_info(path):
    path = plain_path(path)
    h = hashlib.sha256(); size = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(block); h.update(block)
    return {'sha256': h.hexdigest(), 'bytes': size}


def read_db(path):
    path = plain_path(path)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def snapshot_db(source, target):
    # sqlite3 backup includes committed WAL contents; copying the main file alone
    # would lose them. A read transaction pins the snapshot across backup steps.
    with closing(read_db(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.execute('BEGIN')
        src.execute('SELECT count(*) FROM sqlite_master').fetchone()
        src.backup(dst, pages=256)
        dst.execute('PRAGMA journal_mode=DELETE')
        dst.commit()


def blob_specs(vault_db):
    records = {}; blobs = {}
    with closing(read_db(vault_db)) as db:
        if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok']:
            raise ValueError('vault_database_integrity_failed')
        for row in db.execute('SELECT * FROM snapshots'):
            value = json.loads(row['payload'])
            if (digest(value) != row['sha256'] or value['snapshot_id'] != row['id']
                    or value['scope'] != row['scope'] or not HEX.fullmatch(row['scope'])
                    or not HEX.fullmatch(value['source_sha256'])
                    or type(value['source_bytes']) is not int or value['source_bytes'] < 0):
                raise ValueError('snapshot_integrity_failed')
            extraction = Extraction.model_validate(value['extraction'])
            if extraction.source_sha256 != value['source_sha256']:
                raise ValueError('extraction_source_mismatch')
            name = f"vault/{row['scope']}/blobs/{value['source_sha256']}"
            info = {'sha256': value['source_sha256'], 'bytes': value['source_bytes']}
            if name in blobs and blobs[name] != info:
                raise ValueError('conflicting_blob_metadata')
            blobs[name] = info; records[row['id']] = value
    return records, blobs


def inspect_data(root):
    """Integrity and referential closure without instantiating a mutating Store."""
    root = plain_path(root)
    snapshots, blobs = blob_specs(root / 'vault/snapshots.sqlite3')
    for name, info in blobs.items():
        if file_info(root / name) != info:
            raise ValueError('backup_blob_integrity_failed')
    with closing(read_db(root / 'runtime.sqlite3')) as db:
        if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok'] or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('runtime_database_integrity_failed')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'runs', 'tasks', 'results', 'run_limits', 'model_requests', 'run_builds',
                    'app_users', 'app_tokens', 'app_employees', 'app_grants', 'app_catalogs',
                    'app_materials', 'app_assessments', 'app_decisions', 'app_events'}
        if not required <= tables:
            raise ValueError('application_schema_required')
        materials = {r['snapshot_id']: dict(r) for r in db.execute('SELECT * FROM app_materials')}
        for ident, row in materials.items():
            snap = snapshots.get(ident)
            if (not snap or snap['scope'] != digest([row['tenant'], row['employee_ref']])
                    or snap['period'] != row['period']):
                raise ValueError('application_material_reference_broken')
        def check_source(source):
            for material in source['materials']:
                ref = material.get('snapshot_ref')
                if not ref:  # Synthetic text-only CLI runs do not have vault files.
                    continue
                snap = snapshots.get(ref); row = materials.get(ref)
                if (not snap or not row or row['tenant'] != source['tenant_id']
                        or row['employee_ref'] != source['employee_ref'] or row['period'] != source['period']
                        or snap['source_sha256'] != material.get('source_sha256')):
                    raise ValueError('assessment_material_reference_broken')
        runs = {}
        for row in db.execute('SELECT run_id,input_json,input_hash FROM runs'):
            source = json.loads(row['input_json'])
            if digest(source) != row['input_hash']:
                raise ValueError('run_input_integrity_failed')
            check_source(source); runs[row['run_id']] = row['input_hash']
        for row in db.execute('SELECT * FROM app_assessments'):
            source = json.loads(row['source_json']); check_source(source)
            if row['run_id'] and runs.get(row['run_id']) != digest(source):
                raise ValueError('application_run_reference_broken')
        for row in db.execute('SELECT assessment_id,value_json FROM app_decisions'):
            value = json.loads(row['value_json'])
            assessment = db.execute('SELECT tenant,employee_ref,period FROM app_assessments WHERE assessment_id=?', (row['assessment_id'],)).fetchone()
            if not assessment:
                raise ValueError('review_assessment_reference_broken')
            for ident in value.get('supplemental_material_ids', []):
                item = materials.get(ident)
                if not item or any(item[k] != assessment[k] for k in ('tenant', 'employee_ref', 'period')):
                    raise ValueError('supplemental_material_reference_broken')
        return {'runs': len(runs), 'snapshots': len(snapshots), 'blobs': len(blobs),
                'materials': len(materials), 'assessments': db.execute('SELECT count(*) FROM app_assessments').fetchone()[0],
                'human_decisions': db.execute('SELECT count(*) FROM app_decisions').fetchone()[0],
                'application_events': db.execute('SELECT count(*) FROM app_events').fetchone()[0]}


def copy_new(source, target):
    source = plain_path(source); target = plain_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush(); os.fsync(dst.fileno())


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def create(database, vault, destination):
    if str(database).startswith(('postgres://', 'postgresql://')):
        raise ValueError('postgresql_backup_not_implemented')
    database = plain_path(database); vault = plain_path(vault)
    if not database.is_file() or not (vault / 'snapshots.sqlite3').is_file():
        raise ValueError('application_and_vault_required')
    if plain_path(destination).is_relative_to(vault):
        raise ValueError('backup_destination_overlaps_vault')
    identity = build.verify(build.VERSION)
    destination = fresh_private(destination)
    # A failed/incomplete directory has no manifest and is never restorable.
    (destination / 'vault').mkdir()
    snapshot_db(database, destination / 'runtime.sqlite3')
    # Vault rows/blobs are append-only. Capture it after runtime metadata so all
    # material references already committed to runtime are included.
    snapshot_db(vault / 'snapshots.sqlite3', destination / 'vault/snapshots.sqlite3')
    _, blobs = blob_specs(destination / 'vault/snapshots.sqlite3')
    for name, expected in blobs.items():
        source = vault / PurePosixPath(name).relative_to('vault')
        copy_new(source, destination / name)
        if file_info(destination / name) != expected:
            raise ValueError('backup_blob_integrity_failed')
    counts = inspect_data(destination)
    names = ['runtime.sqlite3', 'vault/snapshots.sqlite3', *sorted(blobs)]
    files = {name: file_info(destination / name) for name in names}
    manifest = {'format': FORMAT, 'created_at': now(), 'captured_build': identity, 'files': files,
                'backup_tool_sha256': file_info(__file__)['sha256'],
                'counts': counts, 'consistency': 'runtime_snapshot_before_append_only_vault',
                'includes_external_protocol_archives': False, 'includes_provider_credentials': False}
    if build.verify(build.VERSION) != identity:
        raise ValueError('backup_build_changed')
    write_new(destination / 'manifest.json', manifest)
    return verify_backup(destination)


def verify_backup(folder):
    folder = plain_path(folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != FORMAT or not isinstance(manifest.get('files'), dict):
        raise ValueError('unsupported_backup_format')
    files = manifest['files']
    if not {'runtime.sqlite3', 'vault/snapshots.sqlite3'} <= set(files):
        raise ValueError('incomplete_backup')
    for name, expected in files.items():
        if name not in ('runtime.sqlite3', 'vault/snapshots.sqlite3') and not BLOB.fullmatch(name):
            raise ValueError('invalid_backup_member')
        if file_info(folder / name) != expected:
            raise ValueError('backup_member_integrity_failed')
    actual = set()
    for path in folder.rglob('*'):
        plain_path(path)
        if path.is_file():
            actual.add(path.relative_to(folder).as_posix())
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('unexpected_backup_member')
    counts = inspect_data(folder)
    _, blobs = blob_specs(folder / 'vault/snapshots.sqlite3')
    if set(files) != {'runtime.sqlite3', 'vault/snapshots.sqlite3'} | set(blobs) or counts != manifest.get('counts'):
        raise ValueError('backup_manifest_data_mismatch')
    return manifest


def restore(folder, destination):
    folder = plain_path(folder)
    if plain_path(destination).is_relative_to(folder):
        raise ValueError('restore_destination_overlaps_backup')
    manifest = verify_backup(folder)
    identity = build.verify(build.VERSION)
    if manifest['captured_build'] != identity:
        raise ValueError('restore_requires_captured_build_and_environment')
    destination = fresh_private(destination)
    for name, expected in manifest['files'].items():
        copy_new(folder / name, destination / name)
        if file_info(destination / name) != expected:
            raise ValueError('backup_changed_during_restore')
    counts = inspect_data(destination)
    # Authorization grants/users/history remain identical. Bearer sessions are
    # invalidated so restoring an older backup cannot resurrect a revoked token.
    with closing(sqlite3.connect(destination / 'runtime.sqlite3')) as db, db:
        revoked = db.execute('SELECT count(*) FROM app_tokens').fetchone()[0]
        db.execute('DELETE FROM app_tokens')
    result = {'format': FORMAT, 'restored_at': now(), 'backup_manifest_sha256': file_info(folder / 'manifest.json')['sha256'],
              'counts': counts, 'copied_sessions_invalidated': revoked,
              'services_started': False, 'external_protocol_archives_restored': False}
    write_new(destination / 'RESTORED.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    backup = sub.add_parser('create')
    backup.add_argument('--database', required=True); backup.add_argument('--vault', required=True)
    backup.add_argument('--output', required=True)
    check = sub.add_parser('verify'); check.add_argument('folder')
    recovery = sub.add_parser('restore'); recovery.add_argument('folder'); recovery.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.action == 'create':
        result = create(args.database, args.vault, args.output)
    elif args.action == 'verify':
        result = verify_backup(args.folder)
    else:
        result = restore(args.folder, args.output)
    print(json.dumps({'status': 'pass', 'action': args.action, 'counts': result['counts']}))


if __name__ == '__main__':
    main()
