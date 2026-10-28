"""Pinned, bounded local Office conversion. It never changes extracted evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from medic.ingestion.bounded import WindowsJob
from medic.ingestion.contracts import ParseError, ParseLimits
from medic.p0.common import ROOT, digest
from medic.runtime import build

OFFICE_VERSION='medic-office-preview-v1'
ENGINE_VERSION='26.8.1.1'
ENGINE_MANIFEST_SHA256='1a2c3007759ec23bfe1190424f7d1dfc1ff753f139a567154ee113fb386735fa'
MAX_PDF_BYTES=64*1024*1024


def engine_root():
    return Path(os.environ.get('MEDIC_OFFICE_ENGINE_ROOT',str(ROOT/'medic/artifacts/office-converter/26.8.1'))).resolve()


def engine_available():
    root=engine_root()
    return os.name=='nt' and (root/'program/soffice.exe').is_file() and (root.parent/(root.name+'.manifest.json')).is_file()


def verify_engine(root):
    root=Path(root).resolve()
    if os.name!='nt': raise ParseError('office_engine_unavailable')
    try:
        raw=(root.parent/(root.name+'.manifest.json')).read_bytes()
        if digest(raw)!=ENGINE_MANIFEST_SHA256: raise ParseError('office_engine_changed')
        manifest=json.loads(raw)
        actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.suffix.lower()!='.msi'}
        if actual!=set(manifest['files']): raise ParseError('office_engine_changed')
        for name,expected in manifest['files'].items():
            path=root/name
            if path.is_symlink() or not path.resolve().is_relative_to(root) or digest(path.read_bytes())!=expected:
                raise ParseError('office_engine_changed')
        return {'engine':'LibreOffice','version':ENGINE_VERSION,'engine_manifest_sha256':ENGINE_MANIFEST_SHA256}
    except ParseError: raise
    except Exception: raise ParseError('office_engine_unavailable') from None


def font_identity():
    roots=[Path(os.environ['WINDIR'])/'Fonts',Path(os.environ['LOCALAPPDATA'])/'Microsoft/Windows/Fonts']
    files={}
    for i,root in enumerate(roots):
        if root.exists():
            for p in sorted(root.rglob('*')):
                if p.is_file() and p.suffix.lower() in ('.ttf','.ttc','.otf','.fon'):
                    files[str(i)+'/'+p.relative_to(root).as_posix()]=digest(p.read_bytes())
    return {'system_fonts_sha256':digest(files),'font_files':len(files)}


def convert_file(path, *, kind, source_sha256, limits=None):
    limits=ParseLimits.model_validate(limits or {})
    path=Path(path).resolve()
    if path.stat().st_size>limits.input_bytes: raise ParseError('input_size_exceeded')
    if not engine_available(): raise ParseError('office_engine_unavailable')
    identity=build.verify_once(build.VERSION)
    env={k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','PYTHONNOUSERSITE')}
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    with tempfile.TemporaryDirectory(prefix='medic-office-') as temp:
        output=Path(temp)
        process=subprocess.Popen([sys.executable,'-m','medic.ingestion.office_worker'],cwd=ROOT,env=env,
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        job=None
        try:
            job=WindowsJob(process,limits.memory_bytes,active_processes=8)
            request={'path':str(path),'kind':kind,'source_sha256':source_sha256,'output':str(output),
                     'engine_root':str(engine_root()),'build':identity,'job_name':job.name,'limits':limits.model_dump()}
            try: process.communicate(json.dumps(request).encode(),timeout=limits.seconds)
            except subprocess.TimeoutExpired: raise ParseError('office_conversion_timeout') from None
            result_path=output/'result.json';pdf_path=output/'converted.pdf'
            if process.returncode!=0 or not result_path.is_file(): raise ParseError('office_conversion_failed')
            if result_path.stat().st_size>65536: raise ParseError('office_output_limit_exceeded')
            result=json.loads(result_path.read_text(encoding='utf-8'))
            if result.get('error'): raise ParseError(result['error'])
            if result.get('build')!=identity: raise ParseError('office_build_mismatch')
            if not pdf_path.is_file() or pdf_path.stat().st_size>MAX_PDF_BYTES: raise ParseError('office_output_limit_exceeded')
            data=pdf_path.read_bytes();info=result['info']
            if not data.startswith(b'%PDF-') or digest(data)!=info['pdf_sha256'] or info['source_sha256']!=source_sha256:
                raise ParseError('office_conversion_integrity_failure')
            return info,data
        finally:
            if job: job.close()
            if process.poll() is None: process.kill()
            process.communicate()
