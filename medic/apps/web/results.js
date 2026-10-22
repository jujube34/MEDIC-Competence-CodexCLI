'use strict';
// A/B/C/D positions are ordinal display indices, never inferred ability scores.
function resultMetrics(rows,framework){
  const index=grade=>typeof grade==='string'&&/^[ABCD]$/.test(grade)?'ABCD'.indexOf(grade)+1:null;
  const codes=[...new Set([...framework,...rows.map(r=>r.competency_id)])];
  const items=codes.map(code=>{
    const row=rows.find(r=>r.competency_id===code);
    return {code,row,recommendation:index(row?.recommendation),ai:index(row?.ai.grade),human:index(row?.human?.decision.grade)};
  });
  const dimensions='MEDIC'.split('').map(code=>{
    const members=items.filter(i=>i.code.startsWith(code)),n=members.length;
    const summary={code,total:n,scope:members.filter(i=>i.row).length};
    for(const key of ['recommendation','ai','human']){
      const present=members.map(i=>i[key]).filter(v=>v!==null);
      summary[key+'Count']=present.length;
      summary[key]=n>0&&present.length===n?present.reduce((a,b)=>a+b,0)/n:null;
    }
    return summary;
  });
  const comparable=items.filter(i=>i.human!==null&&i.recommendation!==null);
  return {items,dimensions,total:items.length,scope:rows.length,processed:rows.filter(r=>r.human).length,
    human:items.filter(i=>i.human!==null).length,ai:items.filter(i=>i.ai!==null).length,
    comparable:comparable.length,met:comparable.filter(i=>i.human>=i.recommendation).length,
    gap:comparable.filter(i=>i.human<i.recommendation).length};
}
let criterionSelection=null;
function canEditReview(){const d=state.detail;return !!(d?.permissions.can_review&&d.ai_report&&d.human_status!=='finalized');}
function closeCriterion(){
  const dialog=$('criterion-dialog');if(dialog.open)dialog.close();criterionSelection=null;
  for(const id of ['criterion-title','criterion-content','criterion-hint'])$(id).replaceChildren();
  $('criterion-apply').disabled=true;
}
function openCriterion(code,grade){
  const d=state.detail,row=d?.rows.find(r=>r.competency_id===code);if(!row)return;
  const criteria=(row.criteria||[]).filter(c=>c.grade===grade);
  criterionSelection={assessment:d.assessment_id,revision:d.revision,code,grade};
  $('criterion-title').textContent=code+' · '+grade+' 档评判标准';
  const body=$('criterion-content');body.replaceChildren();
  body.append(node('p',d.job_key+' · '+(dimensions[code[0]]||'能力项'),'criterion-context'));
  body.append(node('p','岗位推荐 '+row.recommendation+' · AI '+(row.ai.grade||textLabel(row.ai.mode)),'small muted'));
  for(const criterion of criteria){const section=node('section',null,'criterion-anchor');section.append(node('h3',criterion.criterion_id),node('p',criterion.description));body.append(section);}
  if(!criteria.length)body.append(node('p','本次冻结版本未配置该档标准，暂不能从这里选用。','callout'));
  const provenance=node('details'),heading=node('summary','查看标准版本');provenance.append(heading,node('p','本次评估冻结版本：'+d.versions.competency_model_version,'small muted'));body.append(provenance);
  $('criterion-apply').disabled=!canEditReview()||!criteria.length;
  $('criterion-hint').textContent=canEditReview()?'选择后填写理由和依据，再保存人工判断。':'当前为只读查看。';
  if(!$('criterion-dialog').open)$('criterion-dialog').showModal();
}
function renderGradeStandards(){
  const code=$('review-code').value,row=state.detail?.rows.find(r=>r.competency_id===code);
  $('grade-standards').replaceChildren();if(!row)return;
  for(const grade of 'ABCD'){
    const button=node('button',grade+' 档标准'+(row.ai.grade===grade?' · AI 建议':''),'btn btn-secondary');button.type='button';
    action(button,()=>openCriterion(code,grade));$('grade-standards').append(button);
  }
}
function applyCriterion(){
  const s=criterionSelection,d=state.detail;
  if(!s||!canEditReview()||s.assessment!==d.assessment_id||s.revision!==d.revision)return closeCriterion();
  const row=d.rows.find(r=>r.competency_id===s.code);
  if(!row?.criteria.some(c=>c.grade===s.grade))return;
  if($('review-code').value!==s.code){$('review-code').value=s.code;resetReview();}
  $('disposition').value=row.ai.grade===null?'assessed':row.ai.grade===s.grade?'confirmed':'adjusted';
  syncGrade();$('human-grade').value=s.grade;rememberReviewDraft();closeCriterion();
  $('review-reason').focus();notify('已选择 '+s.grade+' 档，填写依据后点击“保存人工判断”。');
}
function filterResult(group,code){
  state.resultGroup=group;renderResultDetails();
  const target=code?$('result-item-'+code):$('result-table');
  if(target){target.scrollIntoView({behavior:'smooth',block:'center'});target.focus({preventScroll:true});}
}
function resultChart(entries,title,dimensionChart=false,expanded=false){
  const card=node('section',null,'card result-chart'),head=node('div',null,'result-chart-head');
  if(!expanded){const open=node('button','全屏查看','btn btn-secondary');open.type='button';open.setAttribute('aria-label','全屏查看'+title);open.setAttribute('aria-haspopup','dialog');action(open,()=>openResultChart(entries,title,dimensionChart));head.append(node('h3',title),open);card.append(head);}
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
  svg.setAttribute('viewBox','0 0 400 400');svg.setAttribute('role','group');svg.setAttribute('aria-label',title);
  const shape=(tag,attrs)=>{const e=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))e.setAttribute(key,String(value));svg.append(e);return e;};
  for(let grade=1;grade<=4;grade++){
    shape('circle',{cx:200,cy:200,r:grade*32,class:'radar-grid'});
    shape('text',{x:204,y:200-grade*32+12,class:'radar-label'}).textContent=dimensionChart?grade:'ABCD'[grade-1];
  }
  entries.forEach((entry,i)=>{
    const angle=i*2*Math.PI/entries.length-Math.PI/2,x=Math.cos(angle),y=Math.sin(angle);
    shape('line',{x1:200,y1:200,x2:200+x*128,y2:200+y*128,class:'radar-grid'});
    const label=dimensionChart?entry.code+' '+dimensions[entry.code]:entry.code;
    const link=shape('text',{x:200+x*(dimensionChart?138:158),y:204+y*(dimensionChart?145:158),'text-anchor':'middle',class:'radar-label radar-link',tabindex:0,role:'button','aria-label':'查看 '+label+' 明细'});
    link.textContent=label;const navigate=()=>{if(expanded){$('chart-selection').value=entry.code;renderChartInspector();}else filterResult(entry.code[0],dimensionChart?null:entry.code);};
    action(link,navigate);link.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();navigate();}});
    for(const [key,cls,size] of [['recommendation','radar-recommendation',8],['ai','radar-ai',6],['human','radar-human',4]]){
      if(entry[key]===null)continue;
      const marker=shape('circle',{cx:200+x*entry[key]*32,cy:200+y*entry[key]*32,r:size,class:cls});
      const tooltip=document.createElementNS(ns,'title');tooltip.textContent=label+' · '+({recommendation:'岗位推荐',ai:'AI 建议',human:'人工判断'}[key])+'：'+(dimensionChart?entry[key].toFixed(2):'ABCD'[entry[key]-1]);marker.append(tooltip);
    }
  });
  card.append(svg);if(!expanded)card.append(node('p','紫：岗位推荐 · 蓝：AI 建议 · 橙：人工判断。点击图中标签查看明细。','small muted'));
  return card;
}
function renderResultVisuals(){
  closeResultChart();
  const d=state.detail,m=resultMetrics(d.rows,d.framework_scope||d.rows.map(r=>r.competency_id));
  $('result-stats').replaceChildren(stat(m.met,'达到岗位推荐','可对照 '+m.comparable+' 项'),stat(m.gap,'低于岗位推荐','未评分不计为差距'),stat(m.ai+' / '+m.scope,'AI 评分覆盖','仅本次评估范围'),stat(m.processed+' / '+m.scope,'人工处理进度',d.final?'已定稿':'尚未定稿'));
  const charts=$('radar');charts.replaceChildren();
  charts.append(node('p','本次范围 '+m.scope+' / '+m.total+' 项；人工有档位 '+m.human+' 项。未纳入范围与缺失档位均留空。'+(d.final?'':'当前展示复核进度，尚非定稿结果。'),'callout result-chart-note'));
  const dimensionCard=resultChart(m.dimensions,'五维档位概览',true);
  dimensionCard.append(node('p','A=1、B=2、C=3、D=4 为展示序号。仅当一个维度全部能力项有档位时显示序号均值；不代表等距能力值或员工排名。','small muted'));
  dimensionCard.append(table(['维度','本次范围','人工有档位','人工序号均值'],m.dimensions.map(x=>[x.code+' '+dimensions[x.code],x.scope+' / '+x.total,x.humanCount+' / '+x.total,x.human===null?'—':x.human.toFixed(2)])));
  charts.append(dimensionCard,resultChart(m.items,m.total+' 项能力对照'));
  renderResultDetails();
}
let chartState=null;
function closeResultChart(){
  const dialog=$('chart-dialog');if(dialog.open)dialog.close();chartState=null;
  document.documentElement.classList.remove('radar-dialog-open');
  for(const id of ['chart-title','chart-context','chart-stage','chart-selection','chart-inspector'])$(id).replaceChildren();
  $('chart-stage').classList.remove('chart-zoom-150','chart-zoom-200');$('chart-zoom').textContent='100%';
}
function openResultChart(entries,title,dimensionChart){
  if(!state.detail)return;closeResultChart();
  chartState={assessment:state.detail.assessment_id,revision:state.detail.revision,entries,dimensionChart,zoom:100};
  $('chart-title').textContent=title;
  $('chart-context').textContent=state.detail.employee_ref+' · '+state.detail.period+' · '+(state.detail.final?'已定稿':'复核进度，尚未定稿');
  $('chart-stage').append(resultChart(entries,title,dimensionChart,true));
  $('chart-selection').replaceChildren(...entries.map(e=>new Option(e.code+(dimensionChart?' · '+dimensions[e.code]:''),e.code)));
  renderChartInspector();setChartZoom(100);$('chart-dialog').showModal();document.documentElement.classList.add('radar-dialog-open');
  $('chart-viewport').scrollTo(0,0);
}
function setChartZoom(zoom){
  if(!chartState)return;chartState.zoom=Math.max(100,Math.min(200,zoom));
  for(const size of [150,200])$('chart-stage').classList.toggle('chart-zoom-'+size,chartState.zoom===size);
  $('chart-zoom').textContent=chartState.zoom+'%';$('chart-minus').disabled=chartState.zoom===100;$('chart-plus').disabled=chartState.zoom===200;
  if(chartState.zoom===100)$('chart-viewport').scrollTo(0,0);
}
function renderChartInspector(){
  const s=chartState;if(!s)return;
  if(s.assessment!==state.detail?.assessment_id||s.revision!==state.detail?.revision)return closeResultChart();
  const entry=s.entries.find(e=>e.code===$('chart-selection').value);if(!entry)return;
  const parent=$('chart-inspector');parent.replaceChildren();
  parent.append(node('p','紫：岗位推荐 · 蓝：AI 建议 · 橙：人工判断','small muted'));
  parent.append(table(['来源',s.dimensionChart?'完整维度序号均值':'档位'],[['recommendation','岗位推荐'],['ai','AI 建议'],['human','人工判断']].map(([key,label])=>[label,entry[key]===null?'—':s.dimensionChart?entry[key].toFixed(2):'ABCD'[entry[key]-1]])));
  parent.append(node('p',s.dimensionChart?'仅完整维度显示序号均值；A–D 序号不代表等距能力值。':'未纳入范围或尚无档位时留空，不计为零。','small muted'));
  const go=node('button','查看 '+entry.code+' 明细','btn btn-primary');go.type='button';
  action(go,()=>{closeResultChart();filterResult(entry.code[0],s.dimensionChart?null:entry.code);});parent.append(go);
}
function renderResultDetails(){
  const d=state.detail,m=resultMetrics(d.rows,d.framework_scope||d.rows.map(r=>r.competency_id));
  $('result-filters').replaceChildren(...[['all','全部能力'],...Object.entries(dimensions)].map(([code,title])=>{
    const button=node('button',title,'btn '+(state.resultGroup===code?'btn-primary':'btn-secondary'));button.type='button';button.setAttribute('aria-pressed',String(state.resultGroup===code));action(button,()=>filterResult(code));return button;
  }));
  const visible=m.items.filter(i=>state.resultGroup==='all'||i.code.startsWith(state.resultGroup));
  const resultTable=table(['能力项','AI 建议','岗位推荐','人工判断','人工与推荐差距'],visible.map(({code,row:r})=>{
    const detail=node('button',code,'text-button');detail.type='button';detail.disabled=!r;action(detail,()=>filterResult(code[0],code));
    if(!r)return [detail,'未纳入本次范围','—','—','—'];
    return [detail,gradeCell(r.ai.grade,textLabel(r.ai.status),'ai-grade'),gradeCell(r.recommendation),gradeCell(r.human?.decision.grade,r.human?textLabel(r.human.decision.disposition):'尚未处理','human-grade'),r.difference_from_recommendation===null?'—':(r.difference_from_recommendation>0?'+':'')+r.difference_from_recommendation];
  }));
  $('result-table').replaceChildren(resultTable);$('result-table').tabIndex=-1;
  $('result-evidence').replaceChildren(...visible.map(({code,row:r})=>{
    const card=node('article',null,'result-evidence-card');card.id='result-item-'+code;card.tabIndex=-1;
    card.append(node('h3',code+' · '+dimensions[code[0]]));
    if(!r){card.append(node('p','未纳入本次评估范围。','muted'));return card;}
    const standardButtons=node('div',null,'grade-standards');
    for(const grade of 'ABCD'){const b=node('button',grade+' 档标准','btn btn-secondary');b.type='button';action(b,()=>openCriterion(code,grade));standardButtons.append(b);}card.append(standardButtons);
    const human=r.human?.decision,ai=(d.ai_report?.assessments||[]).find(a=>a.competency_id===code);
    card.append(node('h4','AI 依据'),node('p',ai?.synthesis_reason||(ai?.limitations||[]).join('；')||textLabel(r.ai.status)));
    for(const ref of ai?.supporting_evidence||[])card.append(resultReference(ref));
    card.append(node('h4','人工判断'),node('p',human?(human.grade||'未给档位')+' · '+textLabel(human.disposition)+'：'+human.reason:'尚未处理'));
    if(human?.manual_basis)card.append(node('p','观察依据：'+human.manual_basis));
    if(human?.risk_verification)card.append(node('p','风险核验：'+({confirmed:'人工确认风险',not_present:'经人工核验未发现',requires_investigation:'仍需调查'}[human.risk_verification]),'callout'));
    for(const ref of human?.evidence_refs||[])card.append(resultReference(ref,r.human.supplemental_material_ids||[]));
    return card;
  }));
}
function resultReference(ref,supplemental=[]){
  const box=node('blockquote',ref.quote,'ref'),button=node('button','定位该引用','text-button');button.type='button';
  action(button,()=>locateReference(ref,supplemental));box.append(node('br'),button);return box;
}
if(typeof document!=='undefined'){
  action($('chart-close'),closeResultChart);action($('chart-plus'),()=>setChartZoom(chartState.zoom+50));action($('chart-minus'),()=>setChartZoom(chartState.zoom-50));action($('chart-fit'),()=>setChartZoom(100));
  $('chart-selection').addEventListener('change',renderChartInspector);
  $('chart-dialog').addEventListener('cancel',event=>{event.preventDefault();closeResultChart();});
  action($('criterion-close'),closeCriterion);action($('criterion-apply'),applyCriterion);
  $('criterion-dialog').addEventListener('cancel',event=>{event.preventDefault();closeCriterion();});
  $('criterion-dialog').addEventListener('click',event=>{if(event.target===$('criterion-dialog')){
    const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeCriterion();
  }});
}
if(typeof module!=='undefined')module.exports={resultMetrics};
