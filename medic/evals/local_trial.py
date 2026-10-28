"""Create a private 17-item trial workspace and synthetic documents, never labels."""
import argparse
import io
import json
from pathlib import Path
from xml.sax.saxutils import escape
import zipfile

from medic.apps.api.catalog import candidate_catalog
from medic.apps.api.contracts import CreateAssessment
from medic.apps.api.service import Service
from medic.p0.common import CODES, digest

MATERIALS = [
    ['阶段性循证支持记录（虚构项目）',
     '我对六篇公开研究的研究设计、入排标准、终点定义和局限进行了对照。发现一份摘要把相对变化写成绝对获益，我在交付前指出计算口径差异，保留原始计算表，并在结论旁注明尚无头对头证据，不能推断方案优越性。该分析只描述已提交资料，未声称真实临床效果。',
     '讨论中我将研究终点、效应量和不确定性改写为便于非统计专业同事理解的图表，并请一名同事回述关键限制。记录了两处误解并修改说明。报告的版本记录能说明本人完成了方法核查与修改，但没有客户采纳或跨团队推广的独立证明。',
     '针对三位同事提出的文献检索问题，我制作检索词与筛选清单，逐条记录来源和排除理由。后续通过共享表格跟踪问题负责人、回复时间和仍待核实事项。尚未验证知识变化或对真实业务结果的影响。'],
    ['项目协作与交付复盘（虚构项目）',
     '我把一次内部资料更新拆为检索、证据核对、医学审阅和发布四个环节，列出负责人、截止时间与验收条件。第一次试运行发现不同成员重复核查同一资料，我调整任务看板，并保留修订记录。仅观察到本次交付按期完成，没有依据归因长期效率提升。',
     '一位同事认为资料不应保留不确定性表述，我在会议中列出原始证据和替代解释，邀请大家逐条确认。最终保留限制说明并建立待解决问题清单。该记录来自本人复盘，缺少同事评价，不能据此认定稳定影响力或领导力。',
     '我试用文献去重脚本，并以十条人工检查结果核对输出。发现两条标题相似但研究不同的记录被误删，修正规则后重新核对全部十条。发布前删除资料中的个人信息，检查来源授权并由医学审阅人复核。该材料不是合规调查，不能证明所有工作均无风险。'],
]


def document(paragraphs):
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + ''.join('<w:p><w:r><w:t>'+escape(value)+'</w:t></w:r></w:p>' for value in paragraphs)
           + '</w:body></w:document>')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as package:
        package.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        package.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        package.writestr('word/document.xml', xml)
    return stream.getvalue()


def prepare(folder, registry_root, registry_id):
    folder = Path(folder).resolve()
    from medic.runtime import build
    build.verify(build.VERSION)
    if not any(folder.is_relative_to(build.ROOT/p) for p in ('private', 'var', 'artifacts')):
        raise ValueError('trial_requires_private_directory')
    folder.mkdir(parents=True, exist_ok=False)
    service = Service(folder/'runtime.sqlite3', folder/'vault')
    tenant, employee, job, period = 'local-trial', 'TRIAL-001', 'trial-role', '2026-Q4-trial'
    service.db.provision_employee(tenant, employee, job, synthetic=True)
    tokens = {}
    for user, role, own in [('employee','employee',employee),('manager','manager',None),('director','director',None)]:
        tokens[user] = service.db.provision_user(tenant,user,role=role,employee_ref=own)
        if own is None: service.db.grant(tenant,user,employee,can_review=True)
    candidate = candidate_catalog(registry_root, registry_id)
    service.register_catalog(tenant,candidate)
    trial = candidate.model_dump()
    trial.update(title='17 项能力试用标准（候选锚点，非正式评价）',synthetic=True,status='synthetic',
                 competency_model_version='trial-v17-'+candidate.competency_model_version,
                 job_matrix_version='trial-jobs-v17',ai_policy_version='trial-v17-'+candidate.ai_policy_version,
                 recommendations={job:{c:'B' for c in CODES}},
                 job_profiles={job:{'family':'本机试用','track':'能力评估','title':'试用岗位','level':'技术验证'}})
    # Preserve both concept and execution anchors in one explicit check per grade.
    trial['criteria'] = {code:[{'criterion_id':code+'.'+grade+'1','grade':grade,
                               'description':'\n'.join(item.description for item in items if item.grade==grade)}
                              for grade in 'ABCD'] for code,items in candidate.criteria.items()}
    trial_id = service.register_catalog(tenant,trial)
    principal = service.db.authenticate(tokens['employee'])
    ids = []
    for index, paragraphs in enumerate(MATERIALS,1):
        path = folder/('synthetic-'+str(index)+'.docx')
        path.write_bytes(document(paragraphs))
        ids.append(service.ingest(principal,employee,path,period)['snapshot_id'])
    assessment = service.create(principal,CreateAssessment(employee_ref=employee,period=period,
        registry_id=trial_id,material_ids=ids,competency_scope=list(CODES)),'trial-v17-fullscope-01')
    config = {'synthetic':True,'database':str(folder/'runtime.sqlite3'),'vault':str(folder/'vault'),
              'tokens':tokens,'employee_ref':employee,'period':period,'registry_id':trial_id,
              'assessment_id':assessment['assessment_id'],'run_id':assessment['run_id'],
              'candidate_registry':registry_id,'candidate_criteria_sha256':digest(trial['criteria'])}
    with (folder/'local-access.json').open('x') as out:json.dump(config,out,ensure_ascii=False,indent=2)
    return {'workspace':str(folder),'assessment_id':assessment['assessment_id'],'run_id':assessment['run_id'],
            'scope':17,'materials':2,'criteria':68,'synthetic_materials':True,'model_results_seeded':False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',required=True);parser.add_argument('--registry-root',required=True);parser.add_argument('--registry-id',required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.folder,args.registry_root,args.registry_id),ensure_ascii=False))
