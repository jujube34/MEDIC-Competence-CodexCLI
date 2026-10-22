"""Offline frozen A/B/C experiment accounting; never executes or accepts a model.

Input provenance is declared by the importer. A hash detects changed content,
not expert authority, independent sessions, or authentic provider usage.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import platform
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from medic.evals.metrics import evaluate

STRATEGIES=('single_agent','linear_workflow','multi_agent')
CODES=set('M1 M2 M3 M4 E1 E2 E3 E4 D1 D2 D3 I1 I2 C1 C2 C3 C4'.split())
Text=Annotated[str,Field(min_length=1,max_length=200,pattern=r'^\S(?:.*\S)?$')]
Hash=Annotated[str,Field(pattern=r'^[0-9a-f]{64}$')]
Grade=Literal['A','B','C','D']
Count=Annotated[int,Field(ge=0)]


class Contract(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)


class Label(Contract):
    code: Text
    mode: Literal['suggest','clue_only','human_only','risk_scan']
    cap: Grade | None
    observable: bool
    grade: Grade | None

    @model_validator(mode='after')
    def scope(self):
        if self.code not in CODES: raise ValueError('unknown_competency')
        if self.mode!='suggest' and self.cap is not None: raise ValueError('non_scoring_cap')
        # Expert grades describe supported behavior; they may exceed the AI cap.
        if (self.grade is not None) != (self.observable and self.mode=='suggest'):
            raise ValueError('expert_grade_observability_mismatch')
        return self


class Sample(Contract):
    sample_id: Text
    employee_id: Text
    event_ids: list[Text]=Field(min_length=1)
    material_sha256: list[Hash]=Field(min_length=1)
    assessment_content_sha256: Hash | None=None
    split: Literal['development','test']
    job_key: Text
    labels: list[Label]=Field(min_length=1,max_length=17)
    independent_reviewers: list[Text]=Field(default_factory=list)
    adjudication_reference: Text | None=None

    @model_validator(mode='after')
    def unique(self):
        for values in (self.event_ids,self.material_sha256,self.independent_reviewers,[l.code for l in self.labels]):
            if len(values)!=len(set(values)): raise ValueError('duplicate_sample_field')
        return self


class Dataset(Contract):
    dataset_id: Text
    provenance: Literal['synthetic_fixture','authorized_deidentified_expert']
    competency_model_version: Text
    job_matrix_version: Text
    ai_policy_version: Text
    parser_version: Text
    samples: list[Sample]=Field(min_length=2)

    @model_validator(mode='after')
    def partition(self):
        seen=set(); groups={}; policy={}
        if {s.split for s in self.samples}!={'development','test'}: raise ValueError('both_splits_required')
        for sample in self.samples:
            if sample.sample_id in seen: raise ValueError('duplicate_sample_id')
            seen.add(sample.sample_id)
            for group in [('employee',sample.employee_id),*[('event',e) for e in sample.event_ids],
                          *[('material',h) for h in sample.material_sha256]]:
                if groups.setdefault(group,sample.split)!=sample.split: raise ValueError('split_leakage')
            if self.provenance=='authorized_deidentified_expert' and (
                    len(sample.independent_reviewers)<2 or not sample.adjudication_reference):
                raise ValueError('dual_review_and_adjudication_required')
            for label in sample.labels:
                value=(label.mode,label.cap)
                if policy.setdefault(label.code,value)!=value: raise ValueError('inconsistent_frozen_policy')
        return self


class Design(Contract):
    experiment_id: Text
    provider_profile_version: Text
    model: Text
    prompt_sha256: dict[str,Hash]
    implementation_sha256: dict[str,Hash]
    token_budget: dict[str,Annotated[int,Field(gt=0)]]
    budget_regime: Literal['matched_tokens','configured']
    repeats: int=Field(ge=2,le=100)
    order_seed: int=Field(ge=0,le=2**32-1)
    split: Literal['development','test']

    @model_validator(mode='after')
    def arms(self):
        if any(set(mapping)!=set(STRATEGIES) for mapping in (self.prompt_sha256,self.implementation_sha256,self.token_budget)):
            raise ValueError('three_strategies_required')
        if self.budget_regime=='matched_tokens' and len(set(self.token_budget.values()))!=1:
            raise ValueError('unequal_matched_budgets')
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def analysis_identity():
    from importlib.metadata import version
    return {'python':platform.python_version(),'pydantic':version('pydantic'),
        'files':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                 for name in ('comparison.py','metrics.py')}}


def freeze(dataset,design):
    data=Dataset.model_validate(dataset).model_dump()
    plan=Design.model_validate(design).model_dump()
    payload={'format':'medic-comparison-v1','dataset':data,'design':plan,'analysis':analysis_identity()}
    return {'sha256':digest(payload),'payload':payload}


def load_frozen(envelope):
    if set(envelope)!={'sha256','payload'} or digest(envelope['payload'])!=envelope['sha256']:
        raise ValueError('experiment_changed')
    payload=envelope['payload']
    if set(payload)!={'format','dataset','design','analysis'} or payload['format']!='medic-comparison-v1':
        raise ValueError('unsupported_comparison_format')
    if payload['analysis']!=analysis_identity(): raise ValueError('analysis_implementation_changed')
    # Canonical validation makes schema/default changes visible instead of rebinding an old experiment.
    if freeze(payload['dataset'],payload['design'])!=envelope: raise ValueError('noncanonical_experiment')
    return Dataset.model_validate(payload['dataset']),Design.model_validate(payload['design'])


def sample_input(dataset,sample):
    """Blind execution descriptor: deliberately excludes labels and review identities."""
    return {'sample_id':sample.sample_id,'employee_id':sample.employee_id,'job_key':sample.job_key,
            'assessment_content_sha256':sample.assessment_content_sha256,
            'event_ids':sample.event_ids,'material_sha256':sample.material_sha256,
            'scope':[{'code':l.code,'mode':l.mode,'cap':l.cap} for l in sample.labels],
            'versions':{key:getattr(dataset,key) for key in ('competency_model_version','job_matrix_version','ai_policy_version','parser_version')}}


def schedule(envelope):
    dataset,design=load_frozen(envelope)
    rows=[]
    for sample in dataset.samples:
        if sample.split!=design.split: continue
        descriptor=sample_input(dataset,sample)
        for repeat in range(1,design.repeats+1):
            for strategy in STRATEGIES:
                rows.append({'experiment_sha256':envelope['sha256'],'sample_id':sample.sample_id,
                    'strategy':strategy,'repeat':repeat,'input_sha256':digest(descriptor),'input':descriptor,
                    'prompt_sha256':design.prompt_sha256[strategy],'token_budget':design.token_budget[strategy],
                    'implementation_sha256':design.implementation_sha256[strategy],
                    'provider_profile_version':design.provider_profile_version,'model':design.model})
    random.Random(design.order_seed).shuffle(rows)
    return rows


class Prediction(Contract):
    code: Text
    grade: Grade | None
    evidence_valid: bool | None=None
    anchors_supported: bool | None=None


class Observation(Contract):
    experiment_sha256: Hash
    sample_id: Text
    strategy: Literal['single_agent','linear_workflow','multi_agent']
    repeat: int=Field(ge=1)
    input_sha256: Hash
    prompt_sha256: Hash
    implementation_sha256: Hash
    provider_profile_version: Text
    model: Text
    token_budget: int=Field(gt=0)
    execution_id: Text
    source_report_sha256: Hash
    status: Literal['completed','failed','cancelled']
    predictions: list[Prediction]
    total_tokens: Count | None=None
    model_requests: Count | None=None
    retries: Count | None=None
    elapsed_seconds: Annotated[float,Field(ge=0)] | None=None
    human_review_seconds: Annotated[float,Field(ge=0)] | None=None

    @model_validator(mode='after')
    def results(self):
        if len({p.code for p in self.predictions})!=len(self.predictions): raise ValueError('duplicate_prediction')
        if self.status!='completed' and self.predictions: raise ValueError('failed_run_has_final_predictions')
        return self


def _key(row): return (row['sample_id'],row['strategy'],row['repeat'])


def validate_observations(envelope,observations):
    expected={_key(r):r for r in schedule(envelope)}
    indexed={}; executions=set()
    for raw in observations:
        observation=Observation.model_validate(raw).model_dump()
        key=_key(observation)
        if key in indexed: raise ValueError('duplicate_observation')
        if key not in expected: raise ValueError('observation_outside_frozen_schedule')
        target=expected[key]
        for field in ('experiment_sha256','input_sha256','prompt_sha256','implementation_sha256','provider_profile_version','model','token_budget'):
            if observation[field]!=target[field]: raise ValueError('observation_identity_mismatch')
        if observation['execution_id'] in executions: raise ValueError('reused_execution_id')
        executions.add(observation['execution_id'])
        if {p['code'] for p in observation['predictions']}-{s['code'] for s in target['input']['scope']}:
            raise ValueError('prediction_outside_scope')
        indexed[key]=observation
    return indexed


def _percentile(values,p):
    if not values: return None
    values=sorted(values); position=(len(values)-1)*p; lo=math.floor(position); hi=math.ceil(position)
    return values[lo]+(values[hi]-values[lo])*(position-lo)


def _scores(samples,repeats,strategy,observed):
    labels=[]; predictions=[]
    for sample in samples:
        for repeat in repeats:
            output=observed.get((sample.sample_id,strategy,repeat))
            if output and output['status']=='completed':
                scope={l.code for l in sample.labels}
                predictions.extend({'case_id':f'{repeat}:{sample.sample_id}:{p["code"]}',
                    **{k:v for k,v in p.items() if k!='code'}} for p in output['predictions'] if p['code'] in scope)
            labels.extend({'case_id':f'{repeat}:{sample.sample_id}:{l.code}','mode':l.mode,'cap':l.cap,'grade':l.grade} for l in sample.labels)
    result=evaluate(labels,predictions)
    # evaluate's optional per-cell usage is not valid for shared multi-dimension calls.
    result.pop('measured_total_tokens'); result.pop('missing_usage_cases')
    return result


def _costs(keys,observed):
    runs=[observed[k] for k in keys if k in observed]
    counts=Counter(r['status'] for r in runs)
    result={'expected_runs':len(keys),'missing_runs':len(keys)-len(runs),
            'status_counts':{s:counts[s] for s in ('completed','failed','cancelled')},
            'budget_exceeded_runs':sum(r['total_tokens'] is not None and r['total_tokens']>r['token_budget'] for r in runs)}
    for field in ('total_tokens','model_requests','retries','elapsed_seconds','human_review_seconds'):
        values=[r[field] for r in runs if r[field] is not None]
        result[field]={'measured_runs':len(values),'unknown_runs':len(keys)-len(values),
                       'measured_sum':sum(values) if values else None,
                       'p50':_percentile(values,.5),'p95':_percentile(values,.95)}
    return result


def compare(envelope,observations):
    dataset,design=load_frozen(envelope)
    observed=validate_observations(envelope,observations)
    samples=[s for s in dataset.samples if s.split==design.split]
    repeats=list(range(1,design.repeats+1)); arms={}
    for strategy in STRATEGIES:
        keys=[(s.sample_id,strategy,r) for s in samples for r in repeats]
        arms[strategy]={'scores':_scores(samples,repeats,strategy,observed),'costs':_costs(keys,observed),
            'by_repeat':{str(r):_scores(samples,[r],strategy,observed) for r in repeats},
            'by_job':{job:_scores([s for s in samples if s.job_key==job],repeats,strategy,observed) for job in sorted({s.job_key for s in samples})},
            'by_dimension':{dim:_scores([s.model_copy(update={'labels':[l for l in s.labels if l.code[0]==dim]})
                for s in samples if any(l.code[0]==dim for l in s.labels)],repeats,strategy,observed)
                for dim in 'MEDIC' if any(l.code[0]==dim for s in samples for l in s.labels)}}
    paired={}
    for baseline in STRATEGIES[:2]:
        counts=Counter()
        for sample in samples:
            for repeat in repeats:
                left=observed.get((sample.sample_id,baseline,repeat)); right=observed.get((sample.sample_id,'multi_agent',repeat))
                for label in sample.labels:
                    if label.mode!='suggest' or label.grade is None: continue
                    counts['eligible_expert_cells']+=1
                    grades=[]
                    for record in (left,right):
                        grades.append(next((p['grade'] for p in record['predictions'] if p['code']==label.code),None)
                            if record and record['status']=='completed' else None)
                    if None in grades:
                        counts['unpaired_cells']+=1; continue
                    counts['paired_cells']+=1
                    error=[abs('ABCD'.index(g)-'ABCD'.index(label.grade)) for g in grades]
                    counts['multi_better' if error[1]<error[0] else 'baseline_better' if error[0]<error[1] else 'equal_error']+=1
        paired[baseline]={key:counts[key] for key in ('eligible_expert_cells','paired_cells','unpaired_cells','multi_better','baseline_better','equal_error')}
    return {'format':'medic-comparison-report-v1','experiment_sha256':envelope['sha256'],
        'analysis':envelope['payload']['analysis'],
        'dataset_provenance':dataset.provenance,'split':design.split,'budget_regime':design.budget_regime,
        'samples':len(samples),'repeats':design.repeats,'expected_runs':len(samples)*design.repeats*3,
        'observed_runs':len(observed),'arms':arms,'paired_absolute_error':paired,
        'quality_acceptance':'not_assessed_requires_frozen_expert_set_and_signed_thresholds',
        'limitations':['Input provenance and usage are importer declarations, not authenticated execution traces.',
            'Repeated cells are correlated; pooled counts are not independent sample sizes or confidence intervals.',
            'Failure and missing runs remain in coverage denominators; costs count once per run, including failures.',
            'Matched token ceilings do not imply equal realized tokens, monetary cost, or latency.',
            'No automatic ranking, statistical significance, expert signoff, or production acceptance.']}


def _read(path):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('duplicate_json_key')
            result[key]=value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=unique)


def _write_private(path,value):
    target=Path(path).resolve(); root=Path(__file__).resolve().parents[1]
    if not any(target.is_relative_to(root/name) for name in ('artifacts','private','var')):
        raise ValueError('comparison_output_requires_private_directory')
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('x',encoding='utf-8') as output:
        json.dump(value,output,ensure_ascii=False,indent=2,allow_nan=False); output.write('\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('freeze');p.add_argument('--dataset',required=True);p.add_argument('--design',required=True)
    p.add_argument('--output',required=True)
    p=sub.add_parser('schedule');p.add_argument('--experiment',required=True);p.add_argument('--output',required=True)
    p=sub.add_parser('report');p.add_argument('--experiment',required=True);p.add_argument('--observations',required=True);p.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if args.command=='freeze': value=freeze(_read(args.dataset),_read(args.design))
        elif args.command=='schedule': value=schedule(_read(args.experiment))
        else: value=compare(_read(args.experiment),_read(args.observations))
        _write_private(args.output,value)
    except Exception as exc:
        # Private labels, pseudonyms, paths and raw validation inputs stay out of console logs.
        print(json.dumps({'status':'failed','error_type':type(exc).__name__})); return 2
    print(json.dumps({'status':'written','command':args.command}));return 0


if __name__=='__main__': raise SystemExit(main())
