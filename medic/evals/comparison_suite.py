"""Serial synthetic A/B/C campaign with immutable slots and fail-closed replay."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

from medic.domain.contracts import AssessmentInput
from medic.domain.graph import required_graph
from medic.evals import baseline_a as a,baseline_b as b,comparison_runtime as c
from medic.evals.comparison import _read,_write_private,digest,schedule,compare,validate_observations
from medic.runtime import build
from medic.runtime.service import SupervisorLock
from medic.runtime.service_process import ProcessJob


def require(condition,code):
    if not condition: raise ValueError('comparison_suite_'+code)


class RequestBudgetInfeasible(ValueError):
    def __init__(self,minimum,configured):
        super().__init__('comparison_suite_c_request_budget_infeasible')
        self.minimum,self.configured=minimum,configured


def preflight(experiment,sources,c_limits):
    rows=schedule(experiment);samples={r['sample_id'] for r in rows}
    require(set(sources)==samples and set(c_limits)==samples,'sample_map_mismatch')
    bindings=[];feasibility={}
    for row in rows:
        sample=row['sample_id'];source=sources[sample];arm=row['strategy']
        if arm=='multi_agent':
            bound=c.binding(experiment,source,sample,row['repeat'],c_limits[sample])
            # Every mandatory role has its own real model turn. Even an empty
            # graph still has a Coordinator. Tool continuations/rework cost more;
            # this necessary floor is not a sufficient budget or duration estimate.
            tasks=len(required_graph(AssessmentInput.model_validate(bound['payload']['source'])))
            configured=bound['payload']['budget_limits']['requests']
            if configured<tasks+1: raise RequestBudgetInfeasible(tasks+1,configured)
            feasibility[sample]={'mandatory_downstream_tasks':tasks,'minimum_model_requests':tasks+1,
                                 'configured_requests':configured,'interpretation':'necessary_lower_bound_only'}
        else: bound=(a if arm=='single_agent' else b).bind(experiment,source,sample,row['repeat'])
        bindings.append(bound)
    body={'experiment_sha256':experiment['sha256'],'bindings':bindings,'c_request_feasibility':feasibility,
          'controller_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return {'sha256':digest(body),'payload':body}


def _baseline_folder(experiment,row):
    name='comparison-a' if row['strategy']=='single_agent' else 'comparison-b'
    return build.ROOT/'artifacts'/name/digest([experiment['sha256'],row['sample_id'],row['strategy'],row['repeat']])


def _launch_c(folder,run_id,*,binary,api_example,lock):
    """Reuse the runtime's start handshake and Windows process-tree ownership."""
    require(hashlib.sha256(Path(binary).read_bytes()).hexdigest()==_read(build.ROOT/'p0/results/baseline.lock.json')['binary']['sha256'],'binary_mismatch')
    job=ProcessJob();process=None
    try:
        with (folder/'suite-process.log').open('xb') as log:
            process=subprocess.Popen([sys.executable,'-m','medic.runtime.service_child',job.name,
                '--resume-run',run_id,'--binary',str(Path(binary).resolve()),
                '--api-example',str(Path(api_example).resolve()),'--output',str(folder.resolve())],
                cwd=build.ROOT.parent,env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'},
                stdin=subprocess.PIPE,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process);process.stdin.write(b'start\n');process.stdin.close()
            while process.poll() is None:
                lock.check();time.sleep(0.5)
            require(process.returncode in (0,2),'runtime_process_incomplete')
    finally:
        job.close()
        if process is not None:
            if process.stdin and not process.stdin.closed: process.stdin.close()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill();process.wait(timeout=5)


def execute_slot(experiment,source,row,limits,mode,*,binary,api_example,lock):
    """mode is execute, interrupted, or replay; latter two cannot start new work."""
    arm=row['strategy'];sample=row['sample_id'];repeat=row['repeat']
    if arm!='multi_agent':
        folder=_baseline_folder(experiment,row)
        if mode!='execute':
            require((folder/'ledger.sqlite3').is_file(),'missing_baseline_ledger')
            with closing(sqlite3.connect((folder/'ledger.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                state=db.execute('SELECT state FROM session').fetchone()[0]
            require(state in ('completed','failed','interrupted') or mode=='interrupted' and state=='running','baseline_not_terminal')
        return (a if arm=='single_agent' else b).run(experiment,source,sample,repeat,
            binary=binary,api_example=api_example)['observation']
    folder=c.workspace(experiment,sample,repeat)
    if mode!='execute' or folder.exists():
        # Collect only: missing terminal artifacts require explicit operator recovery
        # of this same run, never automatic fresh execution or a new budget.
        require((folder/'run.json').is_file(),'c_terminal_artifacts_missing')
        return c.collect(folder)['observation']
    prepared=c.prepare(experiment,source,sample,repeat,limits)
    with SupervisorLock(SimpleNamespace(postgres=False,path=str(folder/'runtime.sqlite3'))) as run_lock:
        lock.check();_launch_c(folder,prepared['run_id'],binary=binary,api_example=api_example,lock=run_lock)
    return c.collect(folder)['observation']


def run(experiment,sources,c_limits,*,binary,api_example,root=None,dispatch=None,progress=None):
    """Persist each observation before advancing; interrupted C needs explicit recovery."""
    sealed=preflight(experiment,sources,c_limits);rows=schedule(experiment)
    root=Path(root) if root is not None else build.ROOT/'artifacts/comparison-suites'
    folder=root/experiment['sha256'];folder.mkdir(parents=True,exist_ok=True)
    dispatch=dispatch or execute_slot
    with SupervisorLock(SimpleNamespace(postgres=False,path=str(folder/'campaign'))) as lock:
        c._save_once(folder/'binding.json',sealed);c._save_once(folder/'experiment.json',experiment)
        # Enforce a contiguous prefix; do not silently skip over unknown work.
        completed=[(folder/f'slot-{i:04d}.json').exists() for i in range(len(rows))]
        require(completed==sorted(completed,reverse=True),'nonprefix_checkpoints')
        observations=[]
        for index,row in enumerate(rows):
            lock.check();target=folder/f'slot-{index:04d}.json';started=folder/f'started-{index:04d}.json'
            slot={k:v for k,v in row.items() if k!='input'}
            mode='replay' if target.exists() else 'interrupted' if started.exists() else 'execute'
            c._save_once(started,{'campaign_sha256':sealed['sha256'],'slot':slot})
            if progress: progress({'index':index,'strategy':row['strategy'],'repeat':row['repeat'],'mode':mode})
            value=dispatch(experiment,sources[row['sample_id']],row,c_limits[row['sample_id']],mode,
                           binary=binary,api_example=api_example,lock=lock)
            validate_observations(experiment,[*observations,value])
            require(all(value.get(k)==v for k,v in slot.items()),'returned_slot_mismatch')
            record={'campaign_sha256':sealed['sha256'],'slot':slot,'observation':value}
            c._save_once(target,record)
            observations.append(value)
            c._save_once(folder/f'report-{index+1:04d}.json',compare(experiment,observations))
        report=compare(experiment,observations)
        c._save_once(folder/'observations.json',observations);c._save_once(folder/'comparison-report.json',report)
    return {'folder':str(folder),'observations':observations,'report':report}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('experiment','sources','c-limits','binary','api-example'): parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    try:
        result=run(_read(args.experiment),_read(args.sources),_read(args.c_limits),binary=args.binary,api_example=args.api_example,
                   progress=lambda value:print(json.dumps(value),flush=True))
        print(json.dumps({'folder':result['folder'],'observed_runs':len(result['observations']),
                          'quality_acceptance':False}),flush=True)
        return 0
    except Exception as exc:
        result={'status':'stopped','error_type':type(exc).__name__}
        if isinstance(exc,RequestBudgetInfeasible):
            result.update(error_code=str(exc),minimum_requests=exc.minimum,configured_requests=exc.configured)
        print(json.dumps(result),flush=True);return 2


if __name__=='__main__': raise SystemExit(main())
