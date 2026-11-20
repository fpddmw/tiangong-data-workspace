"""DeepSeek Harness headless transport for the existing agent runtime."""

from contextlib import nullcontext
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import select
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue

from agent_base import (
    CapabilityUnavailable, child_environment, configure_workspace_sandbox, identity,
    native_start_boundary, scope_workspace_writes, native_cli_environment,
)
from collaboration_tools import CONTROLLED_TOOLS, ControlledTools, editor_preflight
from harness_hosts import NativeStartRejected
from codex_host import safe_diagnostic


HOST = 'deepseek_harness'
MODULE = Path(__file__).with_name('dsh_native.mjs')
NATIVE_CODE = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,100}')


class NativeTransportError(CapabilityUnavailable):
    def __init__(self, message, observation):
        super().__init__(message)
        self.observation = observation


def _kind(event):
    reason = event.get('reason')
    return reason.get('kind') if isinstance(reason, dict) else None


def _code(error, fallback):
    value = error.get('code') if isinstance(error, dict) else None
    return value if isinstance(value, str) and NATIVE_CODE.fullmatch(value) else fallback


def _native_id(value):
    return isinstance(value, str) and bool(value) and '\0' not in value


def query_session(host, native, cwd, *, env=None):
    """Read an exact persisted Session without auth, new turns or write handles."""
    if not native.get('turn_id') or not host.get('node') or not host.get('binary'):
        return {'read_observation_issue': 'Original native turn or query executable unavailable'}
    try:
        desktop = host.get('launch_mode') == 'desktop-cli'
        env = dict(os.environ if env is None else env)
        if desktop:
            env['ELECTRON_RUN_AS_NODE'] = '1'
        command = [host['node'], '--expose-internals'] if desktop else [host['node']]
        module_entry = host['module_entry'] if desktop else host['binary']
        call = subprocess.run(command + [str(MODULE), '--query', module_entry,
            str(Path(host['dsh_home']) / 'sessions'), native['id'], native['turn_id']],
            cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
        doc = json.loads(call.stdout)
        if not isinstance(doc, dict):
            return {'read_observation_issue': 'Native result is not an observation'}
        if call.returncode:
            doc.setdefault('read_observation_issue', 'Native query failed')
        return doc
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return {'read_observation_issue': type(exc).__name__}


def _total_usage(steps, usage):
    if not steps or set(usage) != steps or any(not isinstance(row, dict) for row in usage.values()):
        return None
    fields = {'inputTokens': 'input_tokens', 'outputTokens': 'output_tokens', 'totalTokens': 'total_tokens',
              'cacheReadTokens': 'cached_input_tokens', 'cacheWriteTokens': 'cache_write_input_tokens',
              'reasoningTokens': 'reasoning_output_tokens'}
    return {field: sum(row[key] for row in usage.values()) for key, field in fields.items()
            if all(type(row.get(key)) is int and row[key] >= 0 for row in usage.values())} or None


class DeepSeekAdapter:
    requires_program_work_root = True
    tool_boundary = 'DSH native filesystem visibility and write sandbox; Bash has no native network isolation'
    read_operation = 'retained_native_cli_receipt_and_exact_session_query'
    stop_operation = 'original_foreground_process; process exit is not native session confirmation'
    scope_writes = staticmethod(scope_workspace_writes)

    @staticmethod
    def configure(root, config):
        host, provider = config['host'], config['provider']
        if not config['programmatic'] or host.get('entry') != 'headless':
            raise CapabilityUnavailable('DSH uses the managed programmatic headless entry.')
        if config.get('effort') is not None and (not isinstance(config['effort'], str) or not config['effort'].strip()):
            raise CapabilityUnavailable('A selected native reasoning effort must be a nonempty string.')
        if (provider.get('selection') != 'existing-host-configuration'
                or not isinstance(provider.get('dsh_provider'), str) or not provider['dsh_provider']):
            raise CapabilityUnavailable('DSH requires an existing native headless provider identifier.')
        if not all(isinstance(host.get(key), str) and Path(host[key]).is_absolute()
                   for key in ('dsh_home', 'node', 'binary')):
            raise CapabilityUnavailable('DSH requires exact executable, Node, and headless home paths.')
        if host.get('launch_mode', 'node') not in ('node', 'desktop-cli'):
            raise CapabilityUnavailable('Unsupported DSH launch mode.')
        if host.get('launch_mode') == 'desktop-cli' and not (
                isinstance(host.get('module_entry'), str) and Path(host['module_entry']).is_absolute()):
            raise CapabilityUnavailable('DSH Desktop requires its exact bundled JavaScript module entry for session readback.')
        paths = host.get('tool_path', [])
        if not isinstance(paths, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
            raise CapabilityUnavailable('DSH tool_path must contain absolute runtime directories.')
        if config.get('profile') == 'native-workspace':
            configure_workspace_sandbox(root, config)
            if config['native_policy']['sandbox'] != 'workspace-write':
                raise CapabilityUnavailable('DSH workspace roles use the native workspace-write profile.')
        elif config.get('profile') == 'read-only-evidence':
            policy = config.get('native_policy', {})
            if not isinstance(policy, dict) or ('network_access' in policy and type(policy['network_access']) is not bool):
                raise CapabilityUnavailable('Native read-only network access must be explicit when set.')
            config['native_policy'] = {**policy, 'sandbox': 'read-only', 'writable_roots': []}

    @staticmethod
    def tool_capabilities(config):
        profile = config.get('profile')
        if profile not in ('read-only-evidence', 'native-workspace'):
            return set()
        supported = {'read_files'}
        from local_check import available
        if available():
            supported.add('local_checks')
        if profile == 'native-workspace':
            supported.add('write_files')
        if config['native_policy'].get('network_access') is not False:
            supported.update(('local_checks', 'native_cli'))
        return supported

    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.cwd = Path(config.get('native_cwd', root)).resolve(strict=True)
        self.binary = shutil.which(config['host']['binary'])
        self.node = shutil.which(config['host']['node'])
        self.home = Path(config['host']['dsh_home'])
        self.process = None
        self.native = None
        self.control = None
        self.control_fd = None
        self.identity_refreshes = []
        self.env = native_cli_environment(root, config)
        self.write_roots = (config['native_policy']['writable_roots'] if config.get('profile') == 'native-workspace' else [])
        self.mode = 'workspace-write' if self.write_roots else 'read-only'
        from local_check import available
        self.controlled = ControlledTools(root, self.cwd, env=self.env, write_roots=self.write_roots,
            check_available=available() and config.get('profile') in ('read-only-evidence', 'native-workspace'),
            cli_available=config.get('role') == 'executor' and 'native_cli' in self.tool_capabilities(config),
            cli_network_access=config['native_policy'].get('network_access') is True,
            execution_lease=config.get('_execution_lease'),
            preflight=self._editor_preflight, identity_refreshes=self.identity_refreshes)
        self.env.update(DSH_HOME=str(self.home), DSH_PERMISSION_MODE=self.mode)
        self.desktop = config['host'].get('launch_mode') == 'desktop-cli'
        if self.desktop:
            self.env['ELECTRON_RUN_AS_NODE'] = '1'
        if config['host'].get('tool_path'):
            self.env['PATH'] = os.pathsep.join([*config['host']['tool_path'], self.env.get('PATH', '')])

    @property
    def review_service(self):
        return self.controlled.review_service

    @review_service.setter
    def review_service(self, value):
        self.controlled.review_service = value

    @property
    def auth_available(self):
        return self.controlled.auth_available

    @auth_available.setter
    def auth_available(self, value):
        self.controlled.auth_available = value

    def _selection(self):
        return {'provider': self.config['provider']['dsh_provider'], 'model': self.config['model'],
                **({'reasoningEffort': self.config['effort']} if self.config.get('effort') else {})}

    def _patch(self):
        selection = self._selection()
        tools = []
        if self.config.get('profile') in ('read-only-evidence', 'native-workspace'):
            tools = ['read']
            from local_check import available
            if available():
                tools.append('local_check')
            if self.config['native_policy'].get('network_access') is not False:
                tools.append('bash')
            if self.write_roots:
                tools.extend(('write', 'edit'))
        if self.config.get('role') == 'executor':
            tools.extend(('get_goal', 'create_goal', 'update_goal'))
        if self.review_service:
            tools.extend(('request_review', 'review_result', 'report_finding'))
        if self.auth_available:
            tools.append('refresh_editor_identity')
        if self.controlled.cli_available:
            tools.append('tiangong_cli')
        sse = self.config['host'].get('sse_diagnostic_origin')
        from dsh_capacity import is_pilot_provider
        pilot = is_pilot_provider(self.root, selection['provider'])
        diagnostic_directory = self.config.get('diagnostic_directory')
        return [
            {'id': 'agent-default-model', 'config': selection},
            {'id': 'session-title-llm', 'disabled': True},
            {'id': 'tool-bash', 'config': {'enableRunInBackground': False}},
            {'id': 'headless-runner', 'disabled': True},
            {'id': 'sandbox-policy', 'disabled': True},
            {'insert': [{'id': 'htam-invocation', 'name': str(MODULE.with_name('dsh_runner.mjs')),
             'config': {'moduleEntry': self.config['host'].get('module_entry', self.binary), 'mode': self.mode,
                        'technicalContinue': self.config.get('technical_continue', False)}},
            {'id': 'htam-write-policy', 'name': str(MODULE.with_name('dsh_policy.mjs')),
             'config': {'moduleEntry': self.config['host'].get('module_entry', self.binary),
                        'writeRoots': self.write_roots, 'mode': self.mode,
                        'workspaceRoot': str(self.cwd), 'additionalWritableRoots': self.write_roots}},
            {'id':'htam-task-context','name':str(MODULE.with_name('dsh_context.mjs')),
             'inject':['systemPrompt','agents','goals'],'config':{'workspaceRoot':str(self.root),'python':sys.executable,
                 'snapshotEntry':str(MODULE.with_name('harness_context_hook.py')),
                 'requestId':self.config.get('task_request_id'),'runId':self.config.get('task_run_id')}}]},
            {'insert': [{'id': 'htam-native-tool-profile', 'name': str(MODULE),
                         'inject': ['tools', 'goals', 'agents', 'sandboxPolicy'], 'config': {'allow': tools, 'writeRoots': self.write_roots, 'controlFd': self.control_fd,
                         'moduleEntry': self.config['host'].get('module_entry', self.binary),
                         'timeoutRetries': {'provider': selection['provider'],
                                            'recentTimeoutUntilMs': self.config.get('recent_timeout_until_ms', 0)},
                         'tiangongCliTool': CONTROLLED_TOOLS['tiangong_cli'],
                         'reviewRequestTool': CONTROLLED_TOOLS['request_review'],
                         **({'sseDiagnostics': {'origin': sse, 'directory': diagnostic_directory,
                                               'minimal': pilot, 'timings': pilot}}
                            if (sse or pilot) and diagnostic_directory else {})}}]},
        ]

    def _query(self, native):
        return query_session(self.config['host'], native, self.cwd, env=self.env)

    def _apply_route(self, result, observed):
        route = observed.get('route') or {}
        status = ('unobserved' if not route else 'selected' if all(
            route.get(key) == value for key, value in self._selection().items()) else 'different')
        result = {**result, 'native_session_observation': observed or None, 'native_route_status': status,
                  'observed_model': route.get('model'), 'observed_provider': route.get('provider'),
                  'observed_effort': route.get('reasoningEffort')}
        if result['native_status'] == 'completed' and not route:
            result['native_metadata_pending'] = True
        return result

    def recover_metadata(self, result, *, boundary=None):
        with (boundary or nullcontext)():
            observed = self._query(result['native_identity'])
        if not observed.get('route'):
            return result
        original = {key: value for key, value in result.items()
                    if key not in ('dsh_route_pending', 'native_metadata_pending')}
        return self._apply_route(original, observed)

    def _result(self, native, status, *, final=None, diagnostic=None, tools=None, exit_code=None, usage=None,
                goal_observation=None):
        result = {'native_identity': native, 'native_status': status, 'result': final,
                  'native_context': {'cwd': str(self.cwd)}, 'exit_code': exit_code,
                  'metering': {'usage': usage, 'cost_usd': None}, 'stop_confirmed': status == 'interrupted'}
        if diagnostic:
            result['native_diagnostic'] = diagnostic
        if tools:
            result.update(tools)
        if goal_observation is not None:
            result['native_goal_observation'] = goal_observation
        return self._apply_route(result, self._query(native))

    def _verified_progress(self, started, turn, call_turns, call_ends, file_effects, proposed_reviews):
        from dsh_auto_resume import verified_progress
        work = self.config.get('program_work_home')
        changes = {}
        if work:
            work = Path(work).resolve()
            for call, fact in file_effects.items():
                if call_turns.get(call) != turn or call not in call_ends:
                    continue
                try:
                    path = Path(fact['path']).resolve(strict=True)
                    if not path.is_relative_to(work) or not path.is_file():
                        continue
                    before = changes[path][0] if path in changes else fact['before_sha256']
                    changes[path] = (before, fact['sha256'])
                except (OSError, ValueError):
                    continue
        local = []
        for path, (before, after) in changes.items():
            if before == after:
                continue
            try:
                with path.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() == after:
                        local.append({'kind':'artifact', 'path':str(path), 'sha256':after})
            except OSError:
                continue
        lease = self.config.get('_execution_lease')
        if not lease or not started:
            return local
        return local + verified_progress(self.root, lease.path.parent, lease.value, started, proposed_reviews.get(turn, set()))

    def execute(self, text, observe, guard, *, previous=None, boundary=None, requested_session_id=None):
        if not self.binary or not self.node or not self.home.is_dir():
            raise NativeStartRejected('configured_executable_node_or_home_unavailable')
        if self.config.get('profile') == 'native-workspace' and not self.write_roots:
            raise NativeStartRejected('original_package_write_root_missing')
        if previous and (previous.get('host') != HOST or not _native_id(previous.get('id'))
                         or not re.fullmatch(r'[0-9]+', previous.get('turn_id') or '')):
            raise NativeStartRejected('invalid_original_session_or_turn')
        if previous:
            original = self._query(previous)
            if original.get('cwd') and original['cwd'] != str(self.cwd):
                error = NativeStartRejected('original_session_directory_differs')
                error.observation.update(recorded_cwd=original['cwd'], requested_cwd=str(self.cwd))
                raise error
        guard()
        # Native sandbox roots must exist. Prepare the explicitly granted
        # locations before launch, including a newly selected output directory.
        # An unusable path is a known pre-launch failure, not an unknown turn.
        try:
            for directory in self.write_roots:
                Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise NativeStartRejected('write_root_unavailable:' + type(exc).__name__) from exc
        auth = self._editor_preflight()
        self.auth_available = auth is not None
        if auth is not None:
            from collaboration_tools import identity_note
            text += identity_note(auth)
        with tempfile.TemporaryDirectory(prefix='htam-dsh-patch-') as scratch, tempfile.TemporaryFile() as prompt:
            return self._execute_controlled(text, observe, guard, previous, boundary, auth, scratch, prompt)

    def _execute_controlled(self, text, observe, guard, previous, boundary, auth, scratch, prompt):
        capacity = self.config.get('_dsh_capacity')
        if capacity:
            self.env.update(capacity.environment)
        with capacity.admit(guard) if capacity else nullcontext():
            return self._spawn_controlled(text, observe, guard, previous, boundary, auth, scratch, prompt, capacity)

    def _spawn_controlled(self, text, observe, guard, previous, boundary, auth, scratch, prompt, capacity):
        parent, child = socket.socketpair()
        with parent, child:
            self.control = parent
            self.control_fd = child.fileno()
            patch = Path(scratch) / 'runtime.json'
            patch.write_text(json.dumps(self._patch()), encoding='utf-8')
            prompt.write(text.encode('utf-8'))
            prompt.seek(0)
            command = [self.binary] if self.desktop else [self.node, self.binary]
            cmd = command + ['--profile', 'headless', '--patch', str(patch), '--json',
                   '--permission-mode', self.mode, '--approval-policy', 'never']
            for root in self.write_roots:
                cmd.extend(['--add-dir', root])
            if previous:
                cmd.extend(['--session-id', previous['id']])
            with native_start_boundary(guard, boundary):
                try:
                    self.process = subprocess.Popen(cmd + ['-'], cwd=self.cwd, env=self.env, stdin=prompt,
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                    start_new_session=True, pass_fds=(child.fileno(),))
                except OSError as exc:
                    raise NativeStartRejected(type(exc).__name__) from exc
                if capacity:
                    capacity.launched = True
                if lease := self.config.get('_execution_lease'):
                    lease.spawned(self.process)
            child.close()
            result = self._read_stream(observe, guard, previous)
            if auth is not None:
                result['editor_session_preflight'] = auth
            if self.identity_refreshes:
                result['editor_session_refreshes'] = self.identity_refreshes
            return result

    def _editor_preflight(self):
        return editor_preflight(self.root, self.config, self.tool_capabilities(self.config))

    def _read_stream(self, observe, guard, previous):
        queue = Queue(maxsize=8)
        stderr_tail = deque(maxlen=4)
        control_input = b''
        cancelled_controls = set()

        def wait_cancelled(ident):
            nonlocal control_input
            if self.process.poll() is not None:
                return True
            # Cancellation travels over the existing duplex control socket, so
            # it remains readable while a result wait holds the stdout handler.
            if select.select([self.control], [], [], 0)[0]:
                chunk = self.control.recv(4096)
                if not chunk:
                    return True
                control_input += chunk
                while b'\n' in control_input:
                    line, control_input = control_input.split(b'\n', 1)
                    value = json.loads(line)
                    if value.get('cancelled') is True:
                        cancelled_controls.add(value.get('id'))
            return ident in cancelled_controls

        def drain_stderr():
            try:
                with self.process.stderr as stream:
                    for line in stream:
                        from safe_diagnostics import retain
                        stderr_tail.append(retain(line.decode('utf-8', errors='replace'),
                            directory=self.config.get('diagnostic_directory'), source='dsh_stderr'))
            except OSError:
                pass

        stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
        stderr_reader.start()

        def pump():
            try:
                with self.process.stdout as stdout:
                    for line in stdout:
                        queue.put(line)
            except OSError:
                queue.put(OSError('DSH stdout read failed.'))
            finally:
                queue.put(None)

        threading.Thread(target=pump, daemon=True).start()
        last_turn = int(previous['turn_id']) if previous else -1
        turn, end, final, rejected, native_error = None, None, None, None, None
        terminal_at = turn_started_at = None
        goal_observation = None
        ambiguous = False
        calls, failures, shell, tool_results = {}, [], {}, set()
        call_turns, call_starts, call_ends, control_activity = {}, {}, {}, set()
        file_effects, proposed_reviews = {}, {}
        request_timings, retry_events, timeout_limits = [], [], []
        started = time.monotonic()
        stream_epoch = time.time()
        tool_seconds = 0.
        headers = response_attempts = 0
        usage, steps = {}, set()
        try:
            while True:
                guard()
                try:
                    line = queue.get(timeout=0.5)
                except Empty:
                    continue
                if line is None:
                    break
                if isinstance(line, OSError):
                    raise line
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get('type')
                if event_type == 'htam_control':
                    guard()
                    reply = self.controlled.dispatch(event.get('kind'), event.get('text'), guard=guard,
                                                     cancelled=lambda: wait_cancelled(event.get('id')))
                    kind, detail = event.get('kind'), reply.get('diagnostic')
                    if reply.get('accepted') and isinstance(detail, dict) and (
                            kind == 'request_review' and detail.get('review_id') and detail.get('state') in ('requested','intent','running','succeeded')
                            or kind == 'review_result' and detail.get('state') == 'received'
                            or kind in ('local_check','tiangong_cli') and detail.get('exit_code') == 0):
                        control_activity.add((turn,kind))
                        if kind == 'request_review':
                            proposed_reviews.setdefault(turn, set()).add(detail['review_id'])
                    guard()  # A stopped/unadmitted invocation cannot advance on a tool reply.
                    self.control.sendall((json.dumps({'id': event.get('id'), **reply}) + '\n').encode())
                    cancelled_controls.discard(event.get('id'))
                elif event_type == 'session':
                    session_id = event.get('sessionId')
                    if (self.native or not _native_id(session_id)
                            or event.get('cwd') != str(self.cwd) or previous and previous['id'] != session_id):
                        raise CapabilityUnavailable('DSH opening event differs from the exact native session or role cwd.')
                    self.native = identity(HOST, 'session', session_id, self.root)
                    observe(self.native, 'session_ready', {'cwd': str(self.cwd), 'source': 'dsh_session_opening'})
                elif not self.native and event_type == 'error':
                    native_error = safe_diagnostic(event.get('message'))
                    if event.get('native_status') == 'not_started':
                        rejected = _code(event, 'NativeSetupFailed')
                elif self.native and event_type == 'status':
                    phase, value = event.get('phase'), event.get('turn')
                    if phase == 'turn_start':
                        if (type(value) is not int or value <= last_turn or final is not None
                                or turn is not None and (end is None or value <= turn or _kind(end) != 'completed')):
                            ambiguous = True
                            continue
                        prior_turn = ({'turn_id': str(turn), 'terminal': _kind(end)} if turn is not None else None)
                        turn = value
                        turn_started_at = datetime.now(timezone.utc).isoformat()
                        end = None
                        goal_observation = None
                        self.native = {**self.native, 'turn_id': str(value)}
                        observe(self.native, 'running', {'cwd': str(self.cwd), 'source': 'dsh_turn_start',
                                                   **({'previous_turn': prior_turn} if prior_turn else {})})
                    elif phase == 'step_start' and value == turn:
                        if type(event.get('step')) is int:
                            steps.add((turn, event['step']))
                    elif phase == 'step_end' and value == turn and type(event.get('step')) is int:
                        usage[(turn, event['step'])] = event.get('usage')
                    elif phase == 'turn_end':
                        if turn is None or value != turn or end is not None:
                            ambiguous = True
                        else:
                            end = event
                            terminal_at = datetime.now(timezone.utc).isoformat()
                elif (event_type == 'htam_goal_state' and self.native and end
                      and event.get('sessionId') == self.native['id'] and event.get('turn') == turn
                      and event.get('source') == 'dsh_live_agent'):
                    goal_observation = event
                elif event_type == 'final':
                    if final is not None or not end:
                        ambiguous = True
                    final = event
                elif self.native and event_type == 'htam_request_header':
                    headers += 1
                elif self.native and event_type == 'htam_response_attempt':
                    response_attempts += 1
                elif self.native and event_type == 'htam_request_timing':
                    request_timings.append({k:v for k,v in event.items() if k != 'type'})
                elif self.native and event_type == 'htam_request_retry':
                    retry_events.append({k:v for k,v in event.items() if k != 'type'})
                elif self.native and event_type == 'htam_timeout_limit':
                    timeout_limits.append({k:v for k,v in event.items() if k != 'type'})
                elif self.native and event_type == 'tool_call':
                    call, name = event.get('callId'), event.get('tool')
                    if isinstance(call, str) and isinstance(name, str):
                        calls[call] = name
                        call_turns[call] = turn
                        call_starts[call] = time.monotonic()
                elif self.native and event_type == 'htam_shell_result':
                    call, code = event.get('callId'), event.get('exitCode')
                    if isinstance(call, str) and calls.get(call) == 'bash' and type(code) is int:
                        shell[call] = code
                elif self.native and event_type == 'htam_file_effect':
                    call, path, sha = event.get('callId'), event.get('path'), event.get('sha256')
                    if calls.get(call) in ('write','edit') and isinstance(path,str) and isinstance(sha,str):
                        file_effects[call] = {'path':path,'sha256':sha,'before_sha256':event.get('before_sha256')}
                elif self.native and event_type == 'tool_result':
                    call = event.get('callId')
                    if isinstance(call, str) and call in calls and call not in tool_results:
                        tool_results.add(call)
                        tool_seconds += time.monotonic() - call_starts[call]
                        if event.get('status') == 'completed':
                            call_ends[call] = time.monotonic()
                        if event.get('status') == 'error':
                            failures.append({'native_item_id': call, 'tool_name': calls[call], 'kind': 'tool_error',
                                             'error_class': _code(event.get('error'), 'DSHToolError')})
            code = self.process.wait()
            stderr_reader.join(timeout=1)
        except BaseException:
            self.close()
            # A descendant may retain stdout after the direct process exits.
            # Cleanup must never wait forever for a pipe EOF.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    if queue.get(timeout=0.1) is None:
                        break
                except Empty:
                    continue
            raise
        if self.native is None:
            diagnostic = {'exit_code': code, 'native_error': native_error,
            'stderr': list(stderr_tail)}
            if code == 1 and rejected and final is None:
                error = NativeStartRejected(rejected, code)
                error.observation.update(diagnostic)
                raise error
            raise NativeTransportError('DSH exited before an observed opening: ' + (native_error or 'read original safe diagnostic pointers'), diagnostic)
        status, diagnosis, text = 'unknown', 'missing_or_ambiguous_native_terminal', None
        if (not ambiguous and end and final and final.get('sessionId') == self.native['id']
                and _kind(end) == _kind(final)):
            reason = _kind(final)
            if code == 0 and reason == 'completed' and isinstance(final.get('text'), str):
                status, diagnosis, text = 'completed', None, final['text']
            elif code == 1 and reason == 'error':
                status, diagnosis = 'failed', _code(final['reason'].get('error'), 'DSHNativeTurnFailed')
            elif code == 1 and reason == 'aborted':
                status, diagnosis = 'interrupted', None
        shell_results = [{'native_item_id': call, 'exit_code': shell.get(call)}
                         for call, name in calls.items() if name == 'bash' and call in tool_results]
        failures.extend({'native_item_id': item['native_item_id'], 'tool_name': 'bash', 'kind': 'nonzero_exit',
                         'error_class': 'ShellNonzeroExit', 'exit_code': item['exit_code']}
                        for item in shell_results if type(item['exit_code']) is int and item['exit_code'] != 0)
        result = self._result(self.native, status, final=text, diagnostic=diagnosis, exit_code=code,
                            goal_observation=goal_observation,
                            usage=_total_usage(steps, usage),
                            tools={'tools_called': sorted(set(calls.values())), 'tool_failures': failures,
                                   'shell_results': shell_results})
        if final and _kind(final) == 'error':
            from safe_diagnostics import safe_error
            result['native_error'] = safe_error(final['reason'].get('error'))
        native_activity = [call for call in call_ends if calls[call] in ('read','write','edit')
                           or calls[call]=='bash' and shell.get(call)==0
                           or (call_turns[call],calls[call]) in control_activity]
        first_action = min((call_ends[call]-started for call in native_activity),default=None)
        from dsh_auto_resume import classify
        progress = self._verified_progress(turn_started_at, turn, call_turns, call_ends, file_effects, proposed_reviews) \
            if status == 'failed' and classify(result.get('native_error')) else []
        result['turn_activity'] = {'tool_calls':len(calls), 'tool_results':len(tool_results),
            'native_terminal_at':terminal_at,
            'ending_turn_tool_calls':sum(t == turn for t in call_turns.values()),
            'ending_turn_verified_progress':progress,
            'unreturned_tools':len(calls.keys() - tool_results),
            'model_steps':len(steps), 'native_route_headers':headers, 'model_response_attempts':response_attempts,
            'request_timings':request_timings, 'request_retries':retry_events, 'timeout_retry_limits':timeout_limits,
            'summed_tool_seconds':round(tool_seconds,6),
            'first_effective_action_seconds':round(first_action,6) if first_action is not None else None,
            'first_effective_action_at':datetime.fromtimestamp(stream_epoch+first_action,timezone.utc).isoformat()
                if first_action is not None else None}
        return result

    def readresult(self, native, *, boundary=None):
        with (boundary or nullcontext)():
            observed = self._query(native)
            exact = (observed.get('source') == 'dsh_persisted_session_log'
                     and str(observed.get('latest_turn')) == native.get('turn_id'))
            goal = observed.get('native_goal') or {}
            # A cold log has no process-local activation. An active Goal may be
            # between automatic rounds; only the original transport proves quiescence.
            active = goal.get('phase') == 'active'
            terminal = observed.get('terminal')
            if exact and terminal == 'completed' and not active and not observed.get('goal_observation_issue'):
                final = observed.get('final_text')
                if isinstance(final, str):
                    return self._apply_route({'native_identity':native, 'native_status':'completed', 'result':final,
                        'native_context':{'cwd':str(self.cwd)}, 'metering':{'usage':None,'cost_usd':None}}, observed)
            if exact and terminal == 'aborted':
                return self._apply_route({'native_identity':native, 'native_status':'interrupted', 'result':None,
                    'native_context':{'cwd':str(self.cwd)}, 'metering':{'usage':None,'cost_usd':None},
                    'stop_confirmed':True}, observed)
            if exact and terminal == 'error':
                return self._apply_route({'native_identity':native, 'native_status':'failed', 'result':None,
                    'native_context':{'cwd':str(self.cwd)}, 'metering':{'usage':None,'cost_usd':None},
                    'native_diagnostic':_code(observed.get('native_error'), 'DSHNativeTurnFailed')}, observed)
            return self._apply_route({'native_identity':native, 'native_status':'unknown',
                'result': observed.get('final_text') if observed.get('source') == 'dsh_persisted_session_log' else None,
                'native_diagnostic':'original_session_still_active_or_terminal_unavailable'}, observed)

    def stop(self, native, *, boundary=None):
        with (boundary or nullcontext)():
            if self.native == native:
                self.close()
            return self.readresult(native)

    def close(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            # Also close descendants which still own the transport pipe.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=2)
