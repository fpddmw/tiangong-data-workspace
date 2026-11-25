"""Devin's local ACP host; exact model and persistent Session, no synthetic Goal."""
import contextlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading

from agent_base import (CapabilityUnavailable, StopRequested, configure_workspace_sandbox,
    identity, native_cli_environment, native_start_boundary, scope_workspace_writes)
from collaboration_tools import ControlledTools, editor_preflight, identity_note
from collaboration_mcp import Bridge
from devin_acp import ACPHost, ACPError, ClientTools
from execution_lease import atomic_json, tools_observation, execution_observation
from harness_hosts import NativeStartRejected
from safe_diagnostics import safe_value


STANDARD_SWE2 = ('swe-2-high', 'swe-2-medium', 'swe-2-max')


def option_values(items):
    for item in items:
        if 'group' in item:
            yield from option_values(item.get('options', []))
        else:
            yield item.get('value')


class DevinAdapter:
    requires_program_work_root = True
    tool_boundary = 'ACP client file permissions and macOS sandboxed terminals; shared collaboration MCP transport'
    read_operation = 'retained_exact_acp_prompt_receipt; unresolved_prompt_remains_unknown'
    stop_operation = 'original_live_acp_session_cancel_and_tracked_tool_processes'
    scope_writes = staticmethod(scope_workspace_writes)

    @staticmethod
    def configure(root, config):
        if not config.get('programmatic'):
            raise CapabilityUnavailable('Devin uses the existing programmatic work-dispatch entry')
        if config['provider'].get('selection') != 'existing-host-configuration':
            raise CapabilityUnavailable('Authenticate through the official Devin CLI; no provider/key provisioning')
        if config['model'] not in STANDARD_SWE2:
            raise CapabilityUnavailable('Select an exact, verified SWE-2 standard model ID; no fuzzy, Flash or paid fallback')
        if config.get('profile') == 'native-workspace':
            configure_workspace_sandbox(root, config)
        else:
            config['native_policy'] = {'sandbox': 'read-only', 'network_access': False, 'writable_roots': []}
        if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
            raise CapabilityUnavailable('This Devin client tool boundary is qualified for macOS only')

    @staticmethod
    def tool_capabilities(config):
        tools = {'read_files', 'local_checks'}
        if config.get('profile') == 'native-workspace':
            if config['native_policy'].get('writable_roots'):
                tools.add('write_files')
            if config['native_policy'].get('network_access') is True:
                tools.add('native_cli')
        return tools

    def __init__(self, root, config):
        self.root, self.config = Path(root).resolve(), config
        self.cwd = self.root
        self.host, self.process, self.native = None, None, None
        self.cancelled, self.done = threading.Event(), threading.Event()
        self.response, self._guard = None, lambda: None
        self.prompt_submitted=False
        self.pre_prompt_client_requests=[]
        self.verified_model=None
        self.verified_model_configuration=None
        self.messages, self.tool_calls, self.failures = [], {}, []
        self.env = native_cli_environment(self.root, config)
        for key in ('DEVIN_MODEL', 'DEVIN_REFUSAL_FALLBACK', 'DEVIN_PERMISSION_MODE', 'DEVIN_SANDBOX'):
            self.env.pop(key, None)
        self.env['DEVIN_PERMISSION_MODE'] = 'normal'
        if config['host'].get('tool_path'):
            self.env['PATH'] = os.pathsep.join([*config['host']['tool_path'], self.env.get('PATH', '')])
        self.directory = Path(config.get('_native_run_directory') or
            Path(config.get('diagnostic_directory', self.root / '.control/devin-observer/diagnostics')).parent)
        self.controlled = ControlledTools(self.root, self.cwd, env=self.env,
            write_roots=config['native_policy'].get('writable_roots', []), check_available=True,
            execution_lease=config.get('_execution_lease'),
            cli_available=config.get('role') == 'executor' and 'native_cli' in self.tool_capabilities(config),
            cli_network_access=config['native_policy'].get('network_access') is True,
            preflight=lambda: editor_preflight(self.root, config, self.tool_capabilities(config)))
        self.bridge, self.client, self.socket_home = None, None, None

    @property
    def review_service(self):
        return self.controlled.review_service

    @review_service.setter
    def review_service(self, value):
        self.controlled.review_service = value

    def _start(self, *, executing=False):
        self.directory.mkdir(parents=True, exist_ok=True)
        config_path = self.directory / 'devin-config.json'
        # This invocation-specific user config cannot change Desktop/other projects.
        # Search uses the sandboxed terminal; native search cannot bypass client FS.
        private = [str(self.root / name) for name in ('.env', '.control', '.secrets', '.codex', '.claude', '.devin')]
        native_config = {'agent': {'model': self.config['model'], 'subagent_model': self.config['model']},
            'subagents_enabled': False, 'auto_update': False, 'permission_mode': 'normal',
            'permissions': {'deny': ['grep', 'glob', 'fetch', 'run_subagent', 'read_subagent',
                *['Read(' + path + ')' for path in private]], 'allow': [], 'ask': []},
            'read_config_from': {key: key == 'agents_standard' for key in
                ('agents_standard', 'cursor', 'windsurf', 'claude', 'copilot', 'codex', 'kiro', 'opencode', 'zed')}}
        if executing:
            self.env.update(HTAM_DEVIN_ROOT=str(self.root), HTAM_DEVIN_REQUEST=self.config['task_request_id'],
                            HTAM_DEVIN_RUN=self.config['task_run_id'])
            hook = shlex.join([sys.executable, str(Path(__file__).with_name('devin_context_hook.py'))])
            native_config['hooks'] = {event: [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 2}]}]
                for event in ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PostCompaction')}
        atomic_json(config_path, native_config)
        command = [self.config['host']['binary'], '--config', str(config_path),
            '--permission-mode', 'normal', 'acp', '--model', self.config['model']]
        try:
            self.host = ACPHost(command, cwd=self.cwd, env=self.env, on_event=self.event,
                on_request=self.request, lease=self.config.get('_execution_lease') if executing else None,
                transcript=self.directory / ('acp.jsonl' if executing else 'acp-observer.jsonl'))
        except OSError as exc:
            raise NativeStartRejected(type(exc).__name__) from exc
        self.process = self.host.process
        if executing and (capacity := self.config.get('_devin_capacity')):
            capacity.launched = True
        init = self.host.call('initialize', {'protocolVersion': 1,
            'clientCapabilities': {'fs': {'readTextFile': True, 'writeTextFile': True}, 'terminal': True},
            'clientInfo': {'name': 'tiangong-harness', 'version': '1'}})
        if init.get('protocolVersion') != 1 or init.get('agentCapabilities', {}).get('loadSession') is not True:
            raise CapabilityUnavailable('Installed Devin must advertise ACP v1 and loadSession')
        self.initialization = init

    def event(self, message):
        params = message.get('params', {})
        if not self.native or params.get('sessionId') != self.native['id']:
            return
        update = params.get('update', {})
        kind = update.get('sessionUpdate')
        if kind == 'agent_message_chunk' and update.get('content', {}).get('type') == 'text':
            self.messages.append(update['content']['text'])
        if kind == 'tool_call':
            self.tool_calls[update['toolCallId']] = update
        if kind == 'tool_call_update' and update.get('status') == 'failed':
            self.failures.append({'tool': self.tool_calls.get(update['toolCallId'], {}).get('title'),
                'call_id': update['toolCallId'], 'source': 'devin_acp_tool_call_update',
                'error': safe_value(update.get('content'))})
        if kind == 'config_option_update':
            if self.prompt_submitted:self._verify_model(update, required=False)

    def request(self, method, params):
        if not self.prompt_submitted:self.pre_prompt_client_requests.append(method)
        if not self.native or params.get('sessionId') != self.native['id']:
            raise ValueError('ACP tool request must belong to the observed original Session')
        if method == 'session/request_permission':
            self._tool_guard()
            call = params.get('toolCall', {})
            actual = self.tool_calls.get(call.get('toolCallId'), {})
            kind = actual.get('kind')
            tool = actual.get('_meta', {}).get('cognition.ai/inferenceToolName', '')
            shared = tool in {'mcp__htam__' + name for name in self.controlled.offered()}
            allowed = shared or kind in ('read', 'execute') or (kind == 'edit' and self.config['native_policy']['sandbox'] == 'workspace-write')
            # Tool permissions never enlarge file/OS grants. Only this single call
            # is selected; no persistent trust or global bypass is written.
            option = next((item for item in params.get('options', [])
                           if item['kind'] == ('allow_once' if allowed else 'reject_once')), None)
            return {'outcome': {'outcome': 'selected', 'optionId': option['optionId']}} if option else {'outcome': {'outcome': 'cancelled'}}
        if not self.client:
            raise ValueError('Observer has no admitted file or terminal tools')
        return self.client.request(method, params)

    def _tool_guard(self):
        if not self.prompt_submitted:
            raise CapabilityUnavailable('No session/prompt submitted; model tools are not admitted')
        if self.cancelled.is_set():
            raise StopRequested('Original Devin invocation cancelled; tool admission closed', self.config.get('task_run_id'))
        self._guard()

    def _verify_model(self, response, *, required=True):
        options = {item['id']: item for item in response.get('configOptions', [])}
        selected = {key: item.get('currentValue') for key, item in options.items()}
        model = selected.get('model')
        if model in STANDARD_SWE2:
            if 'speed' in options and selected['speed'] != 'standard':
                raise CapabilityUnavailable('Native Devin Speed is not standard: ' + str(selected['speed']))
            if 'thought_level' in options:
                if 'speed' not in options:
                    # Some ACP catalogs separate standard and Lightning in Model itself.
                    offered = set(option_values(options['model'].get('options', [])))
                    if model not in offered or not any(name + '-lightning' in offered for name in STANDARD_SWE2):
                        raise CapabilityUnavailable('Native Devin model catalog does not distinguish standard SWE-2 from Lightning')
                model = 'swe-2-' + str(selected['thought_level'])
        if model != self.config['model'] and (required or model is not None):
            raise CapabilityUnavailable('Native Devin model differs from exact requested SWE-2 ID: ' + str(model))
        if model is not None:self.verified_model_configuration=selected
        return model

    def _select_model(self,response,sid,guard,boundary):
        def select(config_id,value):
            nonlocal response
            option=next((o for o in response.get('configOptions',[]) if o.get('id')==config_id),None)
            if not option or value not in option_values(option.get('options',[])):
                raise CapabilityUnavailable('Requested standard SWE-2 configuration is not offered: '+config_id)
            if option.get('currentValue')==value:return
            with native_start_boundary(guard,boundary):
                response=self.host.call('session/set_config_option',{'sessionId':sid,'configId':config_id,'value':value})
        options={o['id']:o for o in response.get('configOptions',[])}
        current=options.get('model',{}).get('currentValue')
        if 'thought_level' in options:
            if current not in STANDARD_SWE2:select('model','swe-2-high')
            select('thought_level',self.config['model'].removeprefix('swe-2-'))
            if any(o.get('id')=='speed' for o in response.get('configOptions',[])):select('speed','standard')
        elif current!=self.config['model']:
            select('model',self.config['model'])
        self.verified_model=self._verify_model(response)
        return response

    def execute(self, text, observe, guard, *, previous=None, boundary=None, requested_session_id=None):
        try:
            return self._execute(text,observe,guard,previous=previous,boundary=boundary,requested_session_id=requested_session_id)
        except ACPError as exc:
            if (exc.observation['response_received'] and not self.prompt_submitted
                    and not self.pre_prompt_client_requests
                    and not getattr(self.host,'client_requests',[])):
                rejected=NativeStartRejected('acp_request_rejected_before_prompt',ready_thread=self.native,phase='before_prompt')
                rejected.observation.update(acp_request=exc.observation,prompt_submitted=False,
                    target_session=(previous or self.native or {}).get('id'),
                    transcript_path=str(self.directory/'acp.jsonl'))
                raise rejected from exc
            raise
        except CapabilityUnavailable as exc:
            if (self.native and not self.prompt_submitted and not self.pre_prompt_client_requests
                    and not getattr(self.host,'client_requests',[])):
                rejected=NativeStartRejected('native_configuration_unavailable_before_prompt',
                    ready_thread=self.native,phase='before_prompt')
                rejected.observation.update(target_session=self.native['id'],prompt_submitted=False,
                    detail=str(exc),transcript_path=str(self.directory/'acp.jsonl'))
                raise rejected from exc
            raise

    def _execute(self, text, observe, guard, *, previous=None, boundary=None, requested_session_id=None):
        self._guard = guard
        auth = self.controlled.preflight()
        self.controlled.auth_available = auth is not None
        if auth is not None:
            text += identity_note(auth)
        with native_start_boundary(guard, boundary):
            self._start(executing=True)
        self.client = ClientTools(self.root, self.config['native_policy'], env=self.env,
            directory=self.directory / 'tool-output', lease=self.config.get('_execution_lease'), guard=self._tool_guard)
        self.socket_home = tempfile.TemporaryDirectory(prefix='htam-devin-')
        self.bridge = Bridge(Path(self.socket_home.name) / 'tools.sock', self.controlled,
            guard=self._tool_guard, cancelled=self.cancelled.is_set)
        mcp = [{'name': 'htam', 'command': sys.executable,
            'args': [str(Path(__file__).with_name('collaboration_mcp.py')), str(self.bridge.path)], 'env': []}]
        params = {'cwd': str(self.cwd), 'mcpServers': mcp}
        if previous:
            if previous['host'] != 'devin_cli' or (requested_session_id and requested_session_id != previous['id']):
                raise ValueError('Load the exact Devin Session; cross-host identities cannot be resumed')
            params['sessionId'] = previous['id']
        with native_start_boundary(guard, boundary):
            response = self.host.call('session/load' if previous else 'session/new', params)
        sid = previous['id'] if previous else response.get('sessionId')
        self.native = identity('devin_cli', 'session', sid, self.cwd)
        observe(self.native, 'session_ready', {'source': 'devin_acp_session_load' if previous else 'devin_acp_session_new',
            'native_config': response, 'native_goal_api': 'not_available'})
        if response.get('_meta', {}).get('cognition.ai/isLocked') is True:
            raise CapabilityUnavailable('Original Devin Session has a native lock; observe its actual execution')
        self._select_model(response,sid,guard,boundary)
        self.host.call('session/set_mode', {'sessionId': sid, 'modeId': 'accept-edits'})
        text += ('\nDevin宿主能力：在本Session内持续读取原brief、按合同完成制作、自查、主动独审和返修，再交清成果与余项。'
            '本宿主没有已验证的Goal API；不调用get_goal/update_goal。一次调用结束不代表父任务完成。'
            '使用htam MCP的request_review/review_result/report_finding/refresh_editor_identity；共享服务保管原审阅。'
            '作者通过htam MCP tiangong_cli调用天工CLI，例如{role:"editor",surface:"cli",argv:["auth","whoami","--json"]}；'
            '参数不含程序路径或shell，文件使用绝对路径和CLI的--output；核exit_code及完整输出原件。普通terminal只承担文件/离线任务，不能读凭据或通过命令拼写提权。'
            '原生grep/glob已禁用，使用沙箱terminal搜索；原生read/edit通过客户端明确写域。不得新开子agent。')
        text += ('\n保存普通报告并在final给准确路径；运行器会自动保存原native输出并登记原run/return，供主审读取。'
                 '作者和独审不用执行work-return或修改.control等管理元数据。独审把完整意见直接写入final，临时计算可用local_check。')
        with native_start_boundary(guard, boundary):
            self.prompt_submitted=True
            handle = self.host.submit('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': text}]})
        while True:
            try:
                self.response = self.host.result(handle, timeout=.2)
                break
            except TimeoutError:
                try:
                    guard()
                except StopRequested:
                    self._cancel()
        reason = self.response.get('stopReason')
        confirmed = reason == 'cancelled'
        lease = self.config.get('_execution_lease')
        if confirmed and lease:
            confirmed = tools_observation(lease.value.get('tools', {}))['state'] == 'exited'
        result = {'native_identity': self.native, 'native_status': 'completed' if reason == 'end_turn' else
            'interrupted' if confirmed else 'unknown', 'source': 'native_rpc',
            'result': ''.join(self.messages), 'stop_confirmed': confirmed,
            'native_stop_reason': reason, 'native_prompt_response': self.response,
            'native_model': self.verified_model,'native_model_configuration':self.verified_model_configuration,
            'model_verification_source':'acp_session_config_options','native_goal_api': 'not_available',
            'tool_failures': self.failures, 'identity_refreshes': self.controlled.identity_refreshes,
            'metering': {'usage': self.response.get('usage'), 'usage_semantics': 'native_ACP_prompt_response', 'cost_usd': None},
            'transcript_path': str(self.directory / 'acp.jsonl')}
        atomic_json(self.directory / 'acp-result.json', result)
        self.done.set()
        return result

    def _cancel(self):
        if self.native and not self.cancelled.is_set():
            self.cancelled.set()
            self.host.send({'method': 'session/cancel', 'params': {'sessionId': self.native['id']}})
            if self.client:
                self.client.close()

    def readresult(self, native, *, boundary=None):
        path = self.directory / 'acp-result.json'
        if path.is_file():
            result = json.loads(path.read_bytes())
            if result['native_identity'] != native:
                raise ValueError('Retained ACP result differs from original Session binding')
            if result.get('native_stop_reason') == 'cancelled' and not result.get('stop_confirmed'):
                execution = execution_observation(self.directory / 'execution.json')
                if execution['state'] == 'exited':
                    result = {**result, 'native_status': 'interrupted', 'stop_confirmed': True,
                              'execution_observation': execution}
            return result
        return {'native_identity': native, 'native_status': 'unknown', 'source': 'native_rpc',
            'result': None, 'stop_confirmed': False, 'replay_allowed': False,
            'reason': 'No exact retained ACP prompt response; session history alone does not prove this invocation ended',
            'transcript_path': str(self.directory / 'acp.jsonl')}

    def stop(self, native, *, boundary=None):
        if self.host and self.native == native:
            with (boundary or contextlib.nullcontext)():
                self._cancel()
            self.done.wait(timeout=5)
            return self.readresult(native)
        return {**self.readresult(native), 'stop_confirmed': False,
            'reason': 'Stop intent retained; original manager must cancel its live ACP Session, never a replacement transport'}

    def close(self):
        if self.host:
            self.host.close()
        if self.client:
            self.client.close()
        if self.bridge:
            self.bridge.close()
        if self.socket_home:
            self.socket_home.cleanup()
