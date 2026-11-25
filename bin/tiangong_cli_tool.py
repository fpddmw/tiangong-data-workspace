"""One parameterized, host-owned public launcher call; no shell or retry."""
import hashlib
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from agent_base import StopRequested, child_environment
from execution_lease import atomic_json
from safe_diagnostics import safe_text


def execute(root, args, *, guard, cancelled, lease=None, writable_roots=(), network_access=False, boundary=None, review_submission=False):
    if not isinstance(args, dict) or set(args) != {'role', 'surface', 'argv'}:
        raise ValueError('Provide exactly role, surface and argv; no environment, cwd or executable override.')
    role, surface, argv = args['role'], args['surface'], args['argv']
    if not isinstance(role, str) or not isinstance(surface, str) or (role, surface) not in (
            ('editor', 'cli'), ('research', 'cli'), ('research', 'ai')):
        raise ValueError('Use editor/cli, research/cli or research/ai with the original role account.')
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\0' in a for a in argv):
        raise ValueError('argv must contain public CLI arguments as strings, without an executable.')
    from delivery_evidence import operation, operation_effect
    from dataset_checks.write_authority import exact_action
    command_name=operation(argv)
    review_write=((role,surface)==('editor','cli') and command_name in ('review submit','review resubmit')
                  and operation_effect(argv)=='possible_write')
    if review_write and not (review_submission and boundary):
        raise ValueError('Use work-submit-batch with the original adoption and native request for managed review submission.')
    root = Path(root).resolve()
    python, launcher = sys.executable, root / 'bin/launch.py'
    if not python or not Path(python).is_file() or not launcher.is_file():
        raise ValueError('The original project Python or public launcher is unavailable.')
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise ValueError('The qualified CLI OS write boundary is unavailable; no unconfined tool was started.')

    def check():
        guard()
        if cancelled():
            raise StopRequested('Original CLI tool cancelled; retain the original operation and its data-effect evidence.',
                                lease.value['run_id'] if lease else 'tiangong_cli')

    check()
    key = 'tiangong-cli-' + uuid.uuid4().hex
    base = lease.path.parent if lease else root / 'work/controlled-cli-output'
    home = base / 'tool-output' / key
    home.mkdir(parents=True, mode=0o700)
    command = [python, str(launcher), '--role', role, surface, '--', *argv]
    environment = child_environment()
    scratch = home / 'tmp'; scratch.mkdir()
    environment.update(TMPDIR=str(scratch), PYTHONDONTWRITEBYTECODE='1')
    from delivery_evidence import context_environment
    environment.update(context_environment(root, lease))
    # Only this fixed launcher reads the selected role's private configuration.
    # Preserve the host's actual write/network grants, including the original
    # run's native operation archive; arbitrary --output cannot enlarge them.
    writes = {home.resolve(), *(Path(p).resolve() for p in writable_roots)}
    if lease: writes.add(lease.path.parent / 'delivery')
    if (role, surface) == ('editor', 'cli') and command_name=='dataset model result-group':
        # Only this installed command creates recovery files here before save.
        journal = Path.home().resolve() / '.local/state/tiangong-lca/model-result-groups'
        if journal.resolve() != journal:
            raise ValueError('Model result-group journal must remain at its original local state path.')
        writes.add(journal)
    if review_write:
        journal = Path.home().resolve() / '.local/state/tiangong-lca/review-operations'
        if journal.resolve() != journal:
            raise ValueError('The review journal must retain its original local state path')
        writes.add(journal)
    policy = '\n'.join(['(version 1)', '(allow default)', '(deny file-write*)',
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-write* '+ ' '.join('(subpath '+json.dumps(str(p),ensure_ascii=False)+')' for p in sorted(writes)) + ')',
        '' if network_access else '(deny network*)'])
    record = {'schema': 'htam.controlled-tiangong-cli.v1', 'role': role, 'surface': surface,
              'argv': argv, 'cwd': str(root), 'started_at_unix': time.time(), 'phase': 'launch_intent',
              'run_id': lease.value['run_id'] if lease else None}
    # The public call's potential effect is visible even when its response
    # is cut short. The native receipt remains the evidence of actual effect.
    if surface == 'cli' and command_name:
        record['invocation_effect'] = operation_effect(argv)
    atomic_json(home / 'record.json', record)
    outputs = {name: home / (name + '.bin') for name in ('stdout', 'stderr')}
    ledger = nullcontext()
    if lease and role=='editor' and surface=='cli' and exact_action(argv) and (root/'.control/harness.sqlite').is_file():
        from harness_tasks import _connect
        ledger=closing(_connect(root,True,timeout=1))
    with ledger as reader, open(outputs['stdout'], 'xb') as out, open(outputs['stderr'], 'xb') as err:
        if reader is not None:
            # Keep the WAL sidecars open for the restricted launcher. It
            # independently reads current decisions just before native start.
            reader.execute('SELECT name FROM sqlite_schema LIMIT 1').fetchone()
        os.chmod(outputs['stdout'], 0o600); os.chmod(outputs['stderr'], 0o600)
        check()
        if lease: lease.tool_intent(key)
        try:
            with boundary() if boundary else nullcontext():
                process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', policy, *command], cwd=root, env=environment, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err, start_new_session=True, close_fds=True)
        except OSError:
            if lease: lease.tool_not_started(key)
            record.update(phase='not_started', finished_at_unix=time.time())
            atomic_json(home / 'record.json', record)
            raise
        if lease: lease.tool_spawned(key, process)
        phase = 'interrupted'
        try:
            while True:
                check()
                try:
                    process.wait(timeout=.2)
                    phase = 'completed'
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # Signal only this retained live handle's process group. Started
            # operation effects remain unknown on interruption; never replay.
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait(timeout=2)
            record.update(phase=phase, exit_code=process.returncode, finished_at_unix=time.time())
            if phase == 'interrupted': record['data_effect'] = 'unknown; inspect original CLI attempt, no automatic replay'
            atomic_json(home / 'record.json', record)

    def ref(path):
        with path.open('rb') as source:
            return {'path': str(path), 'sha256': hashlib.file_digest(source, 'sha256').hexdigest()}

    result = {'exit_code': process.returncode, 'record_ref': ref(home / 'record.json')}
    if 'invocation_effect' in record:
        result['invocation_effect'] = record['invocation_effect']
    for name, path in outputs.items():
        limit = 120000 if name == 'stdout' else 30000
        with path.open('rb') as stream: raw = stream.read(limit)
        result[name] = safe_text(raw.decode('utf-8', errors='replace'))
        result[name + '_ref'] = ref(path)
        result[name + '_bytes'] = path.stat().st_size
        result[name + '_truncated'] = path.stat().st_size > len(raw)
    return result
