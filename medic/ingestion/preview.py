"""Static original PDF rendering. It never changes evidence or extraction quality."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from medic.ingestion.bounded import WindowsJob
from medic.ingestion.contracts import ParseError, ParseLimits
from medic.p0.common import ROOT, digest
from medic.runtime import build

PREVIEW_VERSION = 'medic-pdf-preview-v1'
MAX_PIXELS = 4_000_000
MAX_OUTPUT = 16*1024*1024


def render_pdf(data, *, page_number, width, source_sha256, highlight=None, limits=None):
    """Worker-only core. Rectangles and text are checked against the original PDF."""
    import pymupdf
    limits=ParseLimits.model_validate(limits or {})
    if len(data)>limits.input_bytes: raise ParseError('input_size_exceeded')
    if digest(data)!=source_sha256: raise ParseError('preview_source_changed')
    if type(page_number) is not int or type(width) is not int or not 320<=width<=1600:
        raise ParseError('invalid_preview_request')
    try:
        with pymupdf.open(stream=data,filetype='pdf') as document:
            if document.needs_pass: raise ParseError('encrypted_pdf')
            if document.page_count>limits.pages: raise ParseError('page_limit_exceeded')
            text_match=None
            if highlight and highlight.get('mode')=='unique_text':
                from medic.ingestion.office_mapping import locate_unique_text
                text_match=locate_unique_text(document,highlight,limits)
                if text_match:page_number=text_match[0]
            if not 1<=page_number<=document.page_count: raise ParseError('preview_page_not_found')
            page=document[page_number-1];rect=page.rect
            if not all(math.isfinite(v) and 0<v<=20000 for v in (rect.width,rect.height)):
                raise ParseError('preview_page_size_exceeded')
            scale=min(width/rect.width,math.sqrt(MAX_PIXELS/(rect.width*rect.height)),8192/rect.height)
            # Reserve rounding space before asking MuPDF to allocate a pixmap.
            while math.ceil(rect.width*scale)*math.ceil(rect.height*scale)>MAX_PIXELS: scale*=.999
            highlight_state='none'; box=None
            if highlight and highlight.get('mode')=='unique_text':
                highlight_state='unavailable'
                if text_match:
                    visible=(pymupdf.Rect(text_match[1])*page.rotation_matrix)&rect
                    if not visible.is_empty and not visible.is_infinite:
                        box=[visible.x0*scale,visible.y0*scale,visible.x1*scale,visible.y1*scale];highlight_state='unique_text'
            elif highlight and highlight.get('mode')=='ocr_page':
                highlight_state='ocr_page_only'
            elif highlight:
                highlight_state='unavailable'
                blocks=[b for b in page.get_text('dict',flags=pymupdf.TEXTFLAGS_TEXT)['blocks'] if b['type']==0]
                number=highlight.get('block')
                if type(number) is int and 1<=number<=len(blocks):
                    block=blocks[number-1]
                    quote='\n'.join(''.join(span['text'] for span in line['spans']) for line in block['lines'])
                    expected=highlight.get('bbox',[])
                    if (len(expected)==4 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in expected)
                            and all(abs(a-b)<.01 for a,b in zip(block['bbox'],expected))
                            and digest(quote)==highlight.get('quote_hash')):
                        # PDF extraction uses unrotated coordinates, as documented by MuPDF.
                        # https://pymupdf.readthedocs.io/en/latest/page.html#Page.rotation_matrix
                        unrotated=pymupdf.Rect(block['bbox'])
                        visible=(unrotated*page.rotation_matrix)&rect
                        if not visible.is_empty and not visible.is_infinite:
                            box=[visible.x0*scale,visible.y0*scale,visible.x1*scale,visible.y1*scale]
                            highlight_state='exact_block'
            pix=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),colorspace=pymupdf.csRGB,alpha=False,annots=True)
            if pix.width*pix.height>MAX_PIXELS: raise ParseError('preview_pixel_limit_exceeded')
            if box:
                # Draw only on the rendered pixels. Inserting PDF graphics can shift
                # overlays on cropped, rotated pages; the original PDF stays untouched.
                x0,y0=max(0,math.floor(box[0])),max(0,math.floor(box[1]))
                x1,y1=min(pix.width,math.ceil(box[2])),min(pix.height,math.ceil(box[3]))
                stroke=max(2,round(scale*1.5))
                for border in ((x0,y0,x1,min(y0+stroke,y1)),(x0,max(y0,y1-stroke),x1,y1),
                               (x0,y0,min(x0+stroke,x1),y1),(max(x0,x1-stroke),y0,x1,y1)):
                    pix.set_rect(pymupdf.IRect(border),(217,31,31))
            png=pix.tobytes('png')
            if len(png)>MAX_OUTPUT: raise ParseError('preview_output_limit_exceeded')
            info={'preview_version':PREVIEW_VERSION,'source_sha256':source_sha256,'page':page_number,
                  'page_count':document.page_count,'width':pix.width,'height':pix.height,'rotation':page.rotation,
                  'highlight':highlight_state,'highlight_pixels':box,'image_sha256':digest(png),
                  'ocr_applied':False,'quality_state_unchanged':True}
            return info,png
    except ParseError: raise
    except Exception: raise ParseError('preview_render_failed') from None


def render_file(path, *, page_number, width, source_sha256, highlight=None, limits=None):
    limits=ParseLimits.model_validate(limits or {})
    path=Path(path).resolve()
    if path.stat().st_size>limits.input_bytes: raise ParseError('input_size_exceeded')
    identity=build.verify(build.VERSION)
    environment={k:v for k,v in os.environ.items() if k.upper() in
                 ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','PYTHONNOUSERSITE')}
    environment.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    with tempfile.TemporaryDirectory(prefix='medic-preview-') as temp:
        output=Path(temp)
        process=subprocess.Popen([sys.executable,'-m','medic.ingestion.preview_worker'],cwd=ROOT,env=environment,
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job=None
        try:
            if os.name=='nt': job=WindowsJob(process,limits.memory_bytes)
            request={'path':str(path),'output':str(output),'build':identity,'limits':limits.model_dump(),
                     'job_name':job.name if job else None,'page_number':page_number,'width':width,
                     'source_sha256':source_sha256,'highlight':highlight}
            try: process.communicate(json.dumps(request).encode(),timeout=limits.seconds)
            except subprocess.TimeoutExpired: raise ParseError('preview_timeout') from None
            result_path=output/'result.json';image_path=output/'page.png'
            if process.returncode!=0 or not result_path.is_file(): raise ParseError('preview_process_failed')
            if result_path.stat().st_size>16384: raise ParseError('preview_output_limit_exceeded')
            result=json.loads(result_path.read_text(encoding='utf-8'))
            if result.get('error'): raise ParseError(result['error'])
            if result.get('build')!=identity: raise ParseError('preview_build_mismatch')
            if not image_path.is_file() or image_path.stat().st_size>MAX_OUTPUT: raise ParseError('preview_output_limit_exceeded')
            png=image_path.read_bytes();info=result['info']
            if not png.startswith(b'\x89PNG\r\n\x1a\n') or digest(png)!=info['image_sha256'] or info['source_sha256']!=source_sha256:
                raise ParseError('preview_integrity_failure')
            return info,png
        finally:
            # Close the job first: a venv launcher may have exited before its interpreter.
            if job: job.close()
            if process.poll() is None: process.kill()
            process.communicate()
