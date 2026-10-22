"""Authorized preview with cross-process render slots and post-render revocation."""
from contextlib import contextmanager

from medic.apps.api.database import AppError
from medic.apps.api.uploads import lock_slot
from medic.ingestion.contracts import ParseError
from medic.ingestion.preview import render_file
from medic.p0.common import digest


@contextmanager
def preview_slots(vault,tenant,user):
    root=vault.root/'preview-locks';root.mkdir(exist_ok=True)
    handles=[]
    try:
        for key,count in ((digest([tenant,user]),1),('global',2)):
            handle=next((h for i in range(count) if (h:=lock_slot(root/(key+'-'+str(i)+'.lock'))) is not None),None)
            if handle is None: raise AppError('preview_busy',429)
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles): handle.close()


def preview(service,p,ident,page,width,evidence_id):
    snapshot=service.material(p,ident)
    office=snapshot['format'] in ('docx','pptx','xlsx')
    if snapshot['format']!='pdf' and not office: raise AppError('preview_not_supported',415)
    highlight=None
    if evidence_id:
        span=next((s for s in service.spans(snapshot) if s['evidence_id']==evidence_id),None)
        if span is None: raise AppError('resource_not_found',404)
        locator=span['locator']
        if office:
            from medic.ingestion.office_mapping import compact
            quote=span['quote'] if len(span['quote'])<=1024 else ''
            text=compact(quote)
            unique=bool(text) and sum(compact(s['quote']).count(text) for s in service.spans(snapshot))==1
            highlight={'mode':'unique_text','quote':quote,'quote_hash':digest(quote),'source_unique':unique}
        else:
            if locator.get('page')!=page: raise AppError('preview_evidence_page_mismatch')
            highlight=({'mode':'ocr_page'} if 'ocr_text_requires_review' in span['quality_flags'] else
                       {'block':locator.get('block'),'bbox':locator.get('bbox'),'quote_hash':digest(span['quote'])})
    with preview_slots(service.vault,p.tenant,p.user_id):
        try:
            if office:
                from medic.apps.api.office_preview import office_document
                path,conversion=office_document(service.vault,snapshot)
                info,png=render_file(path,page_number=page,width=width,source_sha256=conversion['pdf_sha256'],highlight=highlight,limits={'input_bytes':64*1024*1024})
                info={**info,'source_sha256':snapshot['source_sha256'],'derived':True,'conversion':{k:conversion[k] for k in ('preview_version','engine','version','engine_manifest_sha256','system_fonts_sha256','pdf_sha256')},'quality_state_unchanged':True}
            else:
                info,png=render_file(service.vault._blob(snapshot['scope'],snapshot['source_sha256']),
                    page_number=page,width=width,source_sha256=snapshot['source_sha256'],highlight=highlight)
        except ParseError as error:
            if str(error)=='preview_page_not_found':raise AppError('preview_page_not_found',404) from None
            if str(error).startswith(('office_active_','office_external_','office_dynamic_','office_formula_')):raise AppError('office_content_not_supported',422) from None
            if str(error)=='office_engine_unavailable':raise AppError('office_engine_unavailable',503) from None
            raise AppError('preview_unavailable',422) from None
        # A revoked grant or changed blob must never release a completed preview.
        checked=service.material(p,ident)
        if checked['source_sha256']!=info['source_sha256']:raise AppError('preview_integrity_failure',409)
        return info,png
