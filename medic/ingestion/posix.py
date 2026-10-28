"""POSIX parser process groups, CPU limits and sampled group RSS enforcement."""
import os
import signal
import subprocess
import sys
import threading
from subprocess import Popen as resource_process

from medic.ingestion.contracts import ParseError


def child_limits(limits):
    import resource
    # Darwin rejects RLIMIT_AS even for an otherwise valid memory bound.
    # Its parent instead enforces the entire isolated process group's RSS.
    if sys.platform != 'darwin':
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (limits.seconds, limits.seconds + 1))


class ProcessGroupGuard:
    """Own only a new session; kill descendants on limit, timeout and cleanup.

    RSS is sampled every 50ms, not an instantaneous allocation hard limit. Failure
    to inspect usage fails closed. This is resource control, not an OS sandbox.
    """
    def __init__(self, process, memory_bytes):
        if os.getpgid(process.pid) != process.pid:
            raise ParseError('parser_process_group_not_isolated')
        self.process, self.memory_bytes = process, memory_bytes
        self.name = None
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def usage(self):
        with resource_process(['/bin/ps', '-axo', 'pgid=,rss='],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as process:
            try:
                output, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
                raise
            if process.returncode:
                raise OSError('resource_inspection_failed')
        return sum(int(rss) * 1024 for group, rss in
                   (line.split() for line in output.splitlines() if line.strip())
                   if int(group) == self.process.pid)

    def watch(self):
        while not self.stop.is_set() and self.process.poll() is None:
            try:
                if self.usage() > self.memory_bytes:
                    self.error = 'parser_memory_limit_exceeded'
            except (OSError, ValueError, subprocess.SubprocessError):
                self.error = 'parser_resource_inspection_failed'
            if self.error:
                self.kill()
                return
            self.stop.wait(.05)

    def kill(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def check(self):
        if self.error:
            raise ParseError(self.error)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.kill()
