"""Native PostgreSQL public-schema backup plus the immutable MEDIC material vault.

Restores only to a newly created, explicitly named database. Credentials travel
in process environment, never command arguments or public verification output.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from medic.deploy.backup import (BLOB, blob_specs, copy_new, file_info, fresh_private,
                                 plain_path, snapshot_db, write_new)
from medic.p0.common import digest, now
from medic.runtime import build

FORMAT = 'medic-postgres-backup-v1'
ENV_KEYS = {'host':'PGHOST', 'hostaddr':'PGHOSTADDR', 'port':'PGPORT', 'user':'PGUSER',
            'password':'PGPASSWORD', 'dbname':'PGDATABASE', 'sslmode':'PGSSLMODE',
            'sslrootcert':'PGSSLROOTCERT', 'sslcert':'PGSSLCERT', 'sslkey':'PGSSLKEY',
            'connect_timeout':'PGCONNECT_TIMEOUT', 'channel_binding':'PGCHANNELBINDING',
            'target_session_attrs':'PGTARGETSESSIONATTRS', 'application_name':'PGAPPNAME'}


def connection_env(dsn):
    values = conninfo_to_dict(dsn)
    if set(values) - set(ENV_KEYS) or not all(values.get(k) for k in ('host','port','user','dbname')):
        raise ValueError('explicit_supported_postgres_connection_required')
    # Ambient libpq settings must not silently redirect the dump or restore.
    env = {k:v for k,v in os.environ.items() if not k.upper().startswith('PG')}
    env.update({ENV_KEYS[k]:v for k,v in values.items()})
    env.setdefault('PGCONNECT_TIMEOUT', '5')
    return env


def pg_tool(bin_dir, name, args, *, dsn=None, error_folder=None):
    binary = plain_path(Path(bin_dir) / (name + ('.exe' if os.name=='nt' else '')))
    if not binary.is_file(): raise ValueError('postgres_binary_required')
    result = subprocess.run([str(binary), *args], env=connection_env(dsn) if dsn else None,
                            capture_output=True, timeout=180,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode:
        if error_folder:
            with (Path(error_folder) / (name+'-error.log')).open('xb') as log:
                log.write(result.stderr)
        raise RuntimeError(name+'_failed')
    return result.stdout


def connect(dsn, *, autocommit=False):
    connection_env(dsn)  # Validate the same connection vocabulary as native tools.
    return psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row, autocommit=autocommit)


def table_state(connection):
    tables = [r['tablename'] for r in connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    state = {}
    for name in tables:
        cursor = connection.execute(sql.SQL('SELECT * FROM public.{}').format(sql.Identifier(name)))
        columns = [c.name for c in cursor.description]
        rows = sorted(digest([row[c] for c in columns]) for row in cursor)
        state[name] = {'rows':len(rows), 'sha256':digest({'columns':columns,'row_hashes':rows})}
    return state


def inspect_references(connection, vault_db):
    snapshots, blobs = blob_specs(vault_db)
    materials = {r['snapshot_id']:r for r in connection.execute('SELECT * FROM public.app_materials')}
    for ident, row in materials.items():
        snap = snapshots.get(ident)
        if not snap or snap['scope']!=digest([row['tenant'],row['employee_ref']]) or snap['period']!=row['period']:
            raise ValueError('application_material_reference_broken')
    def check_source(source):
        for material in source['materials']:
            ident = material.get('snapshot_ref')
            if not ident: continue
            snap = snapshots.get(ident); row = materials.get(ident)
            if (not snap or not row or any(row[k]!=source[k] for k in ('employee_ref','period'))
                    or row['tenant']!=source['tenant_id'] or snap['source_sha256']!=material.get('source_sha256')):
                raise ValueError('assessment_material_reference_broken')
    runs = {}
    for row in connection.execute('SELECT run_id,input_json,input_hash FROM public.runs'):
        value = json.loads(row['input_json'])
        if digest(value)!=row['input_hash']: raise ValueError('run_input_integrity_failed')
        check_source(value); runs[row['run_id']] = row['input_hash']
    assessments = {}
    for row in connection.execute('SELECT * FROM public.app_assessments'):
        value = json.loads(row['source_json']); check_source(value)
        if row['run_id'] and runs.get(row['run_id'])!=digest(value):
            raise ValueError('application_run_reference_broken')
        assessments[row['assessment_id']] = row
    decisions = 0
    for row in connection.execute('SELECT assessment_id,value_json FROM public.app_decisions'):
        decisions += 1; assessment = assessments.get(row['assessment_id'])
        if not assessment: raise ValueError('review_assessment_reference_broken')
        for ident in json.loads(row['value_json']).get('supplemental_material_ids',[]):
            material = materials.get(ident)
            if not material or any(material[k]!=assessment[k] for k in ('tenant','employee_ref','period')):
                raise ValueError('supplemental_material_reference_broken')
    return {'runs':len(runs),'snapshots':len(snapshots),'blobs':len(blobs),'materials':len(materials),
            'assessments':len(assessments),'human_decisions':decisions,
            'application_events':connection.execute('SELECT count(*) AS n FROM public.app_events').fetchone()['n']}


def create(dsn, vault, destination, bin_dir):
    connection_env(dsn)
    vault = plain_path(vault)
    if not (vault/'snapshots.sqlite3').is_file(): raise ValueError('vault_required')
    if plain_path(destination).is_relative_to(vault): raise ValueError('backup_destination_overlaps_vault')
    identity = build.verify(build.VERSION)
    destination = fresh_private(destination); (destination/'vault').mkdir()
    with connect(dsn) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        connection.execute("SET LOCAL statement_timeout='120s'")
        if connection.execute("SELECT 1 FROM pg_tables WHERE schemaname NOT IN ('public','information_schema') AND schemaname NOT LIKE 'pg_%' LIMIT 1").fetchone():
            raise ValueError('non_public_tables_not_supported')
        if connection.execute('SELECT 1 FROM pg_largeobject_metadata LIMIT 1').fetchone():
            raise ValueError('large_objects_not_supported')
        snapshot = connection.execute('SELECT pg_export_snapshot() AS id').fetchone()['id']
        tables = table_state(connection)
        required = {'runs','tasks','results','run_limits','run_builds','model_requests','app_users','app_tokens',
                    'app_employees','app_grants','app_catalogs','app_materials','app_assessments','app_decisions','app_events'}
        if not required<=set(tables): raise ValueError('application_schema_required')
        pg_tool(bin_dir,'pg_dump',['--format=custom','--schema=public','--strict-names',
            '--no-owner','--no-privileges','--no-tablespaces','--no-password','--lock-wait-timeout=10s',
            '--snapshot='+snapshot,'--file='+str(destination/'runtime.dump')],dsn=dsn,error_folder=destination)
        # Runtime references in the exported snapshot precede these append-only
        # vault rows; concurrent uploads cannot make an older reference disappear.
        snapshot_db(vault/'snapshots.sqlite3',destination/'vault/snapshots.sqlite3')
        counts = inspect_references(connection,destination/'vault/snapshots.sqlite3')
        server_version = connection.info.server_version
    _, blobs = blob_specs(destination/'vault/snapshots.sqlite3')
    for name, expected in blobs.items():
        copy_new(vault/PurePosixPath(name).relative_to('vault'),destination/name)
        if file_info(destination/name)!=expected: raise ValueError('backup_blob_integrity_failed')
    names = ['runtime.dump','vault/snapshots.sqlite3',*sorted(blobs)]
    tools = {name:{'sha256':file_info(Path(bin_dir)/(name+('.exe' if os.name=='nt' else '')))['sha256'],
                   'version':pg_tool(bin_dir,name,['--version']).decode().strip()}
             for name in ('pg_dump','pg_restore')}
    manifest = {'format':FORMAT,'created_at':now(),'captured_build':identity,
                'backup_tool_sha256':file_info(__file__)['sha256'],'native_tools':tools,
                'server_version':server_version,'files':{n:file_info(destination/n) for n in names},
                'tables':tables,'counts':counts,'consistency':'exported_repeatable_read_snapshot_before_append_only_vault',
                'includes_external_protocol_archives':False,'includes_cluster_roles_and_database_acl':False}
    if build.verify(build.VERSION)!=identity: raise ValueError('backup_build_changed')
    write_new(destination/'manifest.json',manifest)
    return verify_backup(destination,bin_dir)


def verify_backup(folder, bin_dir):
    folder = plain_path(folder)
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!=FORMAT or not isinstance(manifest.get('files'),dict):
        raise ValueError('unsupported_backup_format')
    files = manifest['files']
    if not {'runtime.dump','vault/snapshots.sqlite3'}<=set(files): raise ValueError('incomplete_backup')
    for name, expected in files.items():
        if name not in ('runtime.dump','vault/snapshots.sqlite3') and not BLOB.fullmatch(name):
            raise ValueError('invalid_backup_member')
        if file_info(folder/name)!=expected: raise ValueError('backup_member_integrity_failed')
    actual = set()
    for path in folder.rglob('*'):
        plain_path(path)
        if path.is_file(): actual.add(path.relative_to(folder).as_posix())
    if actual!=set(files)|{'manifest.json'}: raise ValueError('unexpected_backup_member')
    _, blobs = blob_specs(folder/'vault/snapshots.sqlite3')
    if set(files)!={'runtime.dump','vault/snapshots.sqlite3'}|set(blobs):
        raise ValueError('backup_manifest_data_mismatch')
    for name, info in blobs.items():
        if files[name]!=info: raise ValueError('backup_blob_metadata_mismatch')
    # Native archive parsing, without connecting to or modifying a database.
    pg_tool(bin_dir,'pg_restore',['--list',str(folder/'runtime.dump')])
    return manifest


def restore(folder, maintenance_dsn, database_name, destination, bin_dir):
    folder = plain_path(folder)
    if not re.fullmatch(r'medic_restore_[a-z0-9_]{1,45}',database_name):
        raise ValueError('fresh_restore_database_name_required')
    if plain_path(destination).is_relative_to(folder): raise ValueError('restore_destination_overlaps_backup')
    connection_env(maintenance_dsn)
    manifest = verify_backup(folder,bin_dir)
    if manifest['captured_build']!=build.verify(build.VERSION):
        raise ValueError('restore_requires_captured_build_and_environment')
    target_dsn = make_conninfo(maintenance_dsn,dbname=database_name)
    with connect(maintenance_dsn,autocommit=True) as admin:
        if admin.execute('SELECT 1 FROM pg_database WHERE datname=%s',(database_name,)).fetchone():
            raise ValueError('restore_database_already_exists')
        destination = fresh_private(destination)
        # Verify copied data before creating a new DB. Source is never changed.
        for name, expected in manifest['files'].items():
            copy_new(folder/name,destination/name)
            if file_info(destination/name)!=expected: raise ValueError('backup_changed_during_restore')
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(database_name)))
        admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database_name)))
    # template0 supplies an empty public schema, while this archive contains its
    # own CREATE SCHEMA. Remove only that fresh empty schema; no CASCADE and no
    # cleanup of an existing database are permitted.
    with connect(target_dsn) as connection:
        connection.execute('DROP SCHEMA public')
    # No --clean or --create: the new empty DB must be the only restore target.
    pg_tool(bin_dir,'pg_restore',['--exit-on-error','--single-transaction','--no-owner','--no-privileges',
        '--no-tablespaces','--no-password','--dbname='+database_name,str(destination/'runtime.dump')],
        dsn=target_dsn,error_folder=destination)
    with connect(target_dsn) as connection:
        if table_state(connection)!=manifest['tables']:
            raise ValueError('restored_table_data_mismatch')
        counts = inspect_references(connection,destination/'vault/snapshots.sqlite3')
        if counts!=manifest['counts']: raise ValueError('restored_reference_counts_mismatch')
        revoked = connection.execute('SELECT count(*) AS n FROM public.app_tokens').fetchone()['n']
        connection.execute('DELETE FROM public.app_tokens')
    result = {'format':FORMAT,'restored_at':now(),'database_name':database_name,
              'backup_manifest_sha256':file_info(folder/'manifest.json')['sha256'],'counts':counts,
              'all_table_hashes_verified_before_session_invalidation':True,'copied_sessions_invalidated':revoked,
              'public_connect_revoked':True,'services_started':False,'external_protocol_archives_restored':False}
    write_new(destination/'RESTORED.json',result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir',required=True)
    sub = parser.add_subparsers(dest='action',required=True)
    capture = sub.add_parser('create'); capture.add_argument('--database-env',required=True)
    capture.add_argument('--vault',required=True); capture.add_argument('--output',required=True)
    check = sub.add_parser('verify'); check.add_argument('folder')
    recover = sub.add_parser('restore'); recover.add_argument('folder')
    recover.add_argument('--maintenance-env',required=True); recover.add_argument('--database-name',required=True)
    recover.add_argument('--output',required=True)
    args = parser.parse_args()
    if args.action=='create':
        dsn=os.environ.get(args.database_env)
        if not dsn: parser.error('database environment variable is empty')
        result=create(dsn,args.vault,args.output,args.bin_dir)
    elif args.action=='verify': result=verify_backup(args.folder,args.bin_dir)
    else:
        dsn=os.environ.get(args.maintenance_env)
        if not dsn: parser.error('maintenance environment variable is empty')
        result=restore(args.folder,dsn,args.database_name,args.output,args.bin_dir)
    print(json.dumps({'status':'pass','action':args.action,'counts':result['counts']}))


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        # Native stderr is retained only in a private failure directory. Do not
        # echo connection diagnostics, DSNs or credentials to the console.
        print(json.dumps({'status':'fail','error_type':type(error).__name__}))
        raise SystemExit(2) from None
