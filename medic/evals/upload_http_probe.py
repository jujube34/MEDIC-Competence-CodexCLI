"""Local synthetic-only upload contention across two actual API processes.

Does not invoke any model or seed reports. Reads existing private trial access
configuration in memory; never logs credentials. The primary API must be running.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

from medic.runtime import build
from medic.runtime.service_process import ProcessJob
from medic.p0.common import digest


def run(access_file, output, primary=8765, secondary=8766):
    output=Path(output).resolve()
    if not any(output.is_relative_to(build.ROOT/name) for name in ('artifacts','private','var')):
        raise ValueError('private_output_required')
    output.mkdir(parents=True,exist_ok=False)
    access=json.loads(Path(access_file).read_text(encoding='utf-8'))
    if access.get('synthetic') is not True:raise ValueError('synthetic_only')
    identity=build.verify(build.VERSION)
    vault=Path(access['vault']);db_path=vault/'snapshots.sqlite3'
    streams=[];child=None;job=ProcessJob()
    checks={}

    def usage():
        with closing(sqlite3.connect(db_path)) as db:
            return {r[0]:(r[1],r[2]) for r in db.execute('SELECT id,bytes,settled FROM upload_attempts')}

    def active_new(before):
        return {k:v for k,v in usage().items() if k not in before and not v[1]}

    def wait_for(condition):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if condition():return
            time.sleep(.05)
        raise AssertionError('probe_condition_timeout')

    def partial(port,role):
        stream=socket.create_connection(('127.0.0.1',port),timeout=3)
        request=(f"POST /employees/{access['employee_ref']}/materials?filename=synthetic.docx&period=ui-v8-probe HTTP/1.1\r\n"
                 f"Host: 127.0.0.1:{port}\r\nAuthorization: Bearer {access['tokens'][role]}\r\n"
                 "Content-Length: 100\r\nConnection: close\r\n\r\nx")
        stream.sendall(request.encode('ascii'));streams.append(stream)

    def rejected(port,role):
        url=f"http://127.0.0.1:{port}/employees/{access['employee_ref']}/materials?filename=synthetic.docx&period=ui-v8-probe"
        request=urllib.request.Request(url,data=b'x',headers={'Authorization':'Bearer '+access['tokens'][role]},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=5) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as response:return response.code,json.load(response)

    try:
        with (output/'secondary-api.log').open('xb') as log:
            code='import sys;from medic.runtime.service_process import ProcessJob;j=ProcessJob(sys.argv.pop(1));j.close();from medic.apps.api.__main__ import main;raise SystemExit(main())'
            child=subprocess.Popen([sys.executable,'-c',code,job.name,'--database',access['database'],'--vault',str(vault),'--port',str(secondary)],stdout=log,stderr=subprocess.STDOUT)
        def healthy():
            if child.poll() is not None:raise AssertionError('secondary_api_exited')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{secondary}/health',timeout=.5) as r:return r.status==200
            except OSError:return False
        wait_for(healthy)
        before=usage()
        partial(primary,'employee');wait_for(lambda:len(active_new(before))==1)
        status,payload=rejected(secondary,'employee')
        checks['account_limit_across_processes']=status==429 and payload=={'error':'upload_concurrency_limit'}
        partial(secondary,'manager');wait_for(lambda:len(active_new(before))==2)
        status,payload=rejected(primary,'director')
        checks['global_limit_across_processes']=status==429 and payload=={'error':'upload_concurrency_limit'}
        checks['rejected_requests_do_not_consume_attempts']=len(set(usage())-set(before))==2
        for stream in streams:stream.close()
        streams.clear()
        wait_for(lambda:not active_new(before))
        after={k:v for k,v in usage().items() if k not in before}
        checks['disconnect_settles_received_bytes']=all(v==(1,1) for v in after.values()) and len(after)==2
        checks['temporary_files_cleaned']=not any((vault/'incoming').iterdir())
        partial(secondary,'employee');wait_for(lambda:len(active_new(before))==1)
        checks['slot_reusable_after_disconnect']=True
        for stream in streams:stream.close()
        streams.clear();wait_for(lambda:not active_new(before))
        checks['release_unchanged']=identity==build.verify(build.VERSION)
        result={'status':'pass' if all(checks.values()) else 'fail','synthetic':True,'model_calls':0,
                'execution_build':identity,'checks':checks,'attempts':len(set(usage())-set(before)),
                'scope':'Two local HTTP API processes; no parser-duration or production load claim.'}
        (output/'verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        if not all(checks.values()):raise AssertionError('upload_http_checks_failed')
        print(json.dumps({'status':'pass','checks':len(checks),'model_calls':0,'verification_sha256':digest(result)}))
        return result
    finally:
        for stream in streams:stream.close()
        job.close()
        if child is not None:
            child.wait(timeout=15)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--access',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();run(args.access,args.output)
