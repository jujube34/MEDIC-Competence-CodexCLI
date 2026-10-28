'use strict';
// Poll only the selected authorized assessment. No raw model reasoning is fetched.
let monitorGeneration=0,monitorTimer=null,monitorCursor=0;
const roleNames={coordinator:'任务规划',evaluator:'首轮评估',challenger_scan:'独立扫描',challenger:'反驳核查',reevaluator:'第二轮评估',aggregator:'跨材料汇总',judge:'目标审查'};
const eventNames={run_created:'创建运行',task_dispatched:'派发任务',task_leased:'开始执行',task_completed:'执行完成',task_failed:'执行失败',task_requeued:'重新入队',tool_invoked:'调用工具',result_submitted:'已提交结果',result_rejected:'结果校验拒绝',finalization_requested:'请求完成补正',preview_published:'发布预评估包',run_cancelled:'运行已取消',run_failed:'运行失败',agent_dispatched_task:'派发任务',agent_dispatched_plan:'任务图已创建',execution_bound:'连接 Agent 会话',attempt_requeued:'重新入队',manual_assessment_required:'已标记人工项',run_deadline_exhausted:'运行超时',runtime_event:'运行事件',service_attempt_started:'调度开始',service_attempt_finished:'调度结束'};
function stopMonitoring(){++monitorGeneration;clearTimeout(monitorTimer);monitorTimer=null;monitorCursor=0;$('agent-log').replaceChildren();$('agent-tasks').replaceChildren();$('agent-progress').textContent='选择评估后查看执行进度。';}
function startMonitoring(id){
  stopMonitoring();const generation=monitorGeneration,epoch=state.epoch;
  async function poll(){
    if(generation!==monitorGeneration||epoch!==state.epoch||!state.token||state.detail?.assessment_id!==id)return;
    try{
      const report=await api('/assessments/'+encodeURIComponent(id)+'/execution?after='+monitorCursor+'&limit=200');
      if(generation!==monitorGeneration||epoch!==state.epoch||state.detail?.assessment_id!==id)return;
      monitorCursor=report.next_cursor;
      const counts=report.counts,done=counts.completed||0;
      $('agent-progress').textContent=textLabel(report.state)+' · 已完成 '+done+'/'+report.expected_tasks+' · 执行中 '+((counts.running||0)+(counts.submitted||0))+' · 失败 '+(counts.failed||0)+(report.usage?' · 请求 '+report.usage.requests+' · 已报告 tokens '+report.usage.reported_tokens+' · 用量待确认 '+report.usage.unknown_usage_requests:'');
      $('agent-tasks').replaceChildren(...report.tasks.filter(t=>['running','submitted','failed'].includes(t.status)).map(t=>node('p',(roleNames[t.role]||t.role)+' · '+(t.competency_id||'全范围')+' · '+textLabel(t.status),'small')));
      const log=$('agent-log'),follow=$('agent-follow').checked;
      for(const e of report.events){const label=[new Date(e.created*1000).toLocaleTimeString('zh-CN'),e.role==='coordinator'?'全范围':e.competency_id,roleNames[e.role],eventNames[e.kind]||e.kind,e.tool,e.error_code].filter(Boolean).join(' · ');log.append(node('div',label,'agent-log-line'));}
      while(log.children.length>500)log.firstChild.remove();
      if(follow)log.scrollTop=log.scrollHeight;
      if(report.has_more){monitorTimer=setTimeout(poll,50);return;}
      // Preserve review drafts. Progress refresh never reloads the editing form.
      if(!['planning','evaluating','creation_pending'].includes(report.state)){
        $('agent-progress').textContent+=' · 运行已结束，可点击“刷新进度”加载结果。';return;
      }
      monitorTimer=setTimeout(poll,2000);
    }catch(error){
      if(generation!==monitorGeneration||epoch!==state.epoch)return;
      $('agent-progress').textContent='日志连接中断：'+(errors[error.message]||error.message)+'。可手动重新连接。';
    }
  }
  poll();
}
action($('agent-reconnect'),()=>{if(state.detail)startMonitoring(state.detail.assessment_id);});
