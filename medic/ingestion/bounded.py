"""One parsing process per upload, bounded by wall time and process memory."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from medic.runtime import build
from medic.ingestion.contracts import Extraction, ParseError, ParseLimits
from medic.p0.common import ROOT, digest


class WindowsJob:
    def __init__(self, process, memory_bytes, *, active_processes=2):
        if type(active_processes) is not int or not 2 <= active_processes <= 8:
            raise ValueError('invalid_process_limit')
        from ctypes import wintypes as w
        size = ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_ = [('per_process_time', ctypes.c_int64), ('per_job_time', ctypes.c_int64),
                        ('flags', w.DWORD), ('min_ws', size), ('max_ws', size), ('active_processes', w.DWORD),
                        ('affinity', size), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', size), ('job_memory', size),
                        ('peak_process', size), ('peak_job', size)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.name = 'Local\\medic-parser-'+uuid.uuid4().hex
        self.handle = self.api.CreateJobObjectW(None, self.name)
        info = Extended()
        # Windows venv python.exe redirects to a second interpreter process.
        # Bound both the whole job and each process; allow only that small tree.
        info.basic.flags = 0x100 | 0x200 | 0x2000 | 0x8
        info.basic.active_processes = active_processes
        info.process_memory = memory_bytes
        info.job_memory = memory_bytes
        if (not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                or not self.api.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle)))):
            self.close()
            raise ParseError('parser_resource_isolation_unavailable')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def parse_file(path, *, kind=None, limits=None):
    limits = ParseLimits.model_validate(limits or {})
    path = Path(path).resolve()
    kind = kind or path.suffix.lower().lstrip('.')
    if kind not in ('pdf','docx','pptx','xlsx'):
        raise ParseError('unsupported_format')
    if path.stat().st_size > limits.input_bytes:
        raise ParseError('input_size_exceeded')
    identity = build.verify(build.VERSION)
    environment = {k:v for k,v in os.environ.items() if k.upper() in
                   ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','PYTHONNOUSERSITE','MEDIC_OCR_DATA_ROOT')}
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    with tempfile.TemporaryDirectory(prefix='medic-parse-') as temp:
        output = Path(temp)/'extraction.json'
        process = subprocess.Popen([sys.executable, '-m', 'medic.ingestion.worker'], cwd=ROOT, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job = None
        try:
            if os.name == 'nt':
                job = WindowsJob(process, limits.memory_bytes)
            request = {'path': str(path), 'kind': kind, 'limits': limits.model_dump(), 'output': str(output), 'build': identity,
                       'job_name': job.name if job else None}
            try:
                process.communicate(json.dumps(request).encode('utf-8'), timeout=limits.seconds)
            except subprocess.TimeoutExpired:
                raise ParseError('parser_timeout') from None
            if process.returncode != 0 or not output.exists():
                raise ParseError('parser_process_failed')
            if output.stat().st_size > 32*1024*1024:
                raise ParseError('parser_output_limit_exceeded')
            result = json.loads(output.read_text(encoding='utf-8'))
            if result.get('error'):
                raise ParseError(result['error'])
            extracted = Extraction.model_validate(result['extraction'])
            if result.get('build') != identity:
                raise ParseError('parser_build_mismatch')
            return extracted
        finally:
            if job:
                job.close()
            if process.poll() is None:
                process.kill()
            process.communicate()
