from medic.apps.api.contracts import Catalog
from medic.domain.registry import load_candidate


def candidate_catalog(root, ident):
    """Expose the verified 49-job baseline; never turn import into business signoff."""
    bundle=load_candidate(root,ident)
    criteria={}
    for code, item in bundle['competencies'].items():
        criteria[code]=[{'criterion_id':f'{code}.{grade}.{layer}','grade':grade,
                        'description':item['definition']+'\n'+item[f'{layer}_{grade}']}
                       for grade in 'ABCD' for layer in ('concept','execution')]
    policies={code:{'mode':mode,'cap':bundle['policy']['suggestion_caps'].get(code) if mode=='suggest' else None,'signed_off':False}
              for mode,codes in bundle['policy']['groups'].items() for code in codes}
    recommendations={}
    for row in bundle['recommendations']:
        recommendations.setdefault(row['job_key'],{})[row['competency_id']]=row['grade']
    return Catalog(title='MEDIC 260914 候选标准',synthetic=False,status='awaiting_business_signoff',
                   competency_model_version=bundle['competency_model_version'],job_matrix_version=bundle['job_matrix_version'],
                   ai_policy_version=bundle['ai_policy_version'],criteria=criteria,policies=policies,recommendations=recommendations,
                   job_profiles=bundle.get('job_profiles',{}),
                   competency_names={code:item['name'] for code,item in bundle['competencies'].items()})
