"""Actual POSIX child/resource lifecycle; no documents or model requests."""
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from medic.ingestion.posix import ProcessGroupGuard
from medic.ingestion.contracts import ParseError


@unittest.skipIf(os.name == 'nt', 'POSIX process groups')
class PosixLimits(unittest.TestCase):
    def start(self, code):
        process = subprocess.Popen([sys.executable, '-c', code], start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def test_real_memory_overrun_terminates_group(self):
        process = self.start('import time; time.sleep(.2); data=bytearray(128*1024*1024); time.sleep(30)')
        guard = ProcessGroupGuard(process, 48*1024*1024)
        try:
            process.wait(timeout=8)
            with self.assertRaisesRegex(ParseError, 'memory_limit'):
                guard.check()
            self.assertLess(process.returncode, 0)
        finally:
            guard.close()

    def test_inspection_failure_fails_closed(self):
        process = self.start('import time; time.sleep(30)')
        with patch.object(ProcessGroupGuard, 'usage', side_effect=OSError('synthetic failure')):
            guard = ProcessGroupGuard(process, 128*1024*1024)
            try:
                process.wait(timeout=5)
                with self.assertRaisesRegex(ParseError, 'inspection_failed'):
                    guard.check()
            finally:
                guard.close()

    def test_unisolated_process_is_refused(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            with self.assertRaisesRegex(ParseError, 'not_isolated'):
                ProcessGroupGuard(process, 128*1024*1024)
        finally:
            process.kill(); process.wait()
