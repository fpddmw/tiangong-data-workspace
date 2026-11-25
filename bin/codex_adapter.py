#!/usr/bin/env python3
"""Codex app-server adapter: one native thread and exact turn; transport
closure is not a stop receipt."""
import contextlib
import json
from pathlib import Path
import queue
import threading
import time

import workspace_files as entry
from codex_host import CodexHost, HostError, ISOLATED_FEATURES, rpc_pages, safe_diagnostic
from collaboration_tools import CONTROLLED_TOOLS, ControlledTools, editor_preflight
from harness_hosts import NativeStartRejected
from production_review import mcp_inventory

from agent_base import (
    CapabilityUnavailable, RuntimeError,
    CODEX_USAGE_FIELDS, configure_workspace_sandbox, digest, identity,
    native_cli_environment, native_duration, native_start_boundary,
    observed_usage, scope_workspace_writes,
)


class CodexAdapter:
    """One native thread and exact turn; transport closure is not a stop receipt."""

    requires_program_work_root = False
    tool_boundary = 'Codex native sandbox and no-delegation feature profile'
    read_operation = 'original_native_turn'
    stop_operation = 'original_live_handle_only; observer_notLoaded_never_confirms_stop'
    scope_writes = staticmethod(scope_workspace_writes)

    @staticmethod
    def configure(root, config):
        if not isinstance(config['provider'].get('codex_model_provider'), str):
            raise CapabilityUnavailable('Codex provider needs its existing native model-provider configuration identifier.')
        if config.get('profile') == 'read-only-evidence':
            config['native_policy'] = {'sandbox': 'read-only', 'network_access': False, 'writable_roots': []}
        elif config.get('profile') == 'native-workspace':
            configure_workspace_sandbox(root, config)

    @staticmethod
    def tool_capabilities(config):
        if config.get('profile') == 'native-workspace':
            supported = {'read_files', 'local_checks', 'native_cli'}
            policy = config['native_policy']
            if policy.get('sandbox') == 'workspace-write' and policy.get('writable_roots'):
                supported.add('write_files')
            return supported
        return {'read_files', 'local_checks'} if config.get('profile') == 'read-only-evidence' else set()

    def __init__(self, root, config, host=None):
        self.root, self.config = root, config
        self.cwd = config.get('native_cwd', str(root))
        self.done = threading.Event()
        self.native_identity = None
        self.result = None
        self.turn_usage = {}
        self.turn_events = queue.SimpleQueue()
        self.goal_observation = None
        self.native_model = None
        self.native_policy = config.get('native_policy', {})
        self._guard = lambda: None
        self.env = native_cli_environment(root, config)
        from local_check import available
        self.controlled = ControlledTools(root, self.cwd, env=self.env,
            write_roots=self.native_policy.get('writable_roots', []),
            check_available=available() and 'local_checks' in self.tool_capabilities(config),
            cli_available=config.get('role') == 'executor' and 'native_cli' in self.tool_capabilities(config),
            cli_network_access=self.native_policy.get('network_access') is True,
            execution_lease=config.get('_execution_lease'),
            preflight=self._editor_preflight)
        self.owned = host is None
        try:
            self.host = host or CodexHost(binary=config['host']['binary'], on_event=self.event, on_tool=self._tool_call,
                                         profile=('programmatic-worker' if config.get('programmatic') else 'native-single-writer'),
                                         environment=self.env,
                                         role_cwd=str(root) if config.get('programmatic') else None)
        except HostError as exc:
            if exc.detail.get('outcome') == 'not_started':
                raise NativeStartRejected(exc.detail['code']) from exc
            raise
        if host is not None:
            self.previous_event = getattr(host, 'on_event', None)
            host.on_event = self.event
            self.previous_tool = getattr(host, 'on_tool', None)
            if self.previous_tool is None:
                host.on_tool = self._tool_call

    @property
    def review_service(self):
        return self.controlled.review_service

    @review_service.setter
    def review_service(self, value):
        self.controlled.review_service = value

    def _editor_preflight(self):
        return editor_preflight(self.root, self.config, self.tool_capabilities(self.config))

    def _tool_call(self, params):
        """Answer one native item/tool/call request with the shared envelope."""
        name = params.get('tool') if isinstance(params, dict) else None
        args = params.get('arguments') if isinstance(params, dict) else None
        if name not in CONTROLLED_TOOLS:
            body = {'accepted': False, 'diagnostic': 'unqualified controlled tool'}
            return {'success': False,
                    'contentItems': [{'type': 'inputText', 'text': json.dumps(body, ensure_ascii=False)}]}
        text = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
        reply = self.controlled.dispatch(name, text, guard=self._guard)
        return {'success': True,
                'contentItems': [{'type': 'inputText', 'text': json.dumps(reply, ensure_ascii=False)}]}

    def event(self, message):
        previous = getattr(self, 'previous_event', None)
        if previous:
            previous(message)
        params = message.get('params', {})
        if not self.native_identity or params.get('threadId') != self.native_identity['id']:
            return
        method = message.get('method')
        item = params.get('item', {})
        if method == 'thread/tokenUsage/updated' and params.get('turnId'):
            total = observed_usage((params.get('tokenUsage') or {}).get('total'), CODEX_USAGE_FIELDS)
            if total is not None:
                self.turn_usage[params['turnId']] = total
        if method in ('turn/started', 'turn/completed'):
            self.turn_events.put((method, params['turn']))
        if method == 'turn/completed':
            turn = params['turn']
            if self.native_identity.get('turn_id') and turn['id'] != self.native_identity['turn_id']:
                return
            self.result = turn
            self.done.set()

    def _params(self):
        document_budget, instructions = self._instruction_entry()
        config = {'features': {name: False for name in (*ISOLATED_FEATURES, 'goals', 'code_mode_host')},
                  'web_search': 'disabled',
                  'project_doc_max_bytes': document_budget,
                  'mcp_servers': {s['name']: {'enabled': False} for s in mcp_inventory(self.host)}}
        config['features'].update(shell_tool=True, unified_exec=True, code_mode_host=True, view_image=True)
        if self.config.get('programmatic'):
            config['features'].update(skill_search=True, skip_host_skill_discovery=False,
                                      goals=self.config.get('role') == 'executor',hooks=True)
            if self.config.get('effort'):
                config['model_reasoning_effort'] = self.config['effort']
        params = {'cwd': self.cwd, 'model': self.config['model'],
                'modelProvider': self.config['provider']['codex_model_provider'],
                'approvalPolicy': 'never', 'sandbox': 'read-only', 'config': config,
                'baseInstructions': instructions}
        policy = self.native_policy
        if policy.get('sandbox') == 'workspace-write':
            # workspaceWrite always adds cwd, even when writableRoots is narrow.
            # An explicit profile keeps the actual workspace identity while only
            # opening the explicitly configured paths. Unsupported hosts must fail closed.
            profile = {'extends': ':read-only',
                       'filesystem': {path: 'write' for path in policy['writable_roots']},
                       'network': {'enabled': policy['network_access']}}
            name = 'harness-filesystem-' + digest(json.dumps(profile, sort_keys=True).encode())[:16]
            config['permissions'] = {name: profile}
            config['default_permissions'] = name
            params.pop('sandbox')
            params['permissions'] = name
        return params

    def _instruction_entry(self):
        """Keep the project entry whole; long guides stay explicit file reads."""
        root = Path(self.root).resolve()
        entry = root / 'AGENTS.md'
        programmatic = self.config.get('programmatic') and Path(self.cwd).resolve() == root
        budget = 32768 if programmatic else 0
        oversized = False
        if budget:
            try:
                oversized = entry.stat().st_size > budget
            except OSError:
                # A missing/unreadable entry must remain an explicit read failure,
                # rather than imply that a successfully loaded prefix was complete.
                budget = 0
            if oversized:
                budget = 0
        role = self.config.get('role')
        if role == 'executor':
            duty = ('Deliver the complete original work package in its existing Session and native Goal. '
                    'Choose methods, develop and check the work, obtain independent scientific review, '
                    'resolve its findings, and hand over the supported result and remaining responsibilities.')
            guide = root / 'roles/data.md'
        elif role == 'scientific_reviewer':
            duty = ('Assess the frozen candidate against its declared use in an independent read-only Session. '
                    'Return scientific evidence, applicable limits and actionable findings directly to the author.')
            guide = root / 'roles/reviewer.md'
        else:
            duty = 'Carry out the responsibility in the original task and read its complete source files.'
            guide = None
        reads = [entry, *([guide] if guide else []), root / 'workflows/business-flow/README.md']
        text = duty + '\nRead these files in full, then the original brief, contract and referenced evidence: '
        text += ', '.join(str(path) for path in reads) + '.'
        text += (' Task cards and list previews provide navigation; read the full conclusion and its conditions '
                 'before scientific, adoption or authority decisions. Use the existing public work commands; '
                 'reports can be returned at any stage while feasible work continues.')
        if oversized:
            text += (' The project entry exceeds the 32768-byte automatic document budget, so automatic '
                     'project-document loading is disabled for this invocation. Read the complete entry from '
                     'the path above; an automatically loaded prefix would not establish full instructions.')
        return budget, text

    def execute(self, text, observe, guard, *, previous=None, boundary=None, requested_session_id=None):
        boundary = boundary or contextlib.nullcontext
        self._guard = guard
        auth = self._editor_preflight()
        self.controlled.auth_available = auth is not None
        if auth is not None:
            from collaboration_tools import identity_note
            text += identity_note(auth)
        params = self._params()
        if not previous:
            tools = self.controlled.declarations()
            if tools:
                params['dynamicTools'] = tools
        if previous:
            # Resume only the exact previous thread after its original turn is
            # known terminal. No "continue most recent" or fork fallback.
            if previous.get('turn_id') or not self.config.get('previous_thread_only'):
                state = self.readresult(previous, recorded_turn=False)
                if state['native_status'] not in ('completed', 'failed', 'interrupted'):
                    raise RuntimeError('Original Codex turn is not confirmed terminal; use its original live host.')
            params.update(threadId=previous['id'], excludeTurns=True)
            with native_start_boundary(guard, boundary):
                response = self.host.call('thread/resume', params)
            if response['thread']['id'] != previous['id']:
                raise RuntimeError('Host resumed a different thread; no turn admitted.')
        else:
            with native_start_boundary(guard, boundary):
                params.update(ephemeral=False)
                if self.config.get('programmatic'):
                    params['allowProviderModelFallback'] = False
                response = self.host.call('thread/start', params)
        native = identity('codex', 'thread', response['thread']['id'], self.root)
        if response.get('cwd') != self.cwd or response['thread'].get('cwd', self.cwd) != self.cwd:
            raise RuntimeError('Native host working directory differs from the bound workspace.')
        if response.get('model') != self.config['model'] or response.get('modelProvider') != self.config['provider']['codex_model_provider']:
            raise RuntimeError('Native model/provider differs from the selected configuration; no silent fallback accepted.')
        self.native_identity = native
        self.native_model = response['model']
        if self.config.get('programmatic') and self.config.get('effort') and response.get('reasoningEffort') != self.config['effort']:
            raise CapabilityUnavailable('Native host did not confirm the selected reasoning effort; no model turn admitted.')
        observe(native, 'thread_ready', {'model': response['model'], 'modelProvider': response['modelProvider']})
        if params.get('permissions') and (response.get('activePermissionProfile') or {}).get('id') != params['permissions']:
            raise CapabilityUnavailable('Native host did not confirm the exact package permission profile; no model turn admitted.')
        if any(s.get('runtimeStatus') != 'disabled' for s in mcp_inventory(self.host, native['id'])):
            raise RuntimeError('MCP isolation is not confirmed; no model turn was started.')
        # The final pre-send admission follows the actual thread observation.
        turn_params = {'threadId': native['id'], 'input': [{'type': 'text', 'text': text}], 'model': self.config['model']}
        if self.config.get('effort'):
            turn_params['effort'] = self.config['effort']
        if params.get('permissions'):
            turn_params['permissions'] = params['permissions']
        else:
            policy = self.native_policy
            turn_params['sandboxPolicy'] = ({'type': 'readOnly', 'networkAccess': policy['network_access']}
                if policy['sandbox'] == 'read-only' else {'type': 'workspaceWrite', 'networkAccess': policy['network_access'],
                    'writableRoots': policy['writable_roots'], 'excludeSlashTmp': True, 'excludeTmpdirEnvVar': True})
        with native_start_boundary(guard, boundary, ready_thread=native):
            turn = self.host.call('turn/start', turn_params)['turn']
        self.native_identity = identity('codex', 'thread', native['id'], self.root, turn['id'])
        observe(self.native_identity, turn.get('status', 'inProgress'),
                {'model': response['model'], 'modelProvider': response['modelProvider']})
        if turn.get('status') in ('completed', 'failed', 'interrupted'):
            self.result = turn
            self.done.set()
        # The host owns automatic Goal turns. Observe its actual events and
        # preserve each turn in this invocation; never send synthetic resumes.
        completed = {turn['id']: turn} if turn.get('status') in ('completed', 'failed', 'interrupted') else {}
        while True:
            while not self.turn_events.empty():
                method, observed_turn = self.turn_events.get_nowait()
                if method == 'turn/completed':
                    completed[observed_turn['id']] = observed_turn
                elif observed_turn['id'] != self.native_identity['turn_id']:
                    previous_turn = completed.get(self.native_identity['turn_id'])
                    if not previous_turn or previous_turn.get('status') != 'completed':
                        raise RuntimeError('Native automatic turn has no observed completed predecessor.')
                    predecessor = self.native_identity['turn_id']
                    self.native_identity = identity('codex', 'thread', native['id'], self.root, observed_turn['id'])
                    self.done.clear()
                    observe(self.native_identity, observed_turn.get('status', 'inProgress'),
                            {'source': 'codex_turn_start', 'previous_turn': {'turn_id': predecessor, 'terminal': 'completed'}})
            terminal = completed.get(self.native_identity['turn_id'])
            if terminal:
                self.result = terminal
                if not self.config.get('programmatic') or terminal.get('status') != 'completed':
                    break
                goal = self.host.call('thread/goal/get', {'threadId': native['id']}).get('goal')
                self.goal_observation = {'source': 'codex_native_rpc', 'sessionId': native['id'],
                                         'turn': self.native_identity['turn_id'], 'goal': goal}
                if not goal or goal.get('status') != 'active':
                    if self.turn_events.empty():
                        break
                    continue
            disconnected = getattr(self.host, 'disconnected', None)
            if disconnected is not None and disconnected.is_set():
                raise HostError('Original turn transport disconnected; recover its native identity.')
            try:
                guard()
            except (ValueError, entry.ProductionError):
                stopped = self.stop(self.native_identity)
                return {**stopped, 'reason': 'explicit_stop_requested'}
            if terminal:
                time.sleep(0.1)
            else:
                self.done.wait(1.0)
        result = self.readresult(self.native_identity)
        if self.goal_observation is not None:
            result['native_goal_observation'] = self.goal_observation
        if auth is not None:
            result['editor_session_preflight'] = auth
        if self.controlled.identity_refreshes:
            result['editor_session_refreshes'] = self.controlled.identity_refreshes
        meter = result['metering']
        cumulative = meter.get('thread_total')
        baseline = self.config.get('prior_thread_usage') if previous else {}
        if cumulative and baseline is not None:
            meter['usage'] = {key: value - baseline.get(key, 0) for key, value in cumulative.items()
                              if (not previous or key in baseline) and value >= baseline.get(key, 0)} or None
        return result

    def readresult(self, native, *, boundary=None, recorded_turn=False):
        def call(method, params):
            with (boundary or contextlib.nullcontext)():
                return self.host.call(method, params)
        thread = call('thread/read', {'threadId': native['id'], 'includeTurns': False})['thread']
        if thread.get('id') != native['id'] or thread.get('cwd') != self.cwd:
            raise RuntimeError('Native read does not match original identity and working directory.')
        if not native.get('turn_id'):
            return {'native_identity': native, 'native_status': 'unknown', 'result': None,
                    'reason': 'No original turn ID was observed; inspect original thread, never guess a new turn.'}
        params = {'threadId': native['id'], 'limit': 100, 'sortDirection': 'desc', 'itemsView': 'notLoaded'}
        turn = next((row for page in rpc_pages(call, 'thread/turns/list', params) for row in page
                     if row.get('id') == native['turn_id']), None)
        if not turn:
            return {'native_identity': native, 'native_status': 'unknown', 'result': None,
                    'reason': 'Original turn not found in its native thread.'}

        from workspace_queries.discovery import observed_commands, observed_failures
        params = {'threadId': native['id'], 'turnId': native['turn_id'], 'limit': 100, 'sortDirection': 'asc'}
        final, latest_message = None, None
        commands, failures = [], []
        for page in rpc_pages(call, 'thread/items/list', params):
            items = [row.get('item', row) for row in page]
            commands.extend(observed_commands(self.root, items))
            failures.extend(observed_failures(items))
            for item in items:
                if item.get('type') == 'agentMessage':
                    latest_message = item
                    if item.get('phase') in ('final', 'final_answer'):
                        final = item
        final = final or latest_message
        error = turn.get('error')
        failure = safe_diagnostic(error.get('message') or error.get('codexErrorInfo')) if isinstance(error, dict) else None
        goal_observation = None
        if self.config.get('programmatic'):
            try:
                goal_observation = {'source': 'codex_native_rpc', 'sessionId': native['id'],
                                    'turn': native['turn_id'],
                                    'goal': call('thread/goal/get', {'threadId': native['id']}).get('goal')}
            except (HostError, RuntimeError, ValueError, KeyError) as exc:
                goal_observation = {'source': 'codex_native_rpc', 'sessionId': native['id'],
                                    'turn': native['turn_id'], 'observation_issue': safe_diagnostic(str(exc))}
        return {'native_identity': native, 'native_status': turn.get('status', 'unknown'),
                'result': final.get('text', '') if final and turn.get('status') == 'completed' else None,
                'native_result_item_id': final.get('id') if final else None,
                'tool_observations': commands, 'tool_failures': failures,
                'source': 'native_rpc', 'stop_confirmed': turn.get('status') == 'interrupted',
                'native_context': {'cwd': self.cwd},
                **({'native_failure': failure} if failure else {}),
                **({'native_goal_observation': goal_observation} if goal_observation else {}),
                'observed_model': self.native_model,
                'metering': {'native_duration_ms': native_duration(turn.get('durationMs')),
                             'thread_total': self.turn_usage.get(native['turn_id']),
                             'usage': None, 'cost_usd': None}}

    def stop(self, native, *, boundary=None):
        # A read connector cannot stop a turn owned by another app-server.
        boundary = boundary or contextlib.nullcontext
        with boundary():
            thread = self.host.call('thread/read', {'threadId': native['id'], 'includeTurns': False})['thread']
        if thread.get('status', {}).get('type') == 'notLoaded' or not native.get('turn_id'):
            return {'native_identity': native, 'native_status': 'unknown', 'stop_confirmed': False,
                    'reason': 'Original live host/turn not available; observer notLoaded is not quiescence.'}
        if self.config.get('programmatic'):
            with boundary():
                goal = self.host.call('thread/goal/get', {'threadId': native['id']}).get('goal')
                if goal and goal.get('status') == 'active':
                    self.host.call('thread/goal/set', {'threadId': native['id'], 'status': 'paused', 'origin': 'user'})
        terminal = self.readresult(native, boundary=boundary)
        if terminal['native_status'] in ('completed', 'failed', 'interrupted'):
            return {**terminal, 'native_terminal': True}
        with boundary():
            self.host.call('turn/interrupt', {'threadId': native['id'], 'turnId': native['turn_id']})
        if self.done.wait(30):
            result = self.readresult(native, boundary=boundary)
            result['stop_confirmed'] = result['native_status'] in ('interrupted', 'completed', 'failed')
            return result
        return {'native_identity': native, 'native_status': 'unknown', 'stop_confirmed': False, 'reason': 'Interrupt requested; terminal confirmation unavailable.'}

    def close(self):
        if self.owned:
            self.host.close()
        elif hasattr(self, 'previous_event'):
            self.host.on_event = self.previous_event
            self.host.on_tool = self.previous_tool
