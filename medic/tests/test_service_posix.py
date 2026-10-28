"""Actual POSIX queue process ownership, without model or database calls."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from medic.runtime.service_process import ProcessJob


def alive(pid):
    result=subprocess.run(['/bin/ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
    return result.returncode==0 and not result.stdout.strip().startswith('Z')


@unittest.skipIf(os.name=='nt','POSIX process groups')
class PosixServiceOwnership(unittest.TestCase):
    def test_close_kills_interpreter_and_descendant(self):
        code=('import subprocess,sys,time,json,os; '
              'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
              'print(json.dumps([os.getpid(),child.pid]),flush=True); time.sleep(60)')
        process=subprocess.Popen([sys.executable,'-c',code],start_new_session=True,stdout=subprocess.PIPE,text=True)
        job=ProcessJob()
        try:
            job.assign(process)
            pids=json.loads(process.stdout.readline())
            self.assertTrue(all(alive(pid) for pid in pids))
            job.close();process.wait(timeout=5)
            deadline=time.monotonic()+3
            while any(alive(pid) for pid in pids) and time.monotonic()<deadline:time.sleep(.02)
            self.assertFalse(any(alive(pid) for pid in pids))
        finally:
            job.close()
            if process.poll() is None:process.kill();process.wait()
            process.stdout.close()

    def test_supervisor_death_triggers_actual_child_parent_watch(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'pids.json'
            child_code='''import os,sys,time,json,subprocess
from pathlib import Path
from medic.runtime import service_child
from medic.runtime import __main__ as runtime
def fake_run():
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    Path(sys.argv[-1]).write_text(json.dumps([os.getpid(),child.pid]))
    time.sleep(60)
runtime.main=fake_run
sys.argv=['child','posix',sys.argv[1]]
service_child.main()
'''
            parent_code=('import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]],'
                         'stdin=subprocess.PIPE,start_new_session=True);p.stdin.write(b"start\\n");p.stdin.flush();time.sleep(60)')
            parent=subprocess.Popen([sys.executable,'-c',parent_code,child_code,str(output)],start_new_session=True,
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            pids=[]
            try:
                deadline=time.monotonic()+8
                while not output.exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(output.exists(),'child reached the actual service wrapper')
                pids=json.loads(output.read_text())
                self.assertTrue(all(alive(pid) for pid in pids))
                parent.kill();parent.wait(timeout=5)
                deadline=time.monotonic()+5
                while any(alive(pid) for pid in pids) and time.monotonic()<deadline:time.sleep(.02)
                self.assertFalse(any(alive(pid) for pid in pids))
            finally:
                if parent.poll() is None:parent.kill();parent.wait()
                if pids:
                    try:os.killpg(pids[0],signal.SIGKILL)
                    except ProcessLookupError:pass


if __name__=='__main__':unittest.main()
