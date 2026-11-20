"""Offline computation under an OS sandbox, independent of network permission."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import signal
import uuid


def available():
    return sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()


def execute(root, cwd, argv, *, writable_roots=(), env=None, timeout=60, guard=lambda: None, execution_lease=None):
    if not available():
        raise ValueError('An offline OS sandbox is unavailable; no unconfined computation was started.')
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\0' in x for x in argv):
        raise ValueError('Provide the executable and its arguments as a list.')
    root, cwd = Path(root).resolve(), Path(cwd).resolve()
    environment = dict(env or {})
    binary = shutil.which(argv[0], path=environment.get('PATH'))
    if binary is None:
        raise ValueError('The selected local executable is unavailable.')
    output = Path(tempfile.mkdtemp(prefix='htam-local-check-')).resolve()
    environment.update(HTAM_CHECK_OUTPUT=str(output), TMPDIR=str(output), PYTHONDONTWRITEBYTECODE='1')
    # Keep outputs for the caller's inspection. The original candidate is read-only
    # for reviewers; authors retain only the roots already granted by their host.
    system = ['/System', '/usr', '/bin', '/sbin', '/opt', '/Library/Frameworks', '/dev', '/private/etc']
    reads = {str(root), str(cwd), str(output), str(Path(binary).resolve().parent),
             str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve()), *system}
    writes = {str(output), *(str(Path(p).resolve()) for p in writable_roots)}
    private = {str(root / name) for name in ('.secrets', '.state', '.foundry', '.codex', '.claude', '.env')}
    home = Path.home()
    private.update(str(home / name) for name in ('.ssh', '.aws', '.dsh', '.codex', '.config', 'Library')
                   if not root.is_relative_to(home / name) and not cwd.is_relative_to(home / name))
    # A symlink may give this runtime another spelling of the original workspace.
    for p in root.glob('.env*'):
        private.add(str(p.resolve()))
    def paths(operation, values):
        return '(' + operation + ' ' + ' '.join('(subpath ' + json.dumps(p) + ')' for p in sorted(values)) + ')'
    policy = '\n'.join(['(version 1)', '(deny default)', '(import "system.sb")', '(allow process*)', '(allow sysctl-read)',
                        '(allow signal (target self))', '(deny network*)',
                        '(allow file-read-metadata)', paths('allow file-read*', reads),
                        paths('deny file-read*', private), '(deny file-write*)',
                        paths('allow file-write*', writes)])
    guard()
    key = 'local-check:' + uuid.uuid4().hex
    if execution_lease:
        execution_lease.tool_intent(key)
    try:
        process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', policy, binary, *argv[1:]], cwd=cwd,
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
    except OSError:
        if execution_lease:
            execution_lease.tool_not_started(key)
        raise
    if execution_lease:
        execution_lease.tool_spawned(key, process)
    deadline = time.monotonic() + min(max(timeout, 1), 300)
    try:
        while True:
            guard()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    return {'exit_code': process.returncode, 'stdout': stdout[-30000:], 'stderr': stderr[-10000:],
            'output_directory': str(output), 'network': 'denied', 'writable_roots': sorted(writes)}
