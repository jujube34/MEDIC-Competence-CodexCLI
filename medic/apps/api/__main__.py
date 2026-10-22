import argparse
import json
import os
from pathlib import Path
import uvicorn
from medic.apps.api.app import create_app
from medic.apps.api.uploads import UploadPolicy


def main(argv=None):
    parser=argparse.ArgumentParser(description='Loopback-only MEDIC synthetic verification API')
    database=parser.add_mutually_exclusive_group(required=True)
    database.add_argument('--database',help='SQLite path or PostgreSQL URI; prefer --database-env for credentials')
    database.add_argument('--database-env',help='Environment variable containing the database location')
    parser.add_argument('--vault',required=True)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--upload-policy',help='Local JSON upload limits; must match the policy already bound to this vault')
    args=parser.parse_args(argv)
    if not 1024<=args.port<=65535: parser.error('port must be 1024..65535')
    location=os.environ.get(args.database_env) if args.database_env else args.database
    if not location or not location.strip(): parser.error('database configuration is empty')
    try:
        policy=None
        if args.upload_policy:
            with Path(args.upload_policy).open('rb') as source:
                raw=source.read(4097)
            if len(raw)>4096: raise ValueError('invalid_upload_policy')
            policy=UploadPolicy(**json.loads(raw))
        app=create_app(location,Path(args.vault),upload_policy=policy)
    except Exception as error:
        safe_codes={'invalid_database_location','unsupported_database_connection_format',
                    'runtime_release_files_changed','runtime_process_requires_restart',
                    'invalid_upload_policy','upload_policy_mismatch'}
        print(json.dumps({'event':'api_start_failed','error_type':type(error).__name__,
                          'error_code':str(error) if isinstance(error,ValueError) and str(error) in safe_codes else None}),flush=True)
        return 2
    uvicorn.run(app,host='127.0.0.1',port=args.port,
                access_log=False,proxy_headers=False)
    return 0


if __name__=='__main__': raise SystemExit(main())
