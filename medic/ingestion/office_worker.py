"""Office conversion supervisor inside the preview Job; no provider credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys

from medic.ingestion.contracts import ParseError,ParseLimits
from medic.ingestion.preview_worker import join_job
from medic.p0.common import digest
from medic.runtime import build


def main():
    request=json.loads(sys.stdin.buffer.read(16384));output=Path(request['output'])
    try:
        join_job(request['job_name'])
        identity=build.verify_once(build.VERSION)
        if identity!=request['build']: raise ParseError('office_build_mismatch')
        from medic.ingestion.office import verify_engine,font_identity,MAX_PDF_BYTES,OFFICE_VERSION
        from medic.ingestion.office_preflight import check_office
        limits=ParseLimits.model_validate(request['limits'])
        with Path(request['path']).open('rb') as source:data=source.read(limits.input_bytes+1)
        if digest(data)!=request['source_sha256']: raise ParseError('office_source_changed')
        preflight=check_office(data,request['kind'],limits.model_dump())
        engine=verify_engine(request['engine_root']);fonts=font_identity()
        input_path=output/('input.'+request['kind']);input_path.write_bytes(data)
        bridge={**request,'path':str(input_path)}
        program=Path(request['engine_root'])/'program'
        env={**os.environ,'PATH':str(program),'PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1'}
        child=subprocess.run([str(program/'python.exe'),str(Path(__file__).with_name('office_bridge.py'))],
            input=json.dumps(bridge).encode(),env=env,cwd=output,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,timeout=limits.seconds)
        pdf_path=output/'converted.pdf'
        if child.returncode or not pdf_path.is_file(): raise ParseError('office_conversion_failed')
        if pdf_path.stat().st_size>MAX_PDF_BYTES: raise ParseError('office_output_limit_exceeded')
        pdf=pdf_path.read_bytes()
        import pymupdf
        with pymupdf.open(stream=pdf,filetype='pdf') as document:
            if not 1<=document.page_count<=limits.pages: raise ParseError('page_limit_exceeded')
            pages=document.page_count
        if fonts!=font_identity(): raise ParseError('office_fonts_changed')
        result={'build':identity,'info':{'preview_version':OFFICE_VERSION,**engine,**fonts,**preflight,
                'source_sha256':digest(data),'pdf_sha256':digest(pdf),'page_count':pages,
                'format':request['kind'],'derived':True,'quality_state_unchanged':True,'ocr_applied':False}}
    except ParseError as error:result={'error':str(error)}
    except Exception:result={'error':'office_conversion_failed'}
    (output/'result.json').write_text(json.dumps(result),encoding='utf-8')


if __name__=='__main__':main()
