"""Read-only local audit view; no remote assets, scripts, or client-side scoring state."""
from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote


def render(snapshot, path):
    esc = lambda value: html.escape(str(value), quote=True)
    source = snapshot["source"]
    report = snapshot.get("report") or {}
    gate = report.get("gate") or {"passed": False, "failures": ["运行尚未形成可复核报告"]}
    labels = {"coordinator": "任务规划", "evaluator": "首评", "aggregator": "跨材料汇总", "challenger_scan": "独立盲审", "challenger": "反驳",
              "reevaluator": "二评", "judge": "目标审查"}
    cards = []
    for task in snapshot["tasks"]:
        result = snapshot["results"].get(task["task_id"])
        label = labels.get(task['role'], esc(task['role']))
        if task.get('business_round') == 3:
            label = '返工规划' if task['role'] == 'coordinator' else '第三轮 · ' + label
        header = f'<h2>{label} <small>{esc(task["status"])}</small></h2>'
        header += '<p><small>执行来源：' + esc(task.get('execution_mode') or '尚未完成') + ' · 本业务轮 attempt：' + esc(task.get('attempt_in_round', task['lease_epoch'])) + '</small></p>'
        header += f'<p><small>{esc(task["competency_id"])} · {"跨材料" if task["material_id"] == "__aggregate__" else esc(task["material_id"])}</small></p>'
        if not result:
            cards.append(f'<section>{header}<p>{esc(task.get("error") or "尚未完成")}</p></section>')
            continue
        body = ""
        if "grade" in result:
            body += f'<p class="grade">建议档位：{esc(result["grade"] or "—（未给档位）")}</p>'
            body += '<p>范围限制：' + esc("；".join(result["limitations"])) + '</p>'
        if "source_groups" in result:
            body += '<p>同源分组：' + esc(json.dumps(result["source_groups"], ensure_ascii=False)) + '</p><p>' + esc(result["synthesis_reason"]) + '</p>'
        for claim in result.get("claims", []):
            refs = " ".join(f'<a href="#ev-{quote(r["evidence_id"],safe="")}">{esc(r["evidence_id"])}</a>' for r in claim["evidence_refs"])
            body += f'<p><strong>{esc(claim["claim_id"])}</strong> {esc(claim["statement"])} {refs}</p>'
        for challenge in result.get("challenges", []):
            body += f'<p class="issue"><strong>{esc(challenge["severity"])} · {esc(challenge["challenge_id"])}</strong> → {esc(challenge["claim_id"])}<br>{esc(challenge["falsification_question"])}<br>{esc(challenge["explanation"])}</p>'
        for response in result.get("challenge_responses", []):
            body += f'<p><strong>{esc(response["challenge_id"])} · {esc(response["disposition"])}</strong> {esc(response["reason"])}</p>'
        for response in result.get('prior_dispute_responses', []):
            body += f'<p class="issue"><strong>旧争议 {esc(response["challenge_id"])} · {esc(response["disposition"])}</strong> {esc(response["reason"])}</p>'
        if "verdict" in result:
            body += f'<p class="grade">Judge：{esc(result["verdict"])}</p><p>{esc(result["reason"])}</p>'
            body += '<ul>' + ''.join(f'<li>{"✓" if c["passed"] else "×"} {esc(c["check"])}：{esc(c["reason"])}</li>' for c in result["checks"]) + '</ul>'
        body += f'<details><summary>结构化结果与版本关联</summary><pre>{esc(json.dumps(result,ensure_ascii=False,indent=2))}</pre></details>'
        cards.append('<section>' + header + body + '</section>')
    evidence = ''.join(f'<article id="ev-{quote(s["evidence_id"],safe="")}"><h3>{esc(s["evidence_id"])} · {esc(s["locator"])}</h3><blockquote>{esc(s["quote"])}</blockquote><small>快照 {esc(s["material_version"])} · hash {esc(s["span_hash"][:16])}</small></article>'
                       for m in source["materials"] for s in m["spans"])
    timeline = '<ol>' + ''.join(f'<li>{e["seq"]} · {esc(e["kind"])} <small>{esc((e["task_id"] or "")[-12:])}</small></li>' for e in snapshot["events"]) + '</ol>'
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MEDIC 合成评估审计</title><style>
body{font:16px/1.65 system-ui,"Microsoft YaHei",sans-serif;background:#f3f6f8;color:#182b36;margin:0}main{max-width:1180px;margin:auto;padding:32px 24px}
header{border-left:5px solid #147c83;padding:8px 24px;margin-bottom:24px}h1{margin:0;font-size:30px}h2{font-size:21px;margin-top:0}small{font-weight:400;color:#627580}
section,article{background:white;padding:24px;border:1px solid #dce6eb;border-radius:10px;margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.grade{font-size:20px;font-weight:650}
pre{white-space:pre-wrap;word-break:break-word;font-size:13px;background:#edf3f5;padding:16px}blockquote{margin:0;border-left:3px solid #acc5cf;padding:8px 16px;white-space:pre-wrap}
a{color:#08777e}details{margin-top:18px}summary{cursor:pointer}.issue{background:#fff7e7;padding:14px;border-left:3px solid #c9932b}.badge{display:inline-block;background:#d9edef;padding:4px 12px;border-radius:20px}
@media(max-width:760px){.grid{grid-template-columns:1fr}main{padding:16px}header{padding:8px 16px}}
</style><main>'''
    page += f'<header><h1>MEDIC 合成评估审计</h1><p>仅为软件验收用合成材料 · 所有结果仍需人工复核 · 不构成员工最终评价</p><span class="badge">硬规则：{"通过" if gate["passed"] else "未放行"}</span> <span>{esc(snapshot["state"])}</span><p>{esc(source["goal"]["description"])}</p><small>{esc(snapshot["run_id"])}</small></header>'
    if gate["failures"]:
        page += '<section class="issue"><strong>待处理问题</strong><ul>' + ''.join('<li>' + esc(x) + '</li>' for x in gate["failures"]) + '</ul></section>'
    page += '<div class="grid">' + ''.join(cards) + '</div><h2>证据快照（内容不作为指令执行）</h2>' + evidence
    if snapshot.get('mailbox'):
        page += '<h2>受限争议会谈</h2><p>消息是待核验主张，不替代原文、正式反驳回应或人工裁决。</p>'
        for message in snapshot['mailbox']:
            page += '<article><strong>' + esc(message['sender_role']) + ' · ' + esc(message['kind']) + ' · ' + esc(message['challenge_id']) + '</strong><p>' + esc(message['text']) + '</p><small>' + esc(message['competency_id']) + ' · ' + esc(message['material_id']) + ' · 往返序号 ' + esc(message['sequence']) + '</small></article>'
    if snapshot.get('rework_plan'):
        page += '<details><summary>Coordinator 返工决策与受影响节点</summary><pre>' + esc(json.dumps(snapshot['rework_plan'], ensure_ascii=False, indent=2)) + '</pre></details>'
    if snapshot.get('task_history'):
        page += '<details><summary>已保留的历史 attempt 与业务轮次</summary><pre>' + esc(json.dumps(snapshot['task_history'], ensure_ascii=False, indent=2)) + '</pre></details>'
    page += '<details><summary>完整事件时间线</summary>' + timeline + '</details></main></html>'
    Path(path).write_text(page, encoding="utf-8")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("output")
    args = parser.parse_args()
    render(json.loads(Path(args.snapshot).read_text(encoding="utf-8")), args.output)
