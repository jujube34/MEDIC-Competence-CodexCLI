'use strict';
// UI-v1.02 navigation mapped to authenticated API data. No demo identities/results.
const dimensions={M:'专业素质',E:'影响力',D:'策略能力',I:'创新与数字化',C:'合规意识'};
function navItems(){
  if(state.user?.role==='employee')return [['home','个人主页'],['upload','提交工作产物'],['result','能力结果'],['model','岗位推荐值']];
  if(state.user?.role==='manager')return [['home','管理工作台'],['review','评价复核'],['overview','团队员工'],['model','岗位推荐值']];
  return [['home','团队概览'],['overview','员工结果'],['model','岗位推荐值']];
}
function renderNavigation(){
  for(const [id,cls] of [['main-navigation','nav-button'],['bottom-navigation','bottom-nav-btn']]){
    $(id).replaceChildren(...navItems().map(([view,label],i)=>{
      const button=node('button',null,cls);button.type='button';button.dataset.view=view;
      button.append(node('span',String(i+1).padStart(2,'0'),id==='main-navigation'?'nav-index':'bottom-nav-index'),node('span',label));
      action(button,async()=>{switchView(view);if(view==='overview')renderOverview();if(view==='model')renderModel();});return button;
    }));
  }
}
function switchView(view){
  if(view==='work')view='result';
  closeResultChart();
  const changed=state.view!==view;state.view=view;if(changed)window.scrollTo({top:0,behavior:'instant'});
  for(const [id,visible] of [['home-panel',view==='home'],['overview',view==='overview'],['model-panel',view==='model'],['work-area',['upload','result','review'].includes(view)]])$(id).classList.toggle('hidden',!visible);
  $('upload-panel').classList.toggle('hidden',view!=='upload');
  $('assessment-panel').classList.toggle('hidden',view==='upload');
  $('result').classList.toggle('hidden',!state.detail||view==='upload');
  $('detail-layout').classList.toggle('review-layout',view==='review');$('result-analysis').open=view!=='review';
  $('page-title').textContent=navItems().find(([key])=>key===view)?.[1]||({review:'评价复核',result:'员工能力结果'}[view]||'工作台');
  $('page-subtitle').textContent={home:'查看当前评价进度和下一步操作。',upload:'上传阶段性工作产物，核对解析内容后创建预评估。',result:'查看判断依据、岗位推荐值与能力差距。',review:'按维度复核 AI 建议，补齐人工判断并记录依据。',overview:'查看获授权员工的提交与复核状态。',model:'查看岗位推荐值、能力标准和 AI 参与范围。'}[view]||'';
  document.querySelectorAll('[data-view]').forEach(b=>{const active=b.dataset.view===view;b.classList.toggle('active',active);if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  if(view==='home')renderHome();
}
function stat(value,label,foot){const box=node('div',null,'card kpi');box.append(node('div',label,'kpi-label'),node('div',value,'kpi-value'),node('div',foot||'','kpi-foot'));return box;}
function renderHome(){
  if(!state.user)return;
  const own=state.assessments.filter(a=>a.employee_ref===state.employee),latest=own.at(-1),employee=state.user.role==='employee';
  const final=latest?.human_status==='finalized',count=state.assessments.filter(a=>a.human_status==='finalized').length;
  const box=$('home-task');box.replaceChildren();const content=node('div');
  content.append(node('span',employee?(latest?assessmentLabel(latest):'尚未提交'):'授权范围内 '+state.employees.length+' 名员工','status-badge status-info'),node('h2',employee?(final?'查看能力结果':latest?'跟进评估与复核进度':'提交阶段性工作产物'):'查看团队完成状态'),node('p',employee?'上传工作产物，查看原文证据与评价进度。':'从团队列表进入员工记录，核对材料、评价依据与人工处理。','muted'));
  const go=node('button',employee?(latest?'查看能力结果':'提交工作产物'):'查看团队员工','btn btn-primary');go.type='button';action(go,async()=>{switchView(employee?(latest?'result':'upload'):'overview');if(employee&&latest)await loadDetail(latest.assessment_id);else if(!employee)renderOverview();});box.append(content,go);
  const catalog=state.catalogs.find(c=>c.registry_id===$('registry').value),person=state.employees.find(e=>e.employee_ref===state.employee);
  $('home-stats').replaceChildren(...(employee?[
    stat(latest?assessmentLabel(latest):'未提交','评价状态',latest?.period||'等待上传材料'),
    stat(Object.keys(catalog?.recommendations[person?.job_key]||{}).length,'岗位推荐项',person?.job_key||'未配置岗位'),
    stat(Object.values(catalog?.policies||{}).filter(p=>p.mode==='suggest').length,'AI 建议评分项','以当前标准与策略版本为准'),stat(state.materials.length,'已上传材料','仅展示当前员工材料')
  ]:[stat(state.employees.length,'授权员工'),stat(state.assessments.length,'评估记录'),stat(count,'已定稿记录'),stat(state.assessments.length-count,'未定稿记录')]));
  $('home-profile').replaceChildren(...[['账号',state.user.user_id],['身份',textLabel(state.user.role)],['当前员工',person?.employee_ref||'—'],['岗位职级',person?.job_key||'—']].map(([label,value])=>{const e=node('div',null,'profile-item');e.append(node('span',label),node('strong',value));return e;}));
  const step=latest?(final?3:latest.human_status==='in_review'?2:1):0;
  $('home-flow').replaceChildren(...[['提交材料','员工上传并确认'],['AI 预评估','生成理由与定位'],['直属上级复核','逐项判断并补充依据'],['能力结果','展示推荐值与 Gap']].map(([title,hint],i)=>{const e=node('div',null,'flow-step '+(i<step?'done':i===step?'current':''));e.append(node('strong',title),node('span',hint));return e;}));
}
// Inputs are already restricted by the API. Only the latest record within the
// selected period determines status; a previous finalized report cannot hide work.
function teamPage(employees,records,{query='',job='',period='',status='',sort='employee',page=1,size=20}={}){
  const latest=new Map();for(const r of records)if(!period||r.period===period)latest.set(r.employee_ref,r);
  query=query.trim().toLowerCase();
  const filtered=employees.map(employee=>({employee,latest:latest.get(employee.employee_ref)})).filter(({employee:e,latest:r})=>
    (!query||(e.employee_ref+' '+e.job_key).toLowerCase().includes(query))&&(!job||e.job_key===job)&&(!status||(r?.human_status||'not_submitted')===status));
  const compare=(a,b)=>a.localeCompare(b,'zh-CN',{numeric:true});
  const priority={pending:0,in_review:1,not_submitted:2,finalized:3};
  filtered.sort((a,b)=>(sort==='job'?compare(a.employee.job_key,b.employee.job_key):sort==='status'?(priority[a.latest?.human_status||'not_submitted']??4)-(priority[b.latest?.human_status||'not_submitted']??4):0)||compare(a.employee.employee_ref,b.employee.employee_ref));
  size=[10,20,50].includes(Number(size))?Number(size):20;
  const total=filtered.length,pages=Math.max(1,Math.ceil(total/size));
  page=Math.min(pages,Math.max(1,Number.isFinite(Number(page))?Math.floor(Number(page)):1));
  const start=(page-1)*size;
  return {items:filtered.slice(start,start+size),total,pages,page,size,start:total?start+1:0,end:Math.min(start+size,total)};
}
function renderOverview(){
  const rows=state.assessments;
  for(const [id,values,label] of [['team-job',state.employees.map(e=>e.job_key),'全部岗位'],['team-period',rows.map(r=>r.period),'全部周期']]){
    const select=$(id),current=select.value;select.replaceChildren(new Option(label,''),...[...new Set(values)].sort().map(v=>new Option(v,v)));
    select.value=values.includes(current)?current:'';
  }
  const result=teamPage(state.employees,rows,{query:$('team-search').value,job:$('team-job').value,period:$('team-period').value,status:$('team-status').value,sort:$('team-sort').value,page:state.teamPage,size:$('team-size').value});
  state.teamPage=result.page;
  $('overview-stats').replaceChildren(stat(state.employees.length,'授权员工'),stat(rows.length,'评估记录'),stat(rows.filter(r=>r.human_status==='finalized').length,'已定稿记录'));
  $('team-count').textContent='共 '+result.total+' 名员工，显示 '+result.start+'–'+result.end+' 名';
  $('team-page').replaceChildren(...Array.from({length:result.pages},(_,i)=>new Option(String(i+1),String(i+1))));$('team-page').value=String(result.page);
  $('team-pages').textContent='/ '+result.pages+' 页';$('team-prev').disabled=result.page===1;$('team-next').disabled=result.page===result.pages;
  const entryButton=({employee:e,latest})=>{
    const button=node('button',latest?'查看评估':'查看材料','btn btn-soft');button.type='button';button.setAttribute('aria-label',(latest?'查看评估':'查看材料')+' '+e.employee_ref);
    action(button,async()=>{state.employee=e.employee_ref;$('employee').value=e.employee_ref;await refreshEmployee();if(!state.user||state.employee!==e.employee_ref)return;switchView(latest?(state.user.role==='manager'?'review':'result'):'upload');if(latest)await loadDetail(latest.assessment_id);});return button;
  };
  $('overview-table').replaceChildren(result.total?table(['员工','岗位职级','最新周期','评估状态','操作'],result.items.map(item=>[item.employee.employee_ref,item.employee.job_key,item.latest?.period||'—',item.latest?assessmentLabel(item.latest):'未提交',entryButton(item)])):node('p','未找到匹配员工，请调整搜索或筛选条件。','empty'));
  $('team-mobile').replaceChildren(...(result.total?result.items.map(item=>{
    const card=node('article',null,'team-person');card.append(node('h3',item.employee.employee_ref),node('p',item.employee.job_key),node('p',(item.latest?.period||'当前筛选周期')+' · '+(item.latest?assessmentLabel(item.latest):'未提交'),'small muted'),entryButton(item));return card;
  }):[node('p','未找到匹配员工，请调整搜索或筛选条件。','empty')]));
}
function catalogSelection(c,{family='',track='',job='',preferredJob=''}={}){
  const jobs=Object.keys(c?.recommendations||{}).map(key=>({key,...(c.job_profiles?.[key]||{family:'未提供分类',track:'未提供分类',title:key,level:''})}));
  const preferred=jobs.find(j=>j.key===preferredJob),families=[...new Set(jobs.map(j=>j.family))];
  if(!families.includes(family))family=preferred?.family||families[0]||'';
  const tracks=[...new Set(jobs.filter(j=>j.family===family).map(j=>j.track))];
  if(!tracks.includes(track))track=tracks.includes(preferred?.track)?preferred.track:tracks[0]||'';
  const filtered=jobs.filter(j=>j.family===family&&j.track===track);
  const selected=filtered.find(j=>j.key===job)||filtered.find(j=>j.key===preferredJob)||filtered[0]||null;
  return {families,tracks,jobs:filtered,family,track,selected};
}
function renderModel(reset=false){
  if(!state.user)return;
  const current=$('model-catalog').value;
  $('model-catalog').replaceChildren(...state.catalogs.map(c=>new Option(c.title+(Object.keys(c.job_profiles||{}).length?' · 含岗位分类':''),c.registry_id)));
  if(state.catalogs.some(c=>c.registry_id===current))$('model-catalog').value=current;
  const c=state.catalogs.find(c=>c.registry_id===$('model-catalog').value);
  if(!c){$('model-status').textContent='当前没有可查看的标准。';for(const id of ['model-table','model-stats','model-job-info','model-family','model-track','model-job'])$(id).replaceChildren();return;}
  const selection=catalogSelection(c,{family:reset?'':$('model-family').value,track:reset?'':$('model-track').value,
    job:reset?'':$('model-job').value,preferredJob:state.employees.find(e=>e.employee_ref===state.employee)?.job_key||''});
  for(const [id,values,value] of [['model-family',selection.families,selection.family],['model-track',selection.tracks,selection.track]]){
    $(id).replaceChildren(...values.map(v=>new Option(v,v)));$(id).value=value;$(id).disabled=!values.length;
  }
  $('model-job').replaceChildren(...selection.jobs.map(j=>new Option([j.level,j.title].filter(Boolean).join(' · '),j.key)));
  $('model-job').value=selection.selected?.key||'';$('model-job').disabled=!selection.selected;
  const profiles=Object.values(c.job_profiles||{}),codes=Object.keys(c.criteria),rowCount=Object.values(c.recommendations).reduce((n,r)=>n+Object.keys(r).length,0);
  $('model-stats').replaceChildren(stat(profiles.length?new Set(profiles.map(j=>j.family)).size:'—','一级分类',profiles.length?'来源于当前标准':'该版本未提供分类'),
    stat(Object.keys(c.recommendations).length,'岗位职级组合'),stat(codes.length,'子能力'),stat(rowCount,'推荐值明细'));
  $('model-status').textContent=(c.synthetic?'合成试用标准':'客户候选标准，待业务签收')+'。岗位推荐值用于对照，不是员工实际评分。';
  const job=selection.selected;$('model-job-info').replaceChildren();
  if(!job){$('model-table').replaceChildren(node('p','该版本没有岗位推荐值。','empty'));return;}
  $('model-job-info').append(node('strong',job.key),node('p',[job.family,job.track,job.title,job.level].filter(Boolean).join(' · ')));
  const recs=c.recommendations[job.key];
  const cards=codes.map(code=>{
    const grade=recs[code],policy=c.policies[code],card=node('article',null,'model-item'),head=node('div',null,'model-item-head'),title=node('div');
    card.dataset.competency=code;title.append(node('strong',code+' '+(c.competency_names?.[code]||'')),node('small',dimensions[code[0]]||code[0]));
    head.append(title,node('span',grade||'—','grade-box'));card.append(head);
    card.append(node('small',grade?('ABCD'.indexOf(grade)+1)+' 分 · 岗位推荐':'岗位推荐未提供'));
    card.append(node('small','AI 参与范围：'+(policy.mode==='suggest'?'建议评分':textLabel(policy.mode))));
    for(const [label,criteria] of [['查看推荐档行为标准',(c.criteria[code]||[]).filter(item=>item.grade===grade)],['查看 A–D 全部标准',c.criteria[code]||[]]]){
      const details=node('details');details.append(node('summary',label));
      for(const criterion of criteria)details.append(node('p',criterion.grade+' · '+criterion.description));
      if(!criteria.length)details.append(node('p','当前版本未提供对应档位的标准。'));
      card.append(details);
    }
    return card;
  });
  $('model-table').replaceChildren(...cards);
}
function renderReviewGroups(){
  const d=state.detail;if(!d)return;const done=d.rows.filter(r=>r.human).length;
  $('review-progress-label').textContent='已处理 '+done+' / '+d.rows.length+' 项';$('review-progress').max=d.rows.length||1;$('review-progress').value=done;
  const groups=[['pending','待处理'],...Object.entries(dimensions).filter(([code])=>d.rows.some(r=>r.competency_id.startsWith(code)))];
  $('review-dimensions').replaceChildren(...groups.map(([key,label])=>{const button=node('button',label,'btn '+(state.reviewGroup===key?'btn-primary':'btn-secondary'));button.type='button';button.setAttribute('aria-pressed',String(state.reviewGroup===key));action(button,()=>{state.reviewGroup=key;renderReviewGroups();});return button;}));
  const shown=d.rows.filter(r=>state.reviewGroup==='pending'?!r.human:r.competency_id.startsWith(state.reviewGroup));
  $('ability-cards').replaceChildren(...shown.map(r=>{
    const card=node('article',null,'ability-card'+(!r.human?' attention':'')),head=node('div',null,'ability-head');
    head.append(node('strong',r.competency_id+' · '+(dimensions[r.competency_id[0]]||'')),node('span',r.human?.decision.grade||'待处理','score-pill'));
    const edit=node('button','复核此项','btn btn-soft');edit.type='button';action(edit,()=>{$('review-code').value=r.competency_id;resetReview();$('review-form').scrollIntoView({behavior:'smooth',block:'center'});});
    card.append(head,node('p','岗位推荐 '+(r.recommendation||'—')+' · AI '+(r.ai.grade||textLabel(r.ai.mode))),edit);return card;
  }));
  if(!shown.length)$('ability-cards').append(node('p','本组已处理完毕，可查看其他维度或完成复核。','empty'));
}
if(typeof document!=='undefined'){
const resetTeam=()=>{state.teamPage=1;renderOverview();};
$('team-search').addEventListener('input',resetTeam);
for(const id of ['team-status','team-job','team-period','team-sort','team-size'])$(id).addEventListener('change',resetTeam);
action($('team-prev'),()=>{state.teamPage--;renderOverview();});action($('team-next'),()=>{state.teamPage++;renderOverview();});
$('team-page').addEventListener('change',()=>{state.teamPage=Number($('team-page').value);renderOverview();});
action($('team-clear'),()=>{for(const id of ['team-search','team-status','team-job','team-period'])$(id).value='';$('team-sort').value='employee';resetTeam();});
$('model-catalog').addEventListener('change',()=>renderModel(true));$('model-job').addEventListener('change',()=>renderModel());
$('model-family').addEventListener('change',()=>{$('model-track').value='';$('model-job').value='';renderModel();});
$('model-track').addEventListener('change',()=>{$('model-job').value='';renderModel();});
for(const button of document.querySelectorAll('[data-account]'))action(button,()=>{$('login-account').value=button.dataset.account;$('access-token').focus();});
function selectedFile(){const file=$('upload-file').files[0];$('selected-file').textContent=file?file.name+' · '+(file.size/1024/1024).toFixed(2)+' MiB':'支持 DOCX、XLSX、PPTX 和 PDF';}
$('upload-file').addEventListener('change',selectedFile);
for(const event of ['dragenter','dragover'])$('drop-zone').addEventListener(event,e=>{e.preventDefault();$('drop-zone').classList.add('drag-over');});
$('drop-zone').addEventListener('dragleave',()=>$('drop-zone').classList.remove('drag-over'));
$('drop-zone').addEventListener('drop',e=>{e.preventDefault();$('drop-zone').classList.remove('drag-over');if(e.dataTransfer.files.length!==1){notify('请每次上传一个文件。');return;}$('upload-file').files=e.dataTransfer.files;selectedFile();});
}

function renderSourceLinks(){
  $('source-links').replaceChildren(...state.detail.material_snapshots.map((id,i)=>{const material=state.materials.find(m=>m.snapshot_id===id),b=node('button','查看材料 '+(material?.original_name||String(i+1)),'btn btn-secondary');b.type='button';action(b,()=>openEvidence(id));return b;}));
}
function rememberReviewDraft(){
  const code=state.editingCode;if(!code||!state.detail)return;
  state.reviewDrafts[code]={values:Object.fromEntries(['disposition','human-grade','review-reason','manual-basis','risk-verification'].map(id=>[id,$(id).value])),refs:state.refs.map(r=>({...r})),acks:[...state.acks]};
}
function restoreReviewDraft(code){
  const draft=state.reviewDrafts[code];state.editingCode=code;
  if(!draft)return;
  for(const [id,value] of Object.entries(draft.values))$(id).value=value;
  state.refs=draft.refs.map(r=>({...r}));state.acks=new Set(draft.acks);
}
if(typeof module!=='undefined')module.exports={teamPage,catalogSelection};
