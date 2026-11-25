"""Local ACP transport and client-owned file/terminal capabilities.

There is no agent loop here. Devin owns generation; the client enforces the
configured filesystem boundary and retains each actual tool process.
"""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import uuid

from agent_base import CapabilityUnavailable
from safe_diagnostics import safe_text, safe_value
from production_review import scientific_path


class ACPError(CapabilityUnavailable):
    def __init__(self, method, error, *, request_id=None, response_received=False):
        self.observation = {'method': method, 'request_id': request_id,
            'response_received': response_received,
            'request_outcome': 'rejected' if response_received else 'unknown',
            'error': safe_value(error), 'replay_allowed': False}
        super().__init__(json.dumps(self.observation, ensure_ascii=False))


def pre_prompt_rejection(rows, *, session_id=None):
    """Correlate an original native error; replayed history is not a new tool call.

    An attempted prompt, client tool request, duplicate ID, incomplete request
    or transport failure cannot prove this invocation did not advance.
    """
    requests={};responses={};replayed=0
    for row in rows:
        if row.get('transport_error'):return None
        if row.get('direction')=='client' and row.get('method'):
            key=row.get('id');method=row['method']
            if method=='session/prompt' or key is None or key in requests:return None
            requests[key]=row
        elif row.get('direction')=='agent':
            if row.get('method'):
                if 'id' in row:return None  # Actual client capability request.
                if row['method']=='session/update':replayed+=1
            elif 'id' in row:
                key=row['id']
                if key in responses:return None
                responses[key]=row
    if not requests:return None
    key=next(reversed(requests));request=requests[key];response=responses.get(key,{})
    if request['method'] not in ('initialize','session/new','session/load','session/set_mode','session/set_config_option'):
        return None
    if (response.get('jsonrpc')!='2.0' or not isinstance(response.get('error'),dict)
            or type(response['error'].get('code')) is not int):return None
    for prior in list(requests)[:-1]:
        if 'result' not in responses.get(prior,{}):return None
    params=request.get('params',{})
    if session_id and request['method']!='initialize' and params.get('sessionId')!=session_id:return None
    return {'method':request['method'],'request_id':key,'response_received':True,
        'request_outcome':'rejected','error':safe_value(response['error']),
        'prompt_submitted':False,'client_tool_requests':0,'replayed_notifications':replayed}


def completed_setup_without_prompt(rows, session_id):
    """Verify a complete ACP setup for the exact session, before any prompt intent."""
    requests = {}
    responses = {}
    allowed = {'initialize','session/new','session/load','session/set_config_option','session/set_mode'}
    for row in rows:
        if row.get('transport_error'):
            return None
        key = row.get('id')
        method = row.get('method')
        if row.get('direction') == 'client':
            if method not in allowed or key != len(requests)+1:
                return None
            requests[key] = row
        elif row.get('direction') == 'agent' and key is not None:
            if (method or key not in requests or key in responses or row.get('jsonrpc')!='2.0'
                    or row.get('request_method')!=requests[key]['method'] or not isinstance(row.get('result'),dict)):
                return None
            responses[key] = row['result']
    if not requests or requests.keys()!=responses.keys() or requests[1]['method']!='initialize':
        return None
    setups = [key for key,row in requests.items() if row['method'] in ('session/new','session/load')]
    if setups!=[2] or responses[1].get('protocolVersion')!=1:
        return None
    setup = requests[2]
    bound = responses[2] if setup['method']=='session/new' else setup.get('params',{})
    if bound.get('sessionId')!=session_id:
        return None
    if any(row.get('params',{}).get('sessionId')!=session_id for key,row in requests.items() if key>2):
        return None
    return {'target_session':session_id,'last_setup_request':requests[len(requests)]['method'],
            'prompt_submitted':False,'client_tool_requests':0}


class ACPHost:
    def __init__(self, command, *, cwd, env, on_event, on_request, lease=None, transcript=None):
        self.pending, self.serial = {}, 0
        self.client_requests = []
        self.lock, self.write_lock = threading.RLock(), threading.Lock()
        self.on_event, self.on_request = on_event, on_request
        self.transcript, self.closed = transcript, threading.Event()
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            start_new_session=True, close_fds=True)
        if lease:
            lease.spawned(self.process)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr = threading.Thread(target=self._stderr, daemon=True)
        self.stderr.start()
        self.watcher = threading.Thread(target=self._watch_exit, daemon=True)
        self.watcher.start()

    def retain(self, value, *, durable=False):
        if self.transcript:
            with self.lock, Path(self.transcript).open('a') as output:
                output.write(json.dumps(safe_value(value), ensure_ascii=False) + '\n')
                if durable:
                    output.flush()
                    os.fsync(output.fileno())

    def send(self, value):
        with self.write_lock:
            try:
                self.process.stdin.write(json.dumps({'jsonrpc': '2.0', **value}, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ACPError(value.get('method'), 'ACP transport closed; reconcile original Session') from exc

    def submit(self, method, params):
        with self.lock:
            self.serial += 1
            key = self.serial
            reply = queue.Queue(maxsize=1)
            self.pending[key] = {'method':method,'reply':reply}
        self.retain({'direction': 'client', 'id': key, 'method': method, 'params': params},
                    durable=method=='session/prompt')
        self.send({'id': key, 'method': method, 'params': params})
        return key, reply

    def result(self, handle, *, timeout=30):
        key, reply = handle
        with self.lock:
            method=self.pending[key]['method']
        try:
            response = reply.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError('ACP response pending; original request remains in flight')
        with self.lock:
            self.pending.pop(key, None)
        if 'error' in response:
            raise ACPError(method,response['error'],request_id=key,
                response_received=not response.get('_transport_failure',False))
        return response.get('result', {})

    def call(self, method, params, *, timeout=30):
        return self.result(self.submit(method, params), timeout=timeout)

    def _read(self):
        try:
            for line in self.process.stdout:
                value = json.loads(line)
                if 'method' not in value and 'id' in value:
                    with self.lock:
                        pending = self.pending.get(value['id'])
                    self.retain({'direction':'agent',**value,
                        **({'request_method':pending['method']} if pending else {})})
                    if pending:pending['reply'].put_nowait(value)
                elif 'id' in value:
                    self.retain({'direction':'agent',**value})
                    # Record before scheduling the callback: a following error
                    # response can otherwise overtake that callback thread.
                    self.client_requests.append({'id':value['id'],'method':value['method']})
                    threading.Thread(target=self._request, args=(value,), daemon=True).start()
                else:
                    self.retain({'direction':'agent',**value})
                    self.on_event(value)
        except (ValueError, OSError, queue.Full) as exc:
            self.retain({'transport_error': type(exc).__name__})
        finally:
            self._disconnect('ACP disconnected; native/data effects unknown')
            self.process.stdout.close()

    def _disconnect(self, reason):
        self.closed.set()
        with self.lock:
            waiting = list(self.pending.values())
        for pending in waiting:
            try:
                pending['reply'].put_nowait({'_transport_failure':True,'error': {'message': reason}})
            except queue.Full:
                pass

    def _watch_exit(self):
        self.process.wait()
        # A descendant can retain stdout after the native process exits. Let
        # the reader drain an actual final response, then report interruption;
        # never wait indefinitely for EOF or infer descendant termination.
        self.reader.join(timeout=.2)
        self._disconnect('Original ACP process exited; descendant/data effects require observation')

    def _request(self, value):
        try:
            result = self.on_request(value['method'], value.get('params', {}))
            reply = {'id': value['id'], 'result': result}
        except Exception as exc:
            reply = {'id': value['id'], 'error': {'code': -32603, 'message': safe_text(exc)}}
        written = False
        try:
            self.send(reply)
            written = True
        except ACPError:
            pass  # Original transport failure remains unknown; no second send.
        finally:
            self.retain({'direction': 'client', **reply, 'request_method': value['method'],
                         'response_write_completed': written})

    def _stderr(self):
        try:
            for line in self.process.stderr:
                self.retain({'source': 'devin_stderr', 'diagnostic': safe_text(line.rstrip())})
        finally:
            self.process.stderr.close()

    def close(self):
        # EOF is transport shutdown, never evidence of cancellation or quiescence.
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return
        self.reader.join(timeout=1)
        self.stderr.join(timeout=1)
        if not self.reader.is_alive():
            self.process.stdout.close()
        if not self.stderr.is_alive():
            self.process.stderr.close()


class ClientTools:
    """ACP filesystem/terminal service, with OS-enforced command permissions."""
    def __init__(self, root, policy, *, env, directory, lease=None, guard=lambda: None):
        if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
            raise CapabilityUnavailable('Devin client tools require the qualified macOS OS sandbox')
        self.root, self.policy = Path(root).resolve(), policy
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.writes = [Path(path).resolve() for path in policy.get('writable_roots', [])]
        self.lease, self.guard = lease, guard
        self.terminals, self.lock = {}, threading.RLock()
        self.closed = False
        # Native model credentials and parent identities never enter tool shells.
        self.env = {key: value for key, value in env.items() if key in
            ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'XDG_STATE_HOME')}
        self.env.update(TMPDIR=str(self.directory), PYTHONDONTWRITEBYTECODE='1')

    def file(self, value, *, write=False):
        path = scientific_path(self.root, value, must_exist=not write)
        if any(part in ('.git', '.devin') for part in path.relative_to(self.root).parts):
            raise ValueError('Native configuration and Git metadata are outside worker file tools')
        if write and not any(path == root or path.is_relative_to(root) for root in self.writes):
            raise ValueError('File is outside the assigned writable roots')
        return path

    def sandbox(self):
        private = {self.root / key for key in ('.control', '.env', '.secrets', '.state', '.foundry', '.runtime', '.codex', '.claude', '.devin', '.git')}
        private.update(p.resolve() for p in self.root.glob('.env*'))
        home = Path.home()
        private.update(home / key for key in ('.ssh', '.aws', '.config', 'Library', '.codex', '.dsh', '.devin', '.local/share/devin'))
        def rule(operation, paths):
            return '(' + operation + ' ' + ' '.join('(subpath ' + json.dumps(str(p),ensure_ascii=False) + ')' for p in sorted(paths)) + ')'
        return '\n'.join(['(version 1)', '(allow default)', '(deny file-write*)',
            rule('allow file-write*', {self.directory, *self.writes}),
            '(allow file-write* (literal "/dev/null"))',
            rule('deny file-read*', private) if private else '',
            '(deny network*)' if self.policy.get('network_access') is False else ''])

    def request(self, method, args):
        self.guard()
        if method == 'fs/read_text_file':
            content = self.file(args['path']).read_text()
            if args.get('line') is not None or args.get('limit') is not None:
                start = max(0, int(args.get('line', 1)) - 1)
                lines = content.splitlines(keepends=True)
                content = ''.join(lines[start: start + int(args['limit'])] if args.get('limit') is not None else lines[start:])
            return {'content': content}
        if method == 'fs/write_text_file':
            with self.lock:
                if self.closed:
                    raise ValueError('Original client tool admission is closed')
                self.guard()
                path = self.file(args['path'], write=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                self.file(args['path'], write=True).write_text(args['content'])
            return {}
        if method == 'terminal/create':
            cwd = Path(args.get('cwd') or self.root).resolve(strict=True)
            if not cwd.is_relative_to(self.root):
                raise ValueError('Terminal cwd must remain in the original workspace')
            command, extra = args['command'], args.get('args', [])
            if not isinstance(command, str) or not isinstance(extra, list) or any(not isinstance(x, str) for x in extra):
                raise ValueError('Invalid ACP terminal argv')
            if args.get('env'):
                raise ValueError('Agent-supplied terminal environment overrides are not admitted')
            argv = [command, *extra] if extra else ['/bin/zsh', '-f', '-c', command]
            key = uuid.uuid4().hex
            output = self.directory / (key + '.log')
            with self.lock, output.open('wb') as stream:
                if self.closed:
                    raise ValueError('Original client tool admission is closed')
                self.guard()
                if self.lease:
                    self.lease.tool_intent(key)
                try:
                    environment=dict(self.env)
                    process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', self.sandbox(), *argv],
                        cwd=cwd, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True)
                except OSError:
                    if self.lease:
                        self.lease.tool_not_started(key)
                    raise
                if self.lease:
                    self.lease.tool_spawned(key, process)
                self.terminals[key] = (process, output, args.get('outputByteLimit', 120000))
            return {'terminalId': key}
        key = args.get('terminalId')
        if method.startswith('terminal/') and key in self.terminals:
            process, output, limit = self.terminals[key]
            if method == 'terminal/output':
                raw = output.read_bytes()
                status = self.exit_status(process) if process.poll() is not None else None
                return {'output': raw[:limit].decode(errors='replace'), 'truncated': len(raw) > limit,
                    **({'exitStatus': status} if status else {})}
            if method == 'terminal/wait_for_exit':
                process.wait()
                return self.exit_status(process)
            if method in ('terminal/kill', 'terminal/release'):
                self.kill(process)
                return {}
        raise ValueError('Unqualified ACP client method or terminal identity: ' + method)

    @staticmethod
    def exit_status(process):
        code = process.returncode
        return {'exitCode': code if code >= 0 else None, 'signal': str(-code) if code < 0 else None}

    @staticmethod
    def kill(process):
        # The live Popen handle is retained; never signal a cold/unrelated PID.
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)

    def close(self):
        with self.lock:
            self.closed = True
            processes = list(self.terminals.values())
        for process, _, _ in processes:
            self.kill(process)
