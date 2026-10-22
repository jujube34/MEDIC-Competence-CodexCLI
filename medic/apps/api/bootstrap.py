"""Create a fresh, private, synthetic-only local application workspace."""
import argparse
import json
from pathlib import Path

from medic.apps.api.service import Service
from medic.runtime import build


def bootstrap(folder):
    folder=Path(folder).resolve()
    if build.ROOT.parent in folder.parents and not any(folder.is_relative_to(build.ROOT/p) for p in ('private','var','artifacts')):
        raise ValueError('bootstrap_requires_ignored_private_directory')
    # A new directory avoids accidentally replacing existing identities or credentials.
    folder.mkdir(parents=True,exist_ok=False)
    build.verify(build.VERSION)
    fixture=json.loads((build.ROOT/'evals/fixtures/single_dimension_current.json').read_text(encoding='utf-8'))
    service=Service(folder/'runtime.sqlite3',folder/'vault')
    tenant='synthetic-local'; employee='SYN-001'; job='synthetic-role'
    service.db.provision_employee(tenant,employee,job,synthetic=True)
    tokens={}
    for user,role,own in (('employee','employee',employee),('manager','manager',None),('director','director',None)):
        tokens[user]=service.db.provision_user(tenant,user,role=role,employee_ref=own)
        if own is None: service.db.grant(tenant,user,employee,can_review=True)
    catalog={'title':'虚构 M2 标准，仅软件验证','synthetic':True,'status':'synthetic',
             **{k:fixture['versions'][k] for k in ('competency_model_version','job_matrix_version','ai_policy_version')},
             'criteria':fixture['criteria'],'policies':fixture['policies'],
             'recommendations':{job:{'M2':'C'}},
             'job_profiles':{job:{'family':'合成测试','track':'软件验证','title':'合成岗位','level':'测试职级'}},
             'competency_names':{'M2':'循证思辨能力（合成）'}}
    registry=service.register_catalog(tenant,catalog)
    config={'synthetic':True,'database':str(folder/'runtime.sqlite3'),'vault':str(folder/'vault'),
            'tenant':tenant,'employee_ref':employee,'registry_id':registry,'tokens':tokens}
    with (folder/'local-access.json').open('x',encoding='utf-8') as output:
        json.dump(config,output,ensure_ascii=False,indent=2)
    return {'workspace':str(folder),'credentials_file':str(folder/'local-access.json'),'synthetic':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder')
    print(json.dumps(bootstrap(parser.parse_args().folder),ensure_ascii=False))
