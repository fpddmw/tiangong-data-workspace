#!/usr/bin/env python3
"""Watch the original DSH dispatches and reconcile them on exit or a due retry."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import plistlib
import select
import subprocess
import sys
import time

import dsh_auto_resume as recovery
import dsh_capacity as capacity
import execution_lease as execution
import harness_tasks as tasks
from harness_package import launch_spec
from codex_host import safe_diagnostic


def _signal(root):
    return root / '.control/dsh-recovery-wakeup'


def wake(root):
    try:
        _signal(root).touch(mode=0o600)
    except OSError:
        # The run event is already durable. Process exit or host restart can
        # recover it even if the immediate notification could not be sent.
        pass


def _event(root, actor, run_id, kind, value):
    with tasks.transaction(root) as c:
        tasks._run_event(c, tasks._row(c, 'work_runs', run_id), kind, actor, None, value)
    wake(root)


def reattach(root, actor, run_id, watch):
    _event(root, actor, run_id, recovery.WATCH, watch)


def record_result(root, actor, run, value):
    if not any(e['kind'] == recovery.WATCH for e in run.get('events', ())):
        return
    if run['outcome'] in ('succeeded', 'failed', 'stopped', 'unknown'):
        recovery.finish_observed_probe(root, actor, run['id'])
    if value and value.get('state') in ('cooldown', 'already_processing', 'waiting_original_exit'):
        wake(root)
    else:
        _event(root, actor, run['id'], recovery.HANDLED, value or {'state':'nothing_to_resume'})


def _watched(root):
    with closing(tasks._connect(root, True)) as c:
        rows = c.execute('''SELECT e.* FROM work_run_events e JOIN (
            SELECT max(id) AS id FROM work_run_events WHERE kind IN (?,?) GROUP BY run_id
            ) AS current ON current.id=e.id WHERE e.kind=? ORDER BY e.id''',
                         (recovery.WATCH, recovery.HANDLED, recovery.WATCH)).fetchall()
    return [(row['run_id'], json.loads(row['payload_json'])) for row in rows]


def _metadata(root, run, watch):
    launch = launch_spec(root, run)
    home = launch.get('evidence_home')
    if not home:
        return None, {}
    path = execution.metadata_path(home, run['id'])
    metadata = capacity._read(path, {})
    if (metadata.get('run_id') != run['id'] or metadata.get('request_id') != run['request_id']
            or metadata.get('manager') != watch.get('manager')):
        return path, {}
    return path, metadata


def _retire_model(root, actor, run, watch):
    """After the manager dies, the gated adapter can only drain or exit."""
    if execution.matches(watch['manager']) is not False:
        return
    path, metadata = _metadata(root, run, watch)
    child = metadata.get('child')
    if not child or metadata.get('model_closed') or execution.original_group(child)['state'] != 'exited':
        return
    # A dead DSH leader cannot issue managed model requests. Preserve the
    # independent tool group and native writer checks for Session recovery.
    with capacity._lock(root):
        current_path, current = _metadata(root, run, watch)
        if current_path == path and current == metadata:
            capacity._release_model(root, run['id'], actor, path, metadata)


def inspect_run(root, run_id, watch):
    """Mechanically reconcile one registered run, using its original binding."""
    import agent_runtime
    import run_worker
    from dsh_capacity_handoff import recover_abandoned

    worker = agent_runtime.Runtime(root)
    original = tasks.read_run(root, run_id)
    result = None
    try:
        if not capacity.recovery_managed(root, watch['provider']) or original['stop_requested']:
            return
        _retire_model(root, worker.actor, original, watch)
        if original['outcome'] in ('succeeded', 'stopped'):
            return
        request = tasks.read_request(root, original['request_id'])
        if original['outcome'] == 'intent' and not original['native_identity']:
            result = recover_abandoned(root, worker, original, automatic=True)
            if result is not None:
                # Handoff registered a new execution owner for this same run.
                return
            result = {'state':'launch_ownership_unverified'}
            return
        state = execution.observe_run(root, original)['state']
        if state != 'exited':
            _, metadata = _metadata(root, original, watch)
            tracked = [item.get('process') for item in (metadata.get('tools') or {}).values()]
            result = {'state':'waiting_original_exit' if any(p and execution.matches(p) is True for p in tracked)
                      else 'execution_ownership_unverified'}
            return
        role = 'scientific_reviewer' if request['independent'] else request['recipient_role']
        config = agent_runtime.load_config(root, original['config_ref']['path'], role)
        if config['config_ref'] != original['config_ref'] or config['provider'].get('dsh_provider') != watch['provider']:
            result = {'state':'original_configuration_changed'}
            return
        if original['outcome'] in ('running', 'unknown') and original['native_identity']:
            run_worker._program_read(root, worker, request, original, config_path=config['config_ref']['path'])
            original = tasks.read_run(root, run_id)
        if original['outcome'] in ('failed', 'unknown'):
            parent = tasks.read_request(root, request['parent_request_id']) if request['independent'] else request
            result = run_worker._automatic_continuation(root, worker, parent, request, config, run_id,
                recover_claim=True)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, tasks.WorkConflict) as exc:
        result = {'state':'recovery_error', 'reason':safe_diagnostic(exc)}
    finally:
        current = tasks.read_run(root, run_id)
        latest = next((e for e in reversed(current['events']) if e['kind'] == recovery.WATCH), None)
        if latest and latest['payload'] == watch:
            record_result(root, worker.actor, current, result)


def _watch_exit(kernel, record):
    if not record or execution.matches(record) is not True:
        return False
    try:
        kernel.control([select.kevent(record['pid'], filter=select.KQ_FILTER_PROC,
                                     flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                     fflags=select.KQ_NOTE_EXIT)], 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        if execution.matches(record) is False:
            return False
        raise


def _deadline(root, run, provider):
    pending = recovery.pending_view(run)
    if not pending or pending['state'] != 'cooldown':
        return 0
    with closing(tasks._connect(root, True)) as c:
        probe = recovery._active_probe(c, provider)
        if probe and probe['run_id'] != run['id']:
            return None  # Completion of that exact probe wakes the host.
        return recovery.effective_fuse(c,provider).get('until',0)


def serve(root):
    """Idle in the kernel until a run event, original process exit or deadline."""
    if not hasattr(select, 'kqueue'):
        raise ValueError('This host requires a kernel process-exit notification service.')
    wake(root)
    with execution.FileLock(root, ['dsh-recovery-supervisor']), closing(select.kqueue()) as kernel:
        fd = os.open(_signal(root), os.O_RDONLY)
        try:
            kernel.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE, flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                         fflags=select.KQ_NOTE_ATTRIB | select.KQ_NOTE_WRITE)], 0)
            with ThreadPoolExecutor(max_workers=capacity.LIMITS['total']) as jobs:
                running = {}
                while True:
                    for run_id, job in list(running.items()):
                        if job.done():
                            job.result()
                            del running[run_id]
                    accounted = [capacity.flush_accounting(root, host) for host in capacity.POOL_NAMES]
                    deadlines = [] if all(accounted) else [time.time()+1]
                    for run_id, watch in _watched(root):
                        if run_id in running:
                            continue
                        run = tasks.read_run(root, run_id)
                        due = _deadline(root, run, watch['provider'])
                        if due is None:
                            continue
                        if due > time.time():
                            deadlines.append(due)
                            continue
                        _, metadata = _metadata(root, run, watch)
                        pending = recovery.pending_view(run) or {}
                        if (pending.get('state') != 'cooldown' and _watch_exit(kernel, watch['manager'])
                                or _watch_exit(kernel, metadata.get('child'))):
                            continue
                        if metadata.get('model_closed') and any(_watch_exit(kernel, item.get('process'))
                                for item in (metadata.get('tools') or {}).values()):
                            continue
                        job = jobs.submit(inspect_run, root, run_id, watch)
                        job.add_done_callback(lambda _: wake(root))
                        running[run_id] = job
                    wait = max(0, min(deadlines)-time.time()) if deadlines else None
                    kernel.control([], 16, wait)
        finally:
            os.close(fd)


def activate(root):
    """Adopt the installed DSH provider without changing either capacity pool."""
    from agent_runtime import load_config
    from harness_hosts import installed_program

    installed = installed_program(root)
    config = load_config(root, installed['path'], 'executor') if installed else {}
    if (config.get('host', {}).get('adapter') != 'dsh-headless' or not config['host'].get('enabled')
            or not hasattr(select, 'kqueue')):
        raise ValueError('Select the qualified installed DSH runtime on the original Mac host.')
    provider = config['provider']['dsh_provider']
    label = 'earth.tiangong.dsh-recovery.' + hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    value = {'Label':label, 'ProgramArguments':[sys.executable, str(Path(__file__).resolve()),
             '--root', str(root), '--serve'], 'WorkingDirectory':str(root), 'KeepAlive':True,
             'StandardErrorPath':str(root / '.control/dsh-recovery.stderr')}
    if plist.exists():
        if plistlib.loads(plist.read_bytes()) != value:
            raise ValueError('The original DSH recovery service path is already in use.')
    else:
        with plist.open('xb') as out:
            plistlib.dump(value, out)
    domain = f'gui/{os.getuid()}'
    loaded = subprocess.run(['launchctl','print',f'{domain}/{label}'], capture_output=True)
    if loaded.returncode:
        subprocess.run(['launchctl','bootstrap',domain,str(plist)], check=True)
    deadline = time.monotonic() + 10
    while True:
        state = subprocess.run(['launchctl','print',f'{domain}/{label}'], capture_output=True, text=True)
        if state.returncode == 0 and '\n\tstate = running\n' in state.stdout:
            break
        if time.monotonic() >= deadline:
            raise ValueError('The original Mac recovery service did not start. Inspect its local stderr.')
        time.sleep(.2)
    execution.atomic_json(root / '.control/dsh-recovery.json', {'provider':provider, 'service':label})
    wake(root)
    return {'provider':provider, 'service':label, 'limits':capacity.LIMITS}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--serve', action='store_true')
    action.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    workspace = args.root.resolve(strict=True)
    if args.serve:
        serve(workspace)
    else:
        print(json.dumps(activate(workspace)))
