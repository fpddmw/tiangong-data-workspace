#!/usr/bin/env python3
"""Isolated OS workload for capacity tests; never calls a model or real host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agent_base import StopRequested
import dsh_capacity
import execution_lease
import harness_tasks as tasks


def child(root, run_id):
    (root / (run_id + '.native-ready')).write_text(str(os.getpid()))
    while not (root / (run_id + '.release')).exists():
        time.sleep(.03)


def main():
    mode, directory, run_id, *rest = sys.argv[1:]
    root = Path(directory).resolve()
    if mode == 'child':
        child(root, run_id)
        return
    if mode == 'child-with-tool':
        subprocess.Popen([sys.executable, __file__, 'child', str(root), run_id + '.tool'],
            env=os.environ.copy(), start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while not (root / (run_id + '.tool.native-ready')).exists():
            time.sleep(.02)
        (root / (run_id + '.native-ready')).write_text(str(os.getpid()))
        return
    kind, request_id = rest[:2]
    request = tasks.read_request(root, request_id)
    home = root / 'work' / run_id
    with execution_lease.ExecutionLease(root, request, home, run_id) as lease:
        if mode=='claim-wait':
            import dsh_capacity_handoff
            dsh_capacity_handoff.claim(root,tasks.read_run(root,run_id),lease,rest[2])
            lease.intent();(root/(run_id+'.claim-ready')).touch()
            while True:time.sleep(.03)
        lease.intent()
        cap = dsh_capacity.Capacity(root, lease, kind, tasks.verified_actor(root),provider='configured-dsh')
        def guard():
            if tasks.read_run(root, run_id)['stop_requested']:
                raise StopRequested('Original queued run was stopped', run_id)
        try:
            with cap.admit(guard):
                process = subprocess.Popen([sys.executable, __file__, 'child-with-tool' if mode == 'detached-tool' else 'child', str(root), run_id],
                    env={**os.environ, **cap.environment}, start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                cap.launched = True
                if mode != 'crash-window':
                    lease.spawned(process)
                (root / (run_id + '.manager-ready')).write_text(str(process.pid))
                if mode=='controlled-wait':
                    native={'host':'deepseek_harness','kind':'session','id':'session-'+run_id,
                        'instance':'session-'+run_id,'cwd':str(root),'turn_id':'1'}
                    lease.bind(native)
                    with tasks.transaction(root) as c:
                        c.execute('UPDATE work_runs SET native_identity_json=? WHERE id=?',(tasks._json(native),run_id))
                    while not (root/(run_id+'.begin-wait')).exists():guard();time.sleep(.03)
                    with cap.review_wait(guard,'own-independent-review'):
                        (root/(run_id+'.waiting')).touch()
                        while not (root/(run_id+'.review-done')).exists():guard();time.sleep(.03)
                    (root/(run_id+'.reacquired')).touch()
                process.wait(timeout=90)
            lease.settled()
        except StopRequested:
            (root / (run_id + '.cancelled')).write_text('No native process started')


if __name__ == '__main__':
    main()
