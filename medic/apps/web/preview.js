'use strict';
let previewRequest=0,previewUrl=null,previewState=null;
function clearPreview(){
  ++previewRequest;previewState=null;
  if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=null;
  $('preview-stage').replaceChildren();$('preview-stage').className='preview-stage';$('preview-zoom').value='fit';
  $('preview-status').textContent='';$('preview-unavailable').textContent='';
  $('preview-unavailable').classList.add('hidden');$('original-preview').classList.add('hidden');
}
async function openOriginalPreview(material,page=1,evidenceId=null){
  if(state.evidence?.material.snapshot_id!==material.snapshot_id)return;
  if(!material.preview?.supported){
    clearPreview();$('preview-unavailable').textContent='此格式暂未提供原件页图。下方保留提取文本及结构定位，可在材料列表下载原件核对。';$('preview-unavailable').classList.remove('hidden');return;
  }
  const request=++previewRequest,epoch=state.epoch;
  previewState={material,page,evidenceId,busy:true};
  if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=null;$('preview-stage').replaceChildren();
  $('preview-unavailable').classList.add('hidden');$('original-preview').classList.remove('hidden');
  $('preview-status').textContent='正在生成第 '+page+' 页…';previewControls();
  try{
    const params=new URLSearchParams({page:String(page),width:'1200'});if(evidenceId)params.set('evidence_id',evidenceId);
    const result=await api('/materials/'+material.snapshot_id+'/preview?'+params,{preview:true});
    if(request!==previewRequest||epoch!==state.epoch||state.evidence?.material.snapshot_id!==material.snapshot_id)return;
    const info=result.info;
    if(info.source_sha256!==material.source_sha256||!Number.isInteger(info.page)||info.page<1||info.page>500||((material.format==='pdf'||!evidenceId)&&info.page!==page))throw new Error('preview_integrity_failure');
    page=info.page;
    previewState={material,page,evidenceId,busy:false,total:info.page_count};
    previewUrl=URL.createObjectURL(result.image);const img=document.createElement('img');img.src=previewUrl;
    img.alt=material.original_name+' · 第 '+page+' 页'+(['exact_block','unique_text'].includes(info.highlight)?' · 红框定位文本':'');
    img.width=info.width;img.height=info.height;$('preview-stage').replaceChildren(img);
    $('preview-status').textContent='第 '+page+' / '+info.page_count+' 页'+(info.rotation?' · 原件旋转 '+info.rotation+'°':'')+' · '+(info.highlight==='exact_block'?'红框已定位原文块':info.highlight==='unique_text'?'红框为唯一文本匹配位置':info.highlight==='ocr_page_only'?'已打开 OCR 对应原页；识别文字待人工核对，不显示原生文本红框':info.highlight==='unavailable'?'无法唯一匹配此引用，请核对下方原文':'页图预览')+(info.derived?' · LibreOffice '+info.conversion.version+' 转换页；字体、分页或公式显示可能与 Office 不同，评分依据仍为原文。':'');
  }catch(error){
    if(request!==previewRequest||epoch!==state.epoch)return;
    previewState={material,page,evidenceId,busy:false,total:material.preview.page_count};
    const messages={preview_busy:'预览任务繁忙，请稍后重试。',office_content_not_supported:'此文件含外部资源、动态内容或暂不支持的公式，未执行转换。请下载原件核对；下方原文仍保留。',office_engine_unavailable:'本地 Office 转换器尚未配置，请下载原件核对。',preview_page_not_found:'该页不存在，请核对页码。',preview_unavailable:'此页暂无法生成预览，请下载原件核对；提取文本仍保留。',preview_integrity_failure:'页图与当前材料不一致，请刷新后重试。'};
    $('preview-status').textContent=messages[error.message]||errors[error.message]||'页图未加载，请稍后重试。';
  }finally{if(request===previewRequest)previewControls();}
}
function previewControls(){
  const s=previewState;if(!s)return;
  const total=s.total||s.material.preview.page_count||500;
  $('preview-page').value=s.page;$('preview-page').max=total;$('preview-page').disabled=s.busy;
  $('preview-prev').disabled=s.busy||s.page<=1;$('preview-next').disabled=s.busy||s.page>=total;
  $('preview-page-form').querySelector('button').disabled=s.busy;
}
action($('preview-prev'),()=>{const s=previewState;if(s&&!s.busy)return openOriginalPreview(s.material,s.page-1);});
action($('preview-next'),()=>{const s=previewState;if(s&&!s.busy)return openOriginalPreview(s.material,s.page+1);});
bindForm('preview-page-form',()=>{const s=previewState;if(s&&!s.busy)return openOriginalPreview(s.material,Number($('preview-page').value));});
$('preview-zoom').addEventListener('change',()=>{$('preview-stage').className='preview-stage zoom-'+$('preview-zoom').value;});
