"""Resource-bound preview child; no renderer receives provider credentials."""
import ctypes
import json
import os
from pathlib import Path
import sys

from medic.ingestion.contracts import ParseError,ParseLimits
from medic.runtime import build


def join_job(name):
    from ctypes import wintypes as w
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenJobObjectW.argtypes=[w.DWORD,w.BOOL,w.LPCWSTR];api.OpenJobObjectW.restype=w.HANDLE
    api.GetCurrentProcess.restype=w.HANDLE
    api.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)]
    api.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    api.CloseHandle.argtypes=[w.HANDLE]
    handle=api.OpenJobObjectW(1|4,False,name)
    if not handle: raise ParseError('preview_resource_isolation_unavailable')
    try:
        inside=w.BOOL()
        if not api.IsProcessInJob(api.GetCurrentProcess(),handle,ctypes.byref(inside)):
            raise ParseError('preview_resource_isolation_unavailable')
        if not inside.value and not api.AssignProcessToJobObject(handle,api.GetCurrentProcess()):
            raise ParseError('preview_resource_isolation_unavailable')
    finally:api.CloseHandle(handle)


def main():
    request=json.loads(sys.stdin.buffer.read(16384));output=Path(request['output'])
    try:
        limits=ParseLimits.model_validate(request['limits'])
        if os.name=='nt': join_job(request['job_name'])
        else:
            from medic.ingestion.posix import child_limits
            child_limits(limits)
        identity=build.verify_once(build.VERSION)
        if identity!=request['build']: raise ParseError('preview_build_mismatch')
        from medic.ingestion.preview import render_pdf
        with Path(request['path']).open('rb') as source:data=source.read(limits.input_bytes+1)
        info,png=render_pdf(data,page_number=request['page_number'],width=request['width'],
            source_sha256=request['source_sha256'],highlight=request['highlight'],limits=limits.model_dump())
        (output/'page.png').write_bytes(png)
        result={'build':identity,'info':info}
    except ParseError as error:result={'error':str(error)}
    except Exception:result={'error':'preview_render_failed'}
    (output/'result.json').write_text(json.dumps(result),encoding='utf-8')


if __name__=='__main__':main()
