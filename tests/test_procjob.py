import os
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name != "nt", reason="job object é do Windows")
def test_children_die_with_parent():
    code = ("import subprocess,sys,time;from clipador.procjob import bind_children_to_this_process as b;"
            "assert b();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(p.pid,flush=True);time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, cwd=os.getcwd())
    child_pid = int(parent.stdout.readline())
    parent.kill()
    parent.wait()
    time.sleep(1.5)
    alive = str(child_pid) in subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}"], capture_output=True, text=True).stdout
    assert not alive, "filho continuou vivo depois do pai morrer"
