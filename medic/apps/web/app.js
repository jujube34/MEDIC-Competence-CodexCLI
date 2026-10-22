'use strict';
// All document text is inserted as text nodes. Credentials never enter URLs or storage.
const $ = id => document.getElementById(id);
let state = freshState(), noticeTimer;
const pendingWrites=new Map();
function freshState(){return {token:'',user:null,employees:[],catalogs:[],materials:[],assessments:[],employee:'',detail:null,refs:[],acks:new Set(),evidence:null,epoch:0,view:'home',resultGroup:'all',reviewGroup:'pending',reviewDrafts:{},editingCode:null,teamPage:1};}
const labels={employee:'员工',manager:'直属上级',director:'医学总监',admin:'管理员',planning:'规划中',evaluating:'评估中',ready_for_human_review:'AI 预评估可复核',awaiting_human_review:'需要人工处理',failed:'执行未完成',cancelled:'已取消',creation_pending:'创建待恢复',pending:'待复核',in_review:'复核中',finalized:'已定稿',ready:'解析就绪',needs_review:'有解析缺口',empty:'无可提取文本',suggested:'AI 建议',insufficient_evidence:'证据不足',not_applicable:'不适用',clue_only:'仅提供线索',human_only:'人工判断项',risk_scan:'风险扫描项',risk_signal:'待核实风险',confirmed:'确认原建议',adjusted:'人工调整',assessed:'独立人工判断',not_present:'人工核验未发现',requires_investigation:'仍需调查'};
const errors={parser_version_mismatch:'所选材料来自不同解析版本。请重新上传需要合并的旧材料，再选择同版本材料创建预评估；旧记录会保留。',authentication_required:'访问已失效，请重新登录。',resource_not_found:'该内容不存在或不在你的授权范围内。',review_not_authorized:'当前账号没有该员工的复核权限。',revision_conflict:'记录已被其他复核者修改。请刷新结果，核对后再保存。',idempotency_conflict:'重复请求内容不同，请重新操作。',production_inputs_not_enabled:'客户标准尚未签收，当前只允许合成测试。',material_requires_review:'所选材料存在解析缺口，请补充清晰材料后再创建预评估。',material_scope_mismatch:'材料必须属于当前员工和同一评估周期。',quality_acknowledgement_required:'请先确认补充材料的解析缺口。',invalid_evidence_reference:'引用与原快照不一致，请重新选择原文。',incomplete_human_review:'仍有能力项未处理，请完成全部人工判断。',risk_verification_required:'风险项仍待核验，暂不能定稿。',review_already_finalized:'该复核记录已定稿，不能再修改。',ai_package_not_ready:'AI 预评估尚未完成，请稍后刷新。',confirmation_mismatch:'确认时必须保留原 AI 档位；改分请选择“调整”。',adjustment_mismatch:'调整适用于已有 AI 档位且新档位不同的情况。',assessment_requires_no_ai_grade:'已有 AI 档位，请选择确认或调整。',upload_concurrency_limit:'当前上传或解析任务已满，请等待完成后再试。',upload_daily_quota:'最近 24 小时的上传次数或字节配额不足，请稍后再试。',upload_receive_timeout:'接收文件超时，请检查连接后重新上传。',upload_length_mismatch:'文件接收长度不一致，请重新上传。',empty_upload:'不能上传空文件。',upload_too_large:'单份材料不能超过 25 MiB。',invalid_filename:'请使用普通文件名及 PDF、DOCX、PPTX 或 XLSX 格式。',invalid_request:'请检查必填项、档位、引用和判断理由。',internal_error:'请求未完成，请稍后重试或联系本地管理员。'};
function textLabel(value){return labels[value]||value||'—';}
const issueLabels={ocr_text_requires_review:'OCR 识别文字尚未人工确认，不可自动进入评估',ocr_no_text_recognized:'OCR 未识别出文字，请核对原页',ocr_data_unavailable:'本地 OCR 语言包尚未配置',ocr_data_integrity_failure:'OCR 语言包校验失败，未执行识别',ocr_page_limit_exceeded:'已达到每份材料 5 页的 OCR 上限，该页未识别',ocr_page_size_exceeded:'页面过大，未执行 OCR',ocr_recognition_failed:'该页 OCR 失败，请核对原页',ocr_invalid_geometry:'识别坐标异常，请核对原页',formula_uncached:'公式没有已保存的计算结果',complex_formula_requires_review:'复杂公式需要核对',cell_error:'单元格包含错误值',image_content_requires_ocr_or_review:'图片内容需要核对；文字识别不包含图表含义',vector_content_not_interpreted:'图形内容尚未解释',page_without_extractable_text:'该页没有原生文字层',annotations_or_forms_require_review:'批注或表单需要核对',pdf_text_extraction_disagreement:'两种 PDF 解析结果不一致',pdf_page_count_disagreement:'两种 PDF 解析器的页数不一致',independent_pdf_extraction_failed:'独立 PDF 复核解析失败',no_extractable_text:'没有可提取文本',tracked_changes_require_review:'修订内容需要核对',drawing_not_extracted:'图片或绘图尚未提取',slide_image_not_extracted:'幻灯片图片尚未提取',slide_graphic_not_extracted:'幻灯片图形尚未提取',worksheet_drawing_not_extracted:'工作表图形尚未提取',hidden_text_requires_review:'包含隐藏文字',dynamic_content_not_resolved:'动态内容尚未解析',embedded_content_not_extracted:'嵌入内容尚未提取',external_reference_not_fetched:'外部引用未下载',macros_not_executed:'宏未执行',hidden_sheet:'包含隐藏工作表',non_worksheet_sheet:'非普通工作表需要人工查看'};
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined&&text!==null)el.textContent=String(text);if(cls)el.className=cls;return el;}
function notify(message){clearTimeout(noticeTimer);$('notice').textContent=message;$('notice').classList.remove('hidden');noticeTimer=setTimeout(()=>$('notice').classList.add('hidden'),7000);}
function showError(error){if(error.message!=='stale_request')notify(errors[error.message]||error.userMessage||'请求未完成，请检查连接后重试。');}
async function api(path,{method='GET',json,body,key,blob=false,preview=false}={}){
  const token=state.token,headers={Authorization:'Bearer '+token};
  if(json!==undefined){headers['Content-Type']='application/json';body=JSON.stringify(json);}
  if(key)headers['Idempotency-Key']=key;
  const response=await fetch(path,{method,headers,body,cache:'no-store',credentials:'omit'});
  if(token!==state.token)throw new Error('stale_request');
  if(!response.ok){const data=await response.json().catch(()=>({}));if(response.status===401)logout();throw new Error(data.error||'internal_error');}
  const result=preview?{image:await response.blob(),info:JSON.parse(response.headers.get('X-Preview-Info')||'{}')}:blob?await response.blob():await response.json();
  if(token!==state.token)throw new Error('stale_request');
  return result;
}
function uid(){return 'web-'+crypto.randomUUID();}
async function write(path,body){const signature=JSON.stringify([state.user.tenant,state.user.user_id,path,body]);if(!pendingWrites.has(signature))pendingWrites.set(signature,uid());const result=await api(path,{method:'POST',json:body,key:pendingWrites.get(signature)});pendingWrites.delete(signature);return result;}
function action(element,fn){element.addEventListener('click',()=>Promise.resolve().then(fn).catch(showError));}
function bindForm(id,fn){$(id).addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;if(button)button.disabled=true;try{await fn();}catch(error){showError(error);}finally{if(button)button.disabled=false;if(id==='create-form')renderCatalog();}});}
function logout(){++evidenceRequest;clearPreview();closeCriterion();closeResultChart();state=freshState();pendingWrites.clear();pendingCreation=null;$('access-token').value='';$('login-account').value='';$('team-search').value='';$('team-status').value='';$('team-job').replaceChildren();$('team-period').replaceChildren();$('team-sort').value='employee';$('team-size').value='20';$('team-page').replaceChildren();$('team-pages').textContent='';$('team-mobile').replaceChildren();$('upload-form').reset();$('review-form').reset();$('finalize-form').reset();for(const id of ['result-stats','result-filters','result-evidence','grade-standards','home-task','home-stats','home-profile','home-flow','model-table','model-status','model-catalog','model-family','model-track','model-job','model-stats','model-job-info','source-links','ability-cards','review-dimensions','main-navigation','bottom-navigation','team-count','employee','registry','competencies','review-code','identity','evidence-title','evidence-status','result-title','result-status','result-boundary','materials','assessments','evidence-blocks','quality-issues','selected-refs','result-table','ai-details','audit','versions','overview-table','overview-stats','radar','final-summary'])$(id).replaceChildren();$('workspace').classList.add('hidden');$('login').classList.remove('hidden');}
bindForm('login-form',async()=>{
  state.token=$('access-token').value.trim();state.user=await api('/me');
  if($('login-account').value.trim()&&$('login-account').value.trim()!==state.user.user_id){logout();notify('账号与访问凭据不一致，请核对后重新登录。');return;}
  const [employees,catalogs,assessments]=await Promise.all([api('/employees'),api('/catalogs'),api('/assessments')]);
  state.employees=employees;state.catalogs=catalogs;state.assessments=assessments;$('team-size').value=window.matchMedia('(max-width:820px)').matches?'10':'20';
  $('access-token').value='';$('identity').textContent=state.user.user_id+' · '+textLabel(state.user.role);
  $('role-heading').textContent=textLabel(state.user.role)+'工作台';$('page-title').textContent=state.user.role==='employee'?'材料与预评估':'证据与人工复核';
  $('employee').replaceChildren(...employees.map(e=>new Option(e.employee_ref+' · '+e.job_key,e.employee_ref)));
  $('registry').replaceChildren(...catalogs.map(c=>new Option(c.title+(c.synthetic?' · 合成':' · 待签收'),c.registry_id)));
  $('login').classList.add('hidden');$('workspace').classList.remove('hidden');renderNavigation();switchView('home');
  state.employee=employees[0]?.employee_ref||'';renderCatalog();await refreshEmployee();renderOverview();renderHome();renderModel();
});
action($('logout'),logout);
$('employee').addEventListener('change',()=>{state.employee=$('employee').value;refreshEmployee().catch(showError);});
$('period').addEventListener('change',()=>renderMaterials());
$('registry').addEventListener('change',renderCatalog);
async function refreshEmployee(){
  ++evidenceRequest;clearPreview();closeCriterion();closeResultChart();const epoch=++state.epoch;state.detail=null;state.refs=[];state.acks.clear();state.evidence=null;
  $('result').classList.add('hidden');$('evidence').classList.add('hidden');
  if(!state.employee){state.materials=[];renderMaterials();renderAssessments();return;}
  const [materials,assessments]=await Promise.all([api('/employees/'+encodeURIComponent(state.employee)+'/materials'),api('/assessments')]);
  if(epoch!==state.epoch)return;state.materials=materials;state.assessments=assessments;renderMaterials();renderAssessments();renderCatalog();renderHome();
}
action($('refresh'),async()=>{const id=state.detail?.assessment_id;await refreshEmployee();if(id)await loadDetail(id);notify('已刷新当前记录。');});
action($('refresh-overview'),async()=>{const [employees,assessments]=await Promise.all([api('/employees'),api('/assessments')]);state.employees=employees;state.assessments=assessments;renderOverview();});
function renderCatalog(){
  const c=state.catalogs.find(c=>c.registry_id===$('registry').value),employee=state.employees.find(e=>e.employee_ref===state.employee);
  const selected=new Set([...$('competencies').querySelectorAll('input:checked')].map(i=>i.value));
  $('competencies').replaceChildren();
  if(!c){$('catalog-status').textContent='当前没有可用标准。';$('create-assessment').disabled=true;return;}
  const count=Object.values(c.recommendations).reduce((n,row)=>n+Object.keys(row).length,0);
  $('catalog-status').textContent=(c.synthetic?'合成软件验证标准':'客户候选标准，尚未业务签收')+' · '+Object.keys(c.recommendations).length+' 个岗位 · '+count+' 条推荐值';
  for(const [code,p] of Object.entries(c.policies)){const label=node('label'),check=document.createElement('input');check.type='checkbox';check.name='competency';check.value=code;check.checked=selected.size?selected.has(code):true;label.append(check,node('span',code+' · '+(p.mode==='suggest'?'建议评分':textLabel(p.mode))));$('competencies').append(label);}
  $('create-assessment').disabled=!c.synthetic||!employee?.synthetic||!c.recommendations[employee.job_key];
}
function renderMaterials(){
  const parent=$('materials');parent.replaceChildren();$('material-count').textContent=state.materials.length+' 份';
  if(!state.materials.length){parent.append(node('p','还没有材料。上传后可查看原文和解析缺口。','empty'));return;}
  for(const m of state.materials){const card=node('div',null,'material-card'),head=node('div',null,'material-head'),check=document.createElement('input');
    check.type='checkbox';check.name='material';check.value=m.snapshot_id;check.setAttribute('aria-label','选择材料 '+m.original_name);check.disabled=m.state!=='ready'||m.period!==$('period').value;
    const title=node('div');title.append(node('strong',m.original_name),node('div',m.period+' · '+textLabel(m.state)+' · '+m.block_count+' 段原文','status'+(m.state!=='ready'?' warning':'')));head.append(check,title);
    const actions=node('div',null,'material-actions'),view=node('button','查看原文','text-button'),download=node('button','下载原件','text-button');view.type=download.type='button';action(view,()=>openEvidence(m.snapshot_id));action(download,()=>downloadSource(m));actions.append(view,download);card.append(head,actions);parent.append(card);
  }
}
bindForm('upload-form',async()=>{
  const file=$('upload-file').files[0];if(!file||!state.employee)return;
  if(file.size>25*1024*1024)throw new Error('upload_too_large');
  const employee=state.employee,epoch=state.epoch;
  notify('正在解析材料，请稍候。');
  const result=await api('/employees/'+encodeURIComponent(employee)+'/materials?'+new URLSearchParams({filename:file.name,period:$('period').value}),{method:'POST',body:file});
  if(epoch!==state.epoch)return;
  state.materials=await api('/employees/'+encodeURIComponent(employee)+'/materials');$('upload-file').value='';$('selected-file').textContent='支持 DOCX、XLSX、PPTX 和 PDF';renderMaterials();await openEvidence(result.snapshot_id);notify('材料已保存：'+textLabel(result.state)+'。');
});
// Keep the same creation key after an uncertain network failure, allowing safe recovery.
let pendingCreation=null;
bindForm('create-form',async()=>{
  const body={employee_ref:state.employee,period:$('period').value,registry_id:$('registry').value,
    material_ids:[...$('materials').querySelectorAll('input:checked')].map(i=>i.value),competency_scope:[...$('competencies').querySelectorAll('input:checked')].map(i=>i.value)};
  if(!body.material_ids.length||!body.competency_scope.length){notify('请勾选同一周期的就绪材料和评估范围。');return;}
  const signature=JSON.stringify([state.user?.tenant,state.user?.user_id,body]);if(pendingCreation?.signature!==signature)pendingCreation={signature,key:uid()};
  const result=await api('/assessments',{method:'POST',json:body,key:pendingCreation.key});pendingCreation=null;
  state.assessments=await api('/assessments');renderAssessments();switchView('result');await loadDetail(result.assessment_id);notify('预评估已入队。由本地运行服务执行后，可刷新查看进度。');
});
function renderAssessments(){const parent=$('assessments');parent.replaceChildren();const rows=state.assessments.filter(a=>a.employee_ref===state.employee);if(!rows.length){parent.append(node('p','尚无评估记录。创建后可在这里查看进度。','empty'));return;}
  for(const a of rows){const button=node('button',null,'assessment-card'+(state.detail?.assessment_id===a.assessment_id?' selected':''));button.type='button';button.append(node('strong',a.period),node('div',textLabel(a.human_status)+' · 修订 '+a.revision,'status'),node('div',a.assessment_id,'small muted'));action(button,async()=>{switchView(state.user.role==='employee'?'result':'review');await loadDetail(a.assessment_id);});parent.append(button);}}
let detailRequest=0;
async function loadDetail(id){const epoch=state.epoch,request=++detailRequest;const [detail,history]=await Promise.all([api('/assessments/'+id),api('/assessments/'+id+'/history')]);if(epoch!==state.epoch||request!==detailRequest)return;closeCriterion();state.detail=detail;state.resultGroup='all';state.reviewDrafts={};state.editingCode=null;state.refs=[];state.acks.clear();renderDetail(history);renderAssessments();switchView(state.view);$('result').scrollIntoView({behavior:'smooth',block:'start'});}
function table(headers,rows){const t=node('table'),head=node('thead'),tr=node('tr');for(const h of headers)tr.append(node('th',h));head.append(tr);t.append(head);const body=node('tbody');for(const row of rows){const tr=node('tr');for(const value of row){const td=node('td');td.append(value instanceof Node?value:document.createTextNode(String(value)));tr.append(td);}body.append(tr);}t.append(body);return t;}
function gradeCell(grade,sub,cls){const cell=node('div');cell.append(node('span',grade??'—','grade '+(cls||'')));if(sub)cell.append(node('small',sub));return cell;}
function renderDetail(history){
  const d=state.detail;$('result').classList.remove('hidden');$('result-title').textContent=d.employee_ref+' · '+d.period;$('result-status').textContent=textLabel(d.human_status);
  const passed=d.ai_report?.gate?.passed;
  $('result-boundary').textContent=passed===true?(d.final?'原 AI 预评估检查通过，人工复核已另行定稿。':'AI 预评估检查通过，仍需上级逐项复核。'):passed===false?(d.final?'原 AI 检查保留未解决事项；本记录已由人工独立处理，未更改 AI 检查。':'AI 检查保留未解决事项，请人工核验。人工定稿不会把 AI 检查改为通过。'):'当前状态：'+textLabel(d.runtime_state)+'。评估尚未形成可复核包。';
  $('ai-details').replaceChildren();for(const a of d.ai_report?.assessments||[]){const box=node('div');box.append(node('h3',a.competency_id+' · '+textLabel(a.status)),node('p',(a.limitations||[]).join('；')));for(const ref of a.supporting_evidence||[]){const quote=node('div',ref.quote,'ref'),button=node('button','定位该引用','text-button');button.type='button';action(button,()=>locateReference(ref));quote.append(node('br'),button);box.append(quote);}$('ai-details').append(box);}if(!d.ai_report)$('ai-details').append(node('p','评估尚未完成。','muted'));
  const canReview=d.permissions.can_review&&d.human_status!=='finalized'&&!!d.ai_report;
  $('review-area').classList.toggle('hidden',!canReview);$('review-code').replaceChildren(...d.rows.map(r=>new Option(r.competency_id,r.competency_id)));resetReview();
  $('final-summary').classList.toggle('hidden',!d.final);$('final-summary').textContent=d.final?'合成复核已定稿。复核人：'+d.final.reviewer+'。处理说明：'+d.final.limitation_disposition:'';
  $('versions').textContent=Object.entries(d.versions).map(([key,value])=>key+': '+value).join('\n');
  $('audit').replaceChildren(...history.map(e=>{const names={assessment_requested:'创建评估',runtime_linked:'关联执行任务',human_review_saved:'保存人工判断',human_review_finalized:'完成复核定稿'};const item=node('p',(names[e.kind]||e.kind)+' · '+e.actor+' · '+new Date(e.created*1000).toLocaleString('zh-CN'));if(e.kind==='human_review_saved')for(const decision of e.body.decisions||[])item.append(node('span','\n'+decision.competency_id+'：'+textLabel(decision.disposition)+' '+(decision.grade??'—')+'；'+decision.reason));return item;}));
  renderSourceLinks();renderReviewGroups();renderResultVisuals();if(state.evidence)renderEvidenceBlocks(false);
}
function resetReview(){closeCriterion();rememberReviewDraft();const row=state.detail?.rows.find(r=>r.competency_id===$('review-code').value);if(!row)return;const existing=row.human?.decision;
  $('disposition').value=existing?.disposition||(row.ai.grade?'confirmed':'insufficient_evidence');$('human-grade').value=existing?.grade||row.ai.grade||'';$('review-reason').value=existing?.reason||'';$('manual-basis').value=existing?.manual_basis||'';
  $('risk-field').classList.toggle('hidden',row.ai.mode!=='risk_scan');$('risk-verification').value=existing?.risk_verification||'';
  const materialIds=[...state.detail.material_snapshots,...(row.human?.supplemental_material_ids||[])];
  state.refs=(existing?.evidence_refs||[]).map(ref=>({...ref,snapshot_id:materialIds.find(id=>ref.evidence_id.startsWith('material-'+id.replace('snapshot-','')+'-'))}));
  state.acks=new Set(row.human?.acknowledged_quality_issues||[]);restoreReviewDraft(row.competency_id);renderRefs();syncGrade();renderGradeStandards();if(state.evidence)renderEvidenceBlocks(false);}
$('review-code').addEventListener('change',resetReview);
function syncGrade(){const empty=['insufficient_evidence','not_applicable'].includes($('disposition').value);$('human-grade').disabled=empty;if(empty)$('human-grade').value='';}
$('disposition').addEventListener('change',syncGrade);
action($('clear-refs'),()=>{state.refs=[];renderRefs();});
function renderRefs(){const parent=$('selected-refs');parent.replaceChildren();if(!state.refs.length)parent.append(node('p','请在原文证据中选择“引用此段”，或填写明确的人工观察依据。'));for(const ref of state.refs)parent.append(node('div',ref.quote,'ref'));}
bindForm('review-form',async()=>{
  const d=state.detail,grade=$('human-grade').value||null,disposition=$('disposition').value,reason=$('review-reason').value.trim(),manual_basis=$('manual-basis').value.trim();
  if(reason.length<5||grade&&!state.refs.length&&manual_basis.length<8){notify('请填写判断理由，并提供原文引用或至少 8 个字符的人工观察依据。');return;}
  const supplemental=[...new Set(state.refs.map(r=>r.snapshot_id).filter(id=>!d.material_snapshots.includes(id)))];
  const body={expected_revision:d.revision,decisions:[{competency_id:$('review-code').value,disposition,grade,reason,manual_basis,
    evidence_refs:state.refs.map(({evidence_id,quote,span_hash})=>({evidence_id,quote,span_hash})),risk_verification:$('risk-field').classList.contains('hidden')?null:$('risk-verification').value||null}],
    supplemental_material_ids:supplemental,acknowledged_quality_issues:[...state.acks]};
  await write('/assessments/'+d.assessment_id+'/reviews',body);await loadDetail(d.assessment_id);state.assessments=await api('/assessments');renderAssessments();notify('人工判断已保存，原 AI 建议未改变。');
});
bindForm('finalize-form',async()=>{const d=state.detail,reason=$('limitations').value.trim();if(reason.length<5){notify('请填写限制与未解问题的处理说明。');return;}
  await write('/assessments/'+d.assessment_id+'/finalize',{expected_revision:d.revision,limitation_disposition:reason});$('limitations').value='';await loadDetail(d.assessment_id);state.assessments=await api('/assessments');renderAssessments();notify('合成复核记录已定稿。');});
let evidenceRequest=0;
async function openEvidence(id,offset=0,focusRef=null){if(!focusRef&&offset===0&&state.evidence?.material.snapshot_id===id&&previewState?.busy){$('evidence').classList.remove('hidden');$('evidence').scrollIntoView({behavior:'smooth',block:'start'});return;}const request=++evidenceRequest,epoch=state.epoch;clearPreview();const [material,blocks]=await Promise.all([api('/materials/'+id),api('/materials/'+id+'/blocks?offset='+offset)]);if(epoch!==state.epoch||request!==evidenceRequest)return;state.evidence={material,blocks:blocks.blocks,total:blocks.total,offset};$('evidence').classList.remove('hidden');$('evidence-title').textContent=material.original_name;renderEvidenceBlocks(true);$('evidence').scrollIntoView({behavior:'smooth',block:'start'});const target=blocks.blocks.find(b=>b.evidence_id===focusRef);await openOriginalPreview(material,target?.locator.page||blocks.blocks[0]?.locator.page||1,focusRef);}
async function locateReference(ref,supplemental=[]){const epoch=state.epoch,assessment=state.detail.assessment_id;const id=[...state.detail.material_snapshots,...supplemental].find(id=>ref.evidence_id.startsWith('material-'+id.replace('snapshot-','')+'-'));if(!id){notify('该历史引用未关联可定位的上传快照。');return;}const located=await api('/materials/'+id+'/blocks/'+encodeURIComponent(ref.evidence_id));if(epoch!==state.epoch||state.detail?.assessment_id!==assessment)return;if(located.block.span_hash!==ref.span_hash||located.block.quote!==ref.quote)throw new Error('invalid_evidence_reference');await openEvidence(id,Math.floor(located.index/50)*50,ref.evidence_id);if(epoch!==state.epoch||state.detail?.assessment_id!==assessment||state.evidence?.material.snapshot_id!==id)return;const block=$('evidence-blocks').querySelector('[data-evidence-id="'+CSS.escape(ref.evidence_id)+'"]');if(block){block.classList.add('selected-evidence');if(!state.evidence.material.preview?.supported)block.scrollIntoView({behavior:'smooth',block:'center'});}}
function renderEvidenceBlocks(reset){const evidence=state.evidence;if(!evidence)return;const {material,blocks,total}=evidence;
  $('evidence-status').textContent=(material.state==='failed'?'解析失败':textLabel(material.state))+' · 当前显示 '+(blocks.length?evidence.offset+1:0)+'–'+(evidence.offset+blocks.length)+' / '+total+' 段 · '+material.period;
  const issues=$('quality-issues');issues.replaceChildren();for(const issue of material.issues){const label=node('label',null,'quality-ack');if(issue.blocking){const check=document.createElement('input');check.type='checkbox';const key=material.snapshot_id+':'+issue.code;check.checked=state.acks.has(key);check.addEventListener('change',()=>{if(check.checked)state.acks.add(key);else state.acks.delete(key);});label.append(check);}label.append(node('span',(issue.blocking?'我已核对该解析缺口：':'解析提示：')+(issueLabels[issue.code]||'该位置的解析结果需要人工核对')+' · '+issue.locator));issues.append(label);}
  const parent=$('evidence-blocks');parent.replaceChildren();for(const block of blocks){const card=node('div',null,'evidence-block');card.dataset.evidenceId=block.evidence_id;if(block.quality_flags.includes('ocr_text_requires_review'))card.append(node('p','OCR 识别文字 · 待人工核对。请逐字对照原页；系统未提供识别置信度。','callout'));card.append(node('div',block.locator.page?('第 '+block.locator.page+' 页 · '+(block.locator.ocr_block?'识别段落 '+block.locator.ocr_block:'文本段落 '+block.locator.block)):Object.entries(block.locator).map(([k,v])=>k+'：'+JSON.stringify(v)).join(' · '),'locator'),node('pre',block.quote));
    if(material.preview?.supported){const locate=node('button',material.format==='pdf'?'在原件中定位':'在预览中定位','text-button');locate.type='button';action(locate,async()=>{await openOriginalPreview(material,block.locator.page||1,block.evidence_id);$('original-preview').scrollIntoView({behavior:'smooth',block:'start'});});card.append(locate);}
    if(state.detail?.permissions.can_review&&state.detail.human_status!=='finalized'&&state.detail.ai_report){const add=node('button','引用此段','text-button');add.type='button';action(add,()=>{if(material.period!==state.detail.period)throw new Error('material_scope_mismatch');if(!state.refs.some(r=>r.evidence_id===block.evidence_id))state.refs.push({...block,snapshot_id:material.snapshot_id});renderRefs();notify('已加入本次人工判断的引用。');});card.append(add);}parent.append(card);}
  $('more-evidence').classList.toggle('hidden',evidence.offset+blocks.length>=total);
}
action($('more-evidence'),async()=>{const evidence=state.evidence;if(!evidence)return;const next=await api('/materials/'+evidence.material.snapshot_id+'/blocks?offset='+(evidence.offset+evidence.blocks.length));if(state.evidence!==evidence)return;evidence.blocks.push(...next.blocks);renderEvidenceBlocks(false);});
action($('close-evidence'),()=>{++evidenceRequest;clearPreview();$('evidence').classList.add('hidden');});
async function downloadSource(material){const blob=await api('/materials/'+material.snapshot_id+'/source',{blob:true});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=material.original_name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
