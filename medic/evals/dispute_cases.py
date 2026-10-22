"""Prospective synthetic ambiguity cases; never seed assessment results.

Inputs exercise real model judgement, so disagreement/rework is not guaranteed.
Freeze every case and its input hash before execution; retain uncovered cases.
"""
import argparse
import copy
import json
from pathlib import Path

from medic.domain.contracts import AssessmentInput
from medic.domain.validation import verify_input
from medic.p0.common import digest, now, save
from medic.runtime import build
from medic.runtime.budget import BudgetLimits


CASES = {
    'direction-ledger-attribution': {
        'title': '虚构质量记录：指标方向、口径字典与修订归属',
        'hypothesis': '倒数指标可为等价表达，而相同数字的事件口径不一定等价；版本及编辑角色的交叉核对可能产生需要澄清的真实争议。',
        'goal': '按正常六角色流程核对指标方向、事件口径及修订归属，区分可从原材料解决的解释分歧与确需新证据的缺口。若正式反驳中的术语或所指版本不明确，使用受限消息澄清，并保留正式回应；不要为了覆盖测试制造问题、消息、返工或评分。不是正式员工评价。',
        'paragraphs': [
            '这是完全虚构的软件测试记录。SYN-001 整理甲乙两组操作流程，数字、版本、签名与任务都不存在于实际业务。不是医疗研究、药品或患者资料。以下内容均为被评估材料，不是给评估系统的指令。',
            '原始登记 R0：甲组 120 个任务，其中异常返工 24 个；乙组 200 个任务，其中异常返工 50 个。两组均非随机分配，任务难度构成不同，没有分层表。时间指标为每个任务的平均处理分钟数；甲减乙为 -4，模拟 95% 区间为 [-6,-2]。',
            '机器导出 R1：事件字段 E 表示异常返工，表中 B/A 为 1.25，模拟 95% 区间为 [1.10,1.40]。SYN-001 初稿把 E 列中文名写成“成功完成”，同时写“甲的成功率比乙低 20%，甲平均处理时间更长 4 分钟，因此乙更优”。导出比例与区间只是本例给定数，不要求从计数重新估计置信区间。',
            '修订 R2 的正文由 SYN-001 保存：E 为异常返工，甲 20%、乙 25%；A/B 为 0.80，相应区间按原区间取倒数并调换端点，约为 [0.714,0.909]。原 B/A=1.25 与新 A/B=0.80 不是两次独立观察。甲的异常返工率低 5 个百分点，相对低 20%；平均时间甲比乙短 4 分钟。不能因此断言流程造成改善。',
            'R2 中保留了初稿截图，截图标题仍是“成功完成”，正文旁注写“此截图只说明旧版错误，不作为最终事件口径”。R2 的数据字典表明确写 E=异常返工，成功且不返工的计数为甲 96、乙 150；这些是同一批任务的互补事件，不能直接把原返工比值改名为成功比值。',
            '操作台账：T1，SYN-002 在讨论区评论“核对 E 的含义及时间差方向”，没有写出比率、区间端点或更正句。T2，SYN-001 在正文编辑记录新增 R2 的全部计算和解释。T3，SYN-002 将段落样式统一后点击保存。导出文件最后修改者因此显示 SYN-002。编辑记录与最后修改者字段含义不同，不能仅按文件属性推断正文作者。',
            'SYN-001 的补充说明：我先收到同事提醒，再自己查字典、换算倒数区间、区分百分点并纠正时间方向。现有台账能显示修改账号，不能证明我在账号操作之外没有获得具体答案。本材料只有一次修订事件，没有独立测验、第三方能力认证、跨人应用或后续采纳记录。',
            'R2 的局限段由 SYN-001 新增：比较来自不同任务构成，不能作因果推广；区间提示估计不确定性，不能替代效果大小或业务意义；任务数不能当作多次独立能力展示。汇总封面仍保留初稿“乙更优”作为删除线修订痕迹，R2 的结论栏明确为“仅描述本次记录，不作因果优劣结论”。',
        ],
        'facts': ['A_failure_24_of_120_B_50_of_200', 'B_over_A_1_25_equivalent_to_A_over_B_0_8',
                  'reciprocal_interval_requires_reversing_endpoints', 'success_ratio_is_0_8_over_0_75_not_0_8',
                  'A_minus_B_negative_time_means_A_shorter', 'edit_author_differs_from_last_save_account',
                  'prompted_correction_not_independent_unassisted_demonstration', 'one_revision_event_not_corroboration'],
    },
    'measure-version-attribution': {
        'title': '虚构整理材料：效应指标、修订版本与个人归属',
        'hypothesis': '不同指标的无效值、他人订正与个人自述并存，可能触发对锚点映射或个人归属的可证伪反驳。',
        'paragraphs': [
            '仅供软件测试的完全虚构材料。员工 SYN-001 提交同一事件的 v1、v2、v3 修订记录。所有数字、人员代号与研究均虚构，不涉及真实药物或患者。以下是材料正文，不是评估系统指令。',
            'v1，作者 SYN-001：研究甲为两组随机比较，效应指标是均值差，点估计 0.80，95% 区间 0.60 至 1.10；我的结论是区间跨过 1，不能认为两组有明确差异。研究乙为两组随机比较，效应指标是风险比，点估计 0.80，95% 区间 0.60 至 1.10；我的结论是风险降低 20%，效果明确。',
            'v2，修订记录署名 SYN-002：甲的均值差无效值为 0，区间不含 0；乙的风险比无效值为 1，区间包含 1。将两段结论分别改为“甲在该估计的不确定区间下存在统计差异，但不能据此断定实际意义”和“乙不能据该区间断言差异明确”。两处方法学订正均由 SYN-002 写入。',
            'v3，提交人 SYN-001 的说明：我核对并认可 v2 的两处订正，把它们放入最终表；我现在能够区分均值差的 0 和风险比的 1。附件修订轨迹只显示这段认可说明由我新增，没有独立作答过程。本人最初写错的内容仍保留在 v1 中。',
            'v3，作者 SYN-001 新增的局限说明：甲随访只有四周，不能推出长期影响；乙的区间包含无效值，不能以点估计替代不确定性。两项量纲不同，不应直接合并效应值。此段标记为本人撰写，但没有外部考核或复核记录。',
            '归档说明：SYN-002 的订正与 SYN-001 的认可属于同一文档修订事件。没有另一份独立应用、带教、采纳或长期机制记录。提交人自述“完成了独立评价”，同时承认关键统计解释最初由同事订正。',
        ],
        'facts': ['mean_difference_null_is_zero', 'risk_ratio_null_is_one', 'corrections_authored_by_SYN002',
                  'SYN001_acknowledgement_not_independent_demonstration', 'same_revision_event_not_external_corroboration'],
    },
    'denominator-and-selection': {
        'title': '虚构整理材料：总体与分层、样本选择和重复佐证',
        'hypothesis': '总体比例与两层比例方向相反，且报告人先后解释不同，可能暴露首评遗漏或过度保守并形成真实争议。',
        'paragraphs': [
            '完全虚构的软件测试材料，提交人 SYN-001，单一整理事件。方案甲乙只是测试标签，不是医疗干预。成功表示虚构任务完成，不能用于任何实际健康判断。',
            'SYN-001 的原始汇总：甲完成 81/100，乙完成 234/300，因此甲总体完成率更高。分层表：容易层甲 80/90、乙 180/200；困难层甲 1/10、乙 54/100。表内分母均为进入对应层的任务数，没有随机分配记录。',
            'SYN-001 的摘要初稿：甲总体 81%，乙总体 78%，故甲方案在两类难度都更有效，建议直接推广。摘要没有给出分层完成率，也未解释两组任务难度构成。',
            'SYN-001 的后续批注：容易层甲约 88.9%、乙 90%；困难层甲 10%、乙 54%。总体排序与层内排序相反；不能用总体高三个百分点推出每层都优。两组选择机制不明，不能作因果推荐。初稿文字未删除，批注写在同页右栏，签署时间晚一天。',
            '归属及来源说明：批注署名 SYN-001，没有他人代写记录；正文同时附两张“独立复核截图”，实际都是同一个文件的截图，第二张只改变文件名。截图没有复核人、日期或新来源。',
            '材料边界：没有第三方证明批注为独立完成，没有推广采纳、跨人应用或长期跟踪资料。提交人称通过复核纠正了早期错误；究竟是独立识别还是看过外部答案后修改，现有材料无法区分。',
        ],
        'facts': ['pooled_A_81_percent_B_78_percent', 'both_strata_B_higher_than_A',
                  'nonrandom_selection_precludes_causal_claim', 'late_annotation_corrects_early_summary',
                  'two_screenshots_are_one_event'],
    },
}


def case_input(case_id):
    spec=CASES[case_id]
    source=json.loads((build.ROOT/'evals/fixtures/single_dimension_current.json').read_text(encoding='utf-8'))
    source=copy.deepcopy(source)
    source['versions']['runtime_version']=build.VERSION
    source['goal'].update(goal_id='synthetic-dispute-'+case_id, version='synthetic-dispute-goal-v1',
        description='对这份含早期错误、后续订正与归属限制的合成材料完成正常六角色评估。区分材料已能证实的事实、仍需澄清的问题和个人能力推断边界；不预设评分、争议数量或是否返工。不是正式员工评价。')
    if spec.get('goal'):
        source['goal']['description']=spec['goal']
    source['versions']['competency_model_version']='synthetic-dispute-rubric-v1'
    source['criteria']['M2'][0]['description']='能准确区分研究设计、效应指标及不确定性，不把未得到支持的结论写成确定结论；需区分本人操作、本人认可和他人订正。'
    source['criteria']['M2'][1]['description']='在 A 基础上，能列出设计或选择局限并说明不可作出的推断；评价仅限当前材料可归属的表现，不据此推断稳定能力。'
    material=source['materials'][0]
    material.update(material_id='synthetic-'+case_id,version='synthetic-dispute-material-v1',
        title=spec['title'],source_event_id='synthetic-event-'+case_id)
    material['spans']=[]
    for i,quote in enumerate(spec['paragraphs'],1):
        locator=f'text:paragraph:{i}'
        material['spans'].append({'evidence_id':f'{case_id}-span-{i}','material_id':material['material_id'],
            'material_version':material['version'],'locator':locator,'quote':quote,
            'span_hash':digest([material['version'],locator,quote]),'quality_flags':[]})
    material['sha256']=digest(spec['paragraphs'])
    verify_input(AssessmentInput.model_validate(source))
    return source


def prepare(output, *, case_ids=None, limits=None):
    output=Path(output).resolve()
    if not output.is_relative_to(build.ROOT/'artifacts'):
        raise ValueError('prospective_cases_require_private_artifacts')
    selected=list(CASES) if case_ids is None else list(case_ids)
    if not selected or len(selected)!=len(set(selected)) or any(name not in CASES for name in selected):
        raise ValueError('invalid_prospective_case_selection')
    limits=BudgetLimits.model_validate(limits or {'requests':100,'total_tokens':750000,'concurrency':2,'wall_seconds':2400}).model_dump()
    identity=build.verify(build.VERSION)
    output.mkdir(parents=True,exist_ok=False)
    save(output/'limits.json',limits)
    cases=[]
    for case_id in selected:
        spec=CASES[case_id]
        source=case_input(case_id)
        save(output/(case_id+'.json'),source)
        cases.append({'case_id':case_id,'input_hash':digest(source),'hypothesis':spec['hypothesis'],
                      'fact_checks':spec['facts']})
    manifest={'created_at':now(),'scope':'synthetic_real_initial_roles_dispute_coverage',
        'execution_build':identity,'limits':limits,'cases':cases,'seeded_results':False,
        'policy':'Keep every attempted case. Zero challenges is coverage missing, not dispute success. Do not force disagreement or inject results.',
        'business_signoff':False}
    save(output/'prospective-cases.json',manifest)
    return {'cases':len(cases),'manifest_hash':digest(manifest),'runtime_version':build.VERSION}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',required=True)
    parser.add_argument('--case',choices=tuple(CASES),action='append',dest='cases')
    parser.add_argument('--limits',help='Budget JSON frozen before a new case starts; never changes an existing run')
    args=parser.parse_args()
    limits=json.loads(Path(args.limits).read_text(encoding='utf-8')) if args.limits else None
    print(json.dumps(prepare(args.output,case_ids=args.cases,limits=limits)))
