"""Windows process-tree ownership for the local queue supervisor.

The real interpreter explicitly joins before receiving the start message, including
when a venv launcher has already spawned it. Closing the supervisor's job kills
the entire assessment tree. This is lifecycle management, not a security sandbox.
"""
import ctypes
import os
import uuid
import signal


class ProcessJob:
    def __init__(self, name=None):
        if os.name != 'nt':
            self.name = 'posix'
            self.process = None
            if name and (name != 'posix' or os.getpgrp() != os.getpid()):
                raise RuntimeError('service_process_group_not_isolated')
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                        ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
        self.api.OpenJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.GetCurrentProcess.restype = w.HANDLE
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.name = name or 'Local\\medic-service-'+uuid.uuid4().hex
        self.handle = (self.api.OpenJobObjectW(1, False, name) if name
                       else self.api.CreateJobObjectW(None, self.name))
        if not self.handle:
            raise RuntimeError('service_job_unavailable')
        if name:
            if not self.api.AssignProcessToJobObject(self.handle, self.api.GetCurrentProcess()):
                self.close(); raise RuntimeError('service_job_join_failed')
        else:
            info = Extended(); info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.close(); raise RuntimeError('service_job_configuration_failed')

    def assign(self, process):
        if os.name != 'nt':
            if os.getpgid(process.pid) != process.pid:
                raise RuntimeError('service_process_group_not_isolated')
            self.process = process
            return
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise RuntimeError('service_job_assignment_failed')

    def close(self):
        if os.name != 'nt':
            if self.process is not None:
                try: os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                self.process = None
            return
        if self.handle:
            self.api.CloseHandle(self.handle); self.handle = None
