"""One local execution owner per original request chain and native Session.

Locks exclude managers; durable process receipts also protect detached execution.
Missing receipts and inaccessible process identities are unknown, never quiescence.
"""
import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid


class ExecutionBusy(ValueError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('x') as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class FileLock:
    def __init__(self, root, key):
        self.path = Path(root) / '.control/execution-locks' / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + '.lock')
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        os.set_inheritable(self.fd, False)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd); self.fd = None
            raise ExecutionBusy('Original execution manager owns this request/Session; observe its run.') from exc
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd); self.fd = None


def boot_identity():
    try:
        p = Path('/proc/sys/kernel/random/boot_id')
        if p.is_file():
            return p.read_text().strip()
        r = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, timeout=1)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def process_identity(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {'pid': pid, 'exists': False}
    except PermissionError:
        # Another user's reused PID may still have a readable OS start identity.
        pass
    try:
        # ps reads the OS start identity, not a file mtime or a bare PID.
        r = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=,pgid=,stat='], capture_output=True, text=True, timeout=1)
        parts = r.stdout.split()
        if r.returncode or len(parts) < 7:
            return {'pid': pid, 'exists': None, 'reason': 'process start identity unavailable'}
        return {'pid': pid, 'exists': not parts[6].startswith('Z'), 'boot': boot_identity(),
                'start': ' '.join(parts[:5]), 'pgid': int(parts[5])}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'pid': pid, 'exists': None, 'reason': 'process observation unavailable'}


def matches(record):
    if not record or not record.get('pid'):
        return None
    current = process_identity(record['pid'])
    if current['exists'] is not True:
        return current['exists']
    if not record.get('start') or not record.get('boot') or not current.get('boot'):
        return None
    return (current['boot'], current['start']) == (record['boot'], record['start'])


def metadata_path(home, run_id):
    return Path(home) / 'runs' / run_id / 'execution.json'


def original_group(record):
    """Read the lifetime of the recorded group without signalling a replacement."""
    if not record or not isinstance(record.get('pid'), int) or record['pid'] <= 0:
        return {'state': 'unknown', 'reason': 'Missing original process identity'}
    pid, group = record['pid'], record.get('pgid')
    current = process_identity(pid)
    if current['exists'] is None:
        return {'state': 'unknown', 'reason': 'Original process identity is unreadable'}
    if current['exists'] is True:
        if not record.get('start') or not record.get('boot') or current.get('boot') != record['boot']:
            return {'state': 'unknown', 'reason': 'Original process start or boot is unverified'}
        if current.get('start') == record['start']:
            return {'state': 'active', 'reason': 'Original process is alive'}
        # A new PID cannot be allocated while a group with that number exists.
        # Only use this proof for a recorded group leader on the same boot.
        if group == pid and current.get('start'):
            try:
                fmt = '%a %b %d %H:%M:%S %Y'
                if time.strptime(current['start'], fmt) > time.strptime(record['start'], fmt):
                    return {'state': 'exited', 'reason': 'A later process owns the original leader PID'}
            except ValueError:
                pass
        return {'state': 'unknown', 'reason': 'Group lifetime cannot be attributed to the original leader'}
    if not isinstance(group, int) or group <= 0:
        return {'state': 'unknown', 'reason': 'Missing original process group'}
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return {'state': 'exited', 'reason': 'Original process group has exited'}
    except PermissionError:
        return {'state': 'unknown', 'reason': 'EPERM observing original process group'}
    return {'state': 'active', 'reason': 'Original process group still exists; descendants may be active'}


def tools_observation(tools):
    """Observe every client-owned tool group; a dead manager is insufficient."""
    for key, item in tools.items():
        if item.get('phase') == 'launch_intent':
            return {'state': 'unknown', 'reason': 'Tool launch intent has no confirmed process receipt', 'tool': key}
        if item.get('phase') == 'not_started':
            continue
        child = item.get('process')
        group = original_group(child)
        if group['state'] != 'exited':
            return {**group, 'reason': 'Tracked tool group: ' + group['reason'],
                    'tool': key, 'process': child}
    return {'state': 'exited', 'source': 'Tracked tool OS start identities and process groups'}


def execution_observation(path):
    path = Path(path)
    try:
        doc = json.loads(path.read_bytes())
        if not isinstance(doc, dict):
            raise ValueError('Execution metadata must be an object')
    except (OSError, ValueError):
        return {'state': 'unknown', 'reason': 'Original execution metadata missing or unreadable',
                'metadata_readable': False, 'path': str(path)}
    if doc.get('phase') == 'settled':
        # Only the execution manager writes settled, after closing its transport.
        return {'state': 'exited', 'source': str(path)}
    manager = matches(doc.get('manager')) if doc.get('phase')!='manager_released' else False
    if manager is True:
        return {'state': 'active', 'reason': 'Original execution manager alive', 'source': str(path)}
    tools = tools_observation(doc.get('tools', {}))
    if tools['state'] != 'exited':
        return {**tools, 'source': str(path)}
    child = doc.get('child')
    if child:
        group = original_group(child)
        if group['state'] != 'exited' or manager is False:
            return {**group, 'source': str(path)}
    return {'state': 'unknown', 'reason': 'Launch intent without confirmed process exit; reconcile original OS/native evidence', 'source': str(path)}


def observe_run(root, run):
    from harness_package import launch_spec
    spec = launch_spec(root, run)
    if not spec.get('evidence_home'):
        return {'state': 'unknown', 'reason': 'Original launch home unavailable'}
    path = metadata_path(spec['evidence_home'], run['id'])
    observed = execution_observation(path)
    if observed.get('metadata_readable') is False:
        return legacy_observation(root, run, spec, path)
    return observed


@contextlib.contextmanager
def native_read_lock(path):
    """Share observations; exclude the installed native exclusive writer."""
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC)
    try:
        fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        held,current=os.fstat(fd),path.stat()
        if (held.st_dev,held.st_ino)!=(current.st_dev,current.st_ino):
            raise ValueError('Native lock identity changed during observation')
        yield {'path':str(path),'device':held.st_dev,'inode':held.st_ino,'writer':False}
    finally:
        os.close(fd)


def native_writer(path):
    try:
        with native_read_lock(path):return False
    except FileNotFoundError:return False
    except BlockingIOError:return True
    except (OSError,ValueError):return None


def _process_markers(pid):
    """Return only attribution fields. Never log or retain process environments."""
    if sys.platform == 'darwin':
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
        size = ctypes.c_size_t(1024 * 1024)
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        raw = buffer.raw[:size.value]
        argc = ctypes.c_int.from_buffer_copy(raw[:4]).value
        parts = raw[4:].split(b'\0')[1:]  # executable path, then padding
        while parts and not parts[0]:
            parts.pop(0)
        argv, environment = parts[:argc], parts[argc:]
    elif sys.platform.startswith('linux'):
        argv = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
        environment = (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0')
    else:
        raise OSError(errno.ENOTSUP, 'OS process attribution unavailable')
    markers = {}
    for item in environment:
        key, separator, value = item.partition(b'=')
        if separator and key in (b'DSH_SESSION_ID', b'DSH_HOME',
                                  b'HTAM_DSH_CAPACITY_ROOT', b'HTAM_DSH_CAPACITY_RUN_ID'):
            markers[key.decode()] = os.fsdecode(value)
    # Only a native execution flag counts; a query or a prompt mentioning an ID
    # is not an execution binding. Full argv is discarded inside this function.
    markers['session_arguments'] = [os.fsdecode(argv[i + 1]) for i, item in enumerate(argv[:-1])
                                    if item == b'--session-id']
    return markers


def _process_snapshot():
    call = subprocess.run(['ps', '-axo', 'uid=,pid=,ppid=,pgid=,stat=,lstart='],
                          capture_output=True, text=True, timeout=5, check=True)
    result = {}
    for line in call.stdout.splitlines():
        parts = line.split()
        if len(parts) != 10:
            raise ValueError('Incomplete OS process identity row')
        uid, pid, parent, group = map(int, parts[:4])
        if uid == os.getuid() and not parts[4].startswith('Z'):
            result[pid] = {'pid': pid, 'ppid': parent, 'pgid': group, 'start': ' '.join(parts[5:])}
    # The observer and its own ps helper are not the original execution.
    excluded = {os.getpid()}
    while extra := {pid for pid, row in result.items() if row['ppid'] in excluded} - excluded:
        excluded.update(extra)
    return {pid: row for pid, row in result.items() if pid not in excluded}


def session_process_observation(native, home):
    """Check native argv and inherited shell identity, including orphan tools.

    This is an OS observation, not a PID-based authorization or a signal. A
    process whose attribution cannot be read keeps the result unknown. Repeat
    snapshots cover process births/PID reuse during inspection.
    """
    checked, issues, count = {}, {}, 0
    try:
        snapshot = _process_snapshot()
        for _ in range(3):
            for pid, row in snapshot.items():
                if checked.get(pid) == row and pid not in issues:
                    continue
                try:
                    markers = _process_markers(pid)
                    count += 1
                except (OSError, ValueError) as exc:
                    issues[pid] = {'pid': pid, 'error': getattr(exc, 'errno', None) or type(exc).__name__}
                    continue
                checked[pid] = row
                issues.pop(pid, None)
                tagged = markers.get('DSH_SESSION_ID') == native['id']
                if tagged or native['id'] in markers['session_arguments']:
                    return {'state': 'active', 'reason': 'Original Session process or tool descendant is alive',
                            'process': row, 'binding': 'native_session_argument' if not tagged else 'native_shell_session'}
            current = _process_snapshot()
            unresolved = [pid for pid, row in current.items() if checked.get(pid) != row or pid in issues]
            if not unresolved:
                return {'state': 'clear', 'source': 'OS same-user process identities and native Session markers',
                        'inspected': count, 'boot': boot_identity()}
            snapshot = current
            # Give newly observed/exiting helper processes time to settle. The
            # next pass must still verify every surviving exact OS identity.
            time.sleep(.02)
        return {'state': 'unknown', 'reason': 'OS attribution incomplete; inspect the listed original-owner processes',
                'unverified_pids': unresolved, 'issues': [issues[pid] for pid in unresolved if pid in issues]}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {'state': 'unknown', 'reason': 'OS process attribution unavailable: ' + type(exc).__name__}


def legacy_observation(root, run, spec, missing_path):
    """Reconcile pre-lease evidence without inventing execution metadata.

    A retained terminal is only one fact. Native persistence must bind the same
    Session/turn, its kernel writer lock must be free, and OS attribution must
    find no native process or tool descendants. Both continuation callers use
    this path; Runtime repeats it while holding the request/Session lease.
    """
    import harness_tasks as tasks
    from dsh_headless import query_session
    from agent_base import child_environment
    observation = {'state': 'unknown', 'source': 'original_native_and_os_evidence',
                   'run_id': run['id'], 'request_id': run['request_id'],
                   'missing_metadata': str(missing_path),
                   'detail_command': 'python3 bin/run_worker.py --request ' + run['request_id'] +
                                     ' --run-id ' + run['id'] + ' --read-result'}
    def unknown(reason):
        return {**observation, 'state': 'unknown', 'reason': reason}
    try:
        native = run.get('native_identity')
        if not native or native.get('host') != 'deepseek_harness' or not native.get('turn_id'):
            return unknown('Original native Session/turn binding missing; inspect the original run receipts')
        cwd = spec.get('native_cwd')
        if not cwd or native.get('cwd') != cwd or (spec.get('requested_session_id') not in (None, native['id'])):
            return unknown('Original launch and native Session directory/identity do not agree')
        config_ref = tasks.evidence_ref(root, run['config_ref'])
        config = json.loads(Path(config_ref['path']).read_bytes())
        request = tasks.read_request(root, run['request_id'])
        role = 'scientific_reviewer' if request['independent'] else request['recipient_role']
        host = config['hosts'][config['roles'][role]['host']]
        if host.get('adapter') != 'dsh-headless':
            return unknown('Original native host query is unavailable')
        receipt_ref = tasks.evidence_ref(root, run['latest_receipt'])
        receipt = json.loads(Path(receipt_ref['path']).read_bytes())
        if (receipt.get('run_id') != run['id'] or receipt.get('request_id') != run['request_id']
                or receipt.get('native_identity') != native):
            return unknown('Original receipt does not bind this exact run/Session/turn')
        observation['receipt_ref'] = receipt_ref
        home = Path(host['dsh_home']).resolve(strict=True)
        def query():
            return query_session(host, native, cwd, env=child_environment())
        first = query()
        if first.get('read_observation_issue') or first.get('session_id') != native['id'] or first.get('cwd') != cwd:
            return unknown('Exact native Session readback unavailable or mismatched: ' + str(first.get('read_observation_issue', 'identity')))
        artifact = Path(first['artifact_path']).resolve(strict=True)
        artifact.relative_to(home / 'sessions')
        lock_path = artifact.with_name('session.lock')
        # Open only the existing native lock. An absent/empty file alone proves
        # nothing, and this observer must not create or replace native artifacts.
        try:
          with native_read_lock(lock_path) as lock_fact:
            observed = query()
            if (observed.get('read_observation_issue') or observed.get('session_id') != native['id']
                    or observed.get('cwd') != cwd or observed.get('artifact_path') != str(artifact)):
                return unknown('Native artifact changed or became unreadable under its OS lock')
            observation['native_observation'] = {k: observed[k] for k in
                ('session_id', 'cwd', 'latest_turn', 'latest_terminal', 'terminal', 'native_goal', 'artifact_path',
                 'unreturned_tool_calls','persisted_event_count') if k in observed}
            observation['native_lock'] = lock_fact
            if int(observed.get('latest_turn', -1)) < int(native['turn_id']):
                return unknown('Native Session does not contain the originally observed turn')
            processes = session_process_observation(native, home)
            observation['process_observation'] = processes
            if processes['state'] != 'clear':
                return {**observation, 'state': processes['state'], 'reason': processes['reason']}
            latest_terminal = observed.get('latest_terminal')
            if str(observed.get('latest_turn')) == str(native['turn_id']):
                latest_terminal = latest_terminal or observed.get('terminal')
            observation['native_result_state']='terminal' if latest_terminal in ('completed','error','aborted','interrupted') else 'unknown'
            observation['data_effect_state']='unchanged; inspect original native operation receipts'
            return {**observation, 'state': 'exited',
                    'reason': 'Exact native Session readable, shared lock excludes a writer, complete OS attribution clear; native result and data effects remain separate'}
        except BlockingIOError:
            return {**observation,'state':'active','reason':'Original native Session writer holds its OS lock','native_lock':str(lock_path)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return unknown('Original evidence/OS ownership cannot be verified: ' + type(exc).__name__)


class ExecutionLease:
    def __init__(self, root, request, home, run_id, native=None, *, defer_metadata=False):
        import harness_tasks as tasks
        from harness_package import author_lineage
        self.root, self.stack = Path(root), contextlib.ExitStack()
        with contextlib.closing(tasks._connect(root, True)) as c:
            self.chain = author_lineage(c, request)[-1]
        self.path = metadata_path(home, run_id)
        self.native = native
        self.defer_metadata = defer_metadata
        self.value = {'run_id': run_id, 'request_id': request['id'], 'chain': self.chain,
                      'manager': process_identity(os.getpid()), 'phase': 'launch_intent'}
        self.bound = None
        self.metadata_lock = threading.RLock()

    def __enter__(self):
        self.stack.enter_context(FileLock(self.root, ['request', self.chain]))
        if self.native:
            self.bind(self.native, record=not self.defer_metadata)
        return self

    def intent(self):
        with self.metadata_lock:
            atomic_json(self.path, self.value)

    def spawned(self, process):
        with self.metadata_lock:
            self.value.update(phase='spawned', child=process_identity(process.pid))
            atomic_json(self.path, self.value)

    def tool_intent(self, key):
        with self.metadata_lock:
            if self.value['phase'] in ('settled', 'manager_released'):
                raise ExecutionBusy('Original execution has released tool admission; no late tool launch')
            self.value.setdefault('tools', {})[key] = {'phase': 'launch_intent'}
            atomic_json(self.path, self.value)

    def tool_spawned(self, key, process):
        with self.metadata_lock:
            self.value['tools'][key] = {'phase': 'spawned', 'process': {'pgid': process.pid, **process_identity(process.pid)}}
            atomic_json(self.path, self.value)

    def tool_not_started(self, key):
        with self.metadata_lock:
            self.value['tools'][key] = {'phase': 'not_started'}
            atomic_json(self.path, self.value)

    def bind(self, native, *, record=True):
        with self.metadata_lock:
            key = (native['host'], native['kind'], native['id'])
            if self.bound != key:
                self.stack.enter_context(FileLock(self.root, ['native', *key]))
                self.bound = key
            self.value['native_identity'] = native
            if record and self.path.exists():
                atomic_json(self.path, self.value)

    def settled(self):
        with self.metadata_lock:
            self._settled()

    def _settled(self):
        if tools_observation(self.value.get('tools', {}))['state'] != 'exited':
            self.value['phase'] = 'manager_released'
            atomic_json(self.path, self.value)
            return
        child = self.value.get('child')
        if child and original_group(child)['state'] != 'exited':
            self.value['phase'] = 'manager_released'
            atomic_json(self.path, self.value)
            return
        self.value['phase'] = 'settled'
        atomic_json(self.path, self.value)

    def __exit__(self, *args):
        self.stack.close()
