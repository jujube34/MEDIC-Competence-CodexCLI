"""Loopback API. Local bearer identities are not a replacement for customer SSO."""
from pathlib import Path
import json
import re
import anyio

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from medic.apps.api.contracts import CreateAssessment, ReviewRequest, FinalizeRequest
from medic.apps.api.database import AppError
from medic.apps.api.service import Service
from medic.apps.api.uploads import UploadAdmission
from medic.ingestion.contracts import ParseError, ParseLimits


def create_app(database, vault, *, upload_policy=None):
    from medic.runtime import build
    build.verify(build.VERSION)
    service=Service(database,vault)
    uploads=UploadAdmission(service.vault,upload_policy)
    app=FastAPI(title='MEDIC local synthetic verification',debug=False,docs_url=None,redoc_url=None,
                telemetry={'tracing':False,'metrics':False,'logs':False,'operation_spans':False,'auto_configure':False})
    app.state.service=service
    app.state.uploads=uploads
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])

    @app.middleware('http')
    async def private_response(request, call_next):
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return JSONResponse({'error':error.code},status_code=error.status)

    @app.exception_handler(ParseError)
    async def parse_error(request, error):
        return JSONResponse({'error':'material_parse_failed'},status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({'error':'invalid_request','fields':[{'location':e['loc'],'type':e['type']} for e in error.errors()]},status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        return JSONResponse({'error':'internal_error'},status_code=500,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    def principal(authorization: str | None=Header(default=None)):
        if not authorization or not authorization.startswith('Bearer ') or len(authorization)>256:
            raise AppError('authentication_required',401)
        return service.db.authenticate(authorization[7:])

    @app.get('/health')
    def health(): return {'status':'ok','synthetic_execution_only':True}

    @app.get('/',include_in_schema=False)
    def workspace(): return FileResponse(build.ROOT/'apps/web/index.html')

    @app.get('/assets/{name}',include_in_schema=False)
    def asset(name: str):
        if name not in ('app.js','app.css','demo-theme.css','alignment.css','navigation.js','results.js','preview.js'): raise AppError('resource_not_found',404)
        return FileResponse(build.ROOT/'apps/web'/name)

    @app.get('/me')
    def me(p=Depends(principal)):
        with service.db.transaction(p.tenant) as db:
            user=service.db.user(db,p)
            return {k:user[k] for k in ('tenant','user_id','employee_ref','role')}

    @app.get('/employees')
    def employees(p=Depends(principal)): return service.employees(p)

    @app.get('/catalogs')
    def catalogs(p=Depends(principal)): return service.catalogs(p)

    @app.get('/employees/{employee}/materials')
    def materials(employee: str,p=Depends(principal)): return service.materials(p,employee)

    @app.post('/employees/{employee}/materials',status_code=201)
    async def upload(employee: str,request: Request,filename: str=Query(min_length=1,max_length=180),period: str=Query(min_length=1,max_length=64),p=Depends(principal)):
        # Restrict the filename before using it as a local temporary leaf.
        if re.search(r'[\\/:<>"|?*\x00-\x1f]',filename) or filename.endswith((' ','.')) or filename.startswith('.') or Path(filename).suffix.lower() not in ('.pdf','.docx','.pptx','.xlsx'):
            raise AppError('invalid_filename')
        if re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',filename): raise AppError('invalid_filename')
        with service.db.transaction(p.tenant) as db: service.db.authorize(db,p,employee)
        limit=ParseLimits().input_bytes
        length=request.headers.get('content-length')
        if length is not None and (not length.isascii() or not length.isdigit() or len(length)>10 or int(length)>limit): raise AppError('upload_too_large',413)
        permit=uploads.acquire(p.tenant,p.user_id)
        try:
            path=permit.temporary_path(filename)
            size=0
            try:
                with anyio.fail_after(uploads.policy.receive_seconds):
                    with path.open('wb') as output:
                        async for chunk in request.stream():
                            size+=len(chunk)
                            permit.received(size)
                            if size>limit: raise AppError('upload_too_large',413)
                            output.write(chunk)
            except TimeoutError:
                raise AppError('upload_receive_timeout',408) from None
            if size==0: raise AppError('empty_upload')
            if length is not None and size!=int(length): raise AppError('upload_length_mismatch')
            return await run_in_threadpool(permit.process,service.ingest,p,employee,path,period)
        finally:
            permit.cancel_pending()

    @app.get('/materials/{ident}')
    def material(ident: str,p=Depends(principal)): return service.material_summary(service.material(p,ident))

    @app.get('/materials/{ident}/preview')
    def material_preview(ident: str,page: int=Query(default=1,ge=1,le=500),
                         width: int=Query(default=1200,ge=320,le=1600),
                         evidence_id: str | None=Query(default=None,max_length=200),p=Depends(principal)):
        from medic.apps.api.preview import preview
        info,png=preview(service,p,ident,page,width,evidence_id)
        return Response(png,media_type='image/png',headers={'X-Preview-Info':json.dumps(info,separators=(',',':'))})

    @app.get('/materials/{ident}/blocks')
    def blocks(ident: str,offset: int=Query(default=0,ge=0),limit: int=Query(default=50,ge=1,le=100),p=Depends(principal)):
        s=service.material(p,ident); spans=service.spans(s)
        return {'snapshot_id':ident,'state':s['extraction']['state'],'total':len(spans),'offset':offset,'blocks':spans[offset:offset+limit]}

    @app.get('/materials/{ident}/source')
    def source(ident: str,p=Depends(principal)):
        s=service.material(p,ident)
        data=service.vault._blob(s['scope'],s['source_sha256']).read_bytes()
        from medic.p0.common import digest
        if digest(data)!=s['source_sha256']: raise AppError('source_integrity_failure',409)
        return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="{ident}.{s["format"]}"'})

    @app.get('/materials/{ident}/blocks/{evidence_id}')
    def locate_evidence(ident: str,evidence_id: str,p=Depends(principal)):
        s=service.material(p,ident); spans=service.spans(s)
        for index,span in enumerate(spans):
            if span['evidence_id']==evidence_id:
                return {'snapshot_id':ident,'index':index,'block':span,'total':len(spans)}
        raise AppError('resource_not_found',404)

    @app.post('/assessments',status_code=201)
    def create(body: CreateAssessment,p=Depends(principal),idempotency_key: str | None=Header(default=None)):
        return service.create(p,body,idempotency_key)

    @app.get('/assessments')
    def assessments(p=Depends(principal)): return service.assessments(p)

    @app.get('/assessments/{ident}')
    def detail(ident: str,p=Depends(principal)): return service.detail(p,ident)

    @app.post('/assessments/{ident}/reviews')
    def review(ident: str,body: ReviewRequest,p=Depends(principal),idempotency_key: str | None=Header(default=None)):
        return service.review(p,ident,body,idempotency_key)

    @app.post('/assessments/{ident}/finalize')
    def finalize(ident: str,body: FinalizeRequest,p=Depends(principal),idempotency_key: str | None=Header(default=None)):
        return service.finalize(p,ident,body,idempotency_key)

    @app.get('/assessments/{ident}/history')
    def history(ident: str,p=Depends(principal)): return service.history(p,ident)

    return app
