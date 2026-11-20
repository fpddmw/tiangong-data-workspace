#!/usr/bin/env python3
"""Claude adapter: verified local --print/--resume flags; no invented agents
stop/get API."""
import contextlib
import json
import math
from pathlib import Path
import re
import subprocess
import uuid

import workspace_files as entry
from harness_hosts import NativeStartRejected
from safe_diagnostics import safe_text as safe_diagnostic

from agent_base import (
    CapabilityUnavailable, RuntimeError,
    CLAUDE_USAGE_FIELDS, child_environment, identity, native_duration,
    native_model_name, native_start_boundary, observed_usage,
)


class ClaudeAdapter:
    """Verified local --print/--resume flags; no invented agents stop/get API."""

    requires_program_work_root = False
    tool_boundary = 'Claude explicit native tool allow patterns/dontAsk; no independent filesystem/network sandbox'
    read_operation = 'retained_native_cli_receipt_only'
    stop_operation = 'original_foreground_process; process exit is not native session confirmation'

    @staticmethod
    def configure(root, config):
        if config['provider'].get('selection') != 'existing-host-configuration':
            raise CapabilityUnavailable('Claude provider selection must use existing host configuration; this adapter does not provision providers or credentials.')
        settings = config['host'].get('settings_file')
        if settings is not None and (not isinstance(settings, str) or not Path(settings).is_absolute()):
            raise RuntimeError('Claude settings_file must name an exact existing native settings path.')
        if 'backend_model' in config and (not isinstance(config['backend_model'], str) or not config['backend_model']):
            raise RuntimeError('Claude backend_model must name the exact model behind the selected native CLI alias.')
        profile = config.get('profile')
        if profile not in ('native-workspace', 'read-only-evidence'):
            return
        policy = config.get('native_policy', {})
        if not isinstance(policy, dict):
            raise CapabilityUnavailable('Claude tool permissions must be explicitly configured.')
        tools, allowed = policy.get('claude_tools'), policy.get('claude_allowed_tools')
        supported = {'Read', 'Glob', 'Grep'}
        if profile == 'native-workspace':
            supported.update(('Bash', 'Edit', 'Write'))
        if (not isinstance(tools, list) or not tools or not set(tools) <= supported
                or not isinstance(allowed, list) or not allowed
                or not all(isinstance(item, str) and item.split('(', 1)[0] in tools for item in allowed)):
            raise CapabilityUnavailable('Claude roles need explicit native tools and matching allowed-tool patterns.')
        if policy.get('filesystem_boundary') != 'host-native-permissions' or policy.get('network_boundary') != 'host-native-permissions':
            raise CapabilityUnavailable('Claude uses host-native filesystem and network permissions. Configure these boundaries explicitly.')

    @staticmethod
    def tool_capabilities(config):
        if config.get('profile') not in ('native-workspace', 'read-only-evidence'):
            return set()
        tools = {name.split('(', 1)[0] for name in config['native_policy']['claude_allowed_tools']}
        supported = {'read_files'} if 'Read' in tools else set()
        if 'Bash' in tools:
            supported.update(('local_checks', 'native_cli'))
        if (config.get('profile') == 'native-workspace' and tools & {'Write', 'Edit'}
                and (not config.get('programmatic')
                     or config['native_policy'].get('programmatic_write_boundary') == 'host-native-permissions')):
            supported.add('write_files')
        return supported

    @staticmethod
    def scope_writes(root, config, *, original, work, receipt):
        policy = config['native_policy']
        return config

    def __init__(self, root, config):
        self.root, self.config = root, config
        self.process = None
        self.native_identity = None

    def command(self, *, session_id, previous=False):
        tools, allowed = [], []
        if self.config.get('profile') in ('native-workspace', 'read-only-evidence'):
            policy = self.config['native_policy']
            tools, allowed = policy['claude_tools'], policy['claude_allowed_tools']
        native_tools = bool(allowed)
        stream = self.config.get('programmatic')
        command = [self.config['host']['binary'], '--print', '--output-format', 'stream-json' if stream else 'json',
                   '--model', self.config['model'], '--tools', ','.join(tools),
                   '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--permission-mode', 'dontAsk',
                   '--resume' if previous else '--session-id', session_id]
        if stream:
            command.append('--verbose')
        else:
            command += ['--safe-mode', '--disable-slash-commands']
        if settings := self.config['host'].get('settings_file'):
            if not Path(settings).is_file():
                raise CapabilityUnavailable('The selected existing Claude CLI settings file is unavailable.')
            command += ['--settings', settings, '--setting-sources', 'project' if stream else '']
        if native_tools:
            command += ['--allowedTools', ','.join(allowed),
                        '--append-system-prompt', 'Read AGENTS.md and the original files named in the task.']
        if self.config.get('effort'):
            command += ['--effort', self.config['effort']]
        if stream and native_tools and self.config.get('profile') == 'read-only-evidence':
            if additional := self.config.get('additional_read_roots'):
                command += ['--add-dir', *additional]
        return command

    def execute(self, text, observe, guard, *, previous=None, boundary=None, requested_session_id=None):
        boundary = boundary or contextlib.nullcontext
        session_id = previous['id'] if previous else requested_session_id or str(uuid.uuid4())
        # Resolve the existing host command before spawning its native session.
        try:
            command = self.command(session_id=session_id, previous=bool(previous))
        except CapabilityUnavailable as exc:
            raise NativeStartRejected('configured_native_settings_unavailable') from exc
        with native_start_boundary(guard, boundary):
            try:
                self.process = subprocess.Popen(command, cwd=self.config.get('native_cwd', self.root),
                                                env=child_environment(), stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except OSError as exc:
                raise NativeStartRejected(type(exc).__name__) from exc
        first_input = text
        while True:
            try:
                stdout, _stderr = self.process.communicate(first_input, timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                first_input = None
                try:
                    guard()
                except (ValueError, entry.ProductionError):
                    self.stop(identity('claude_code', 'session', session_id, self.root))
                    raise RuntimeError('Original Claude process was asked to stop after admission change; native outcome remains unknown for session ' + session_id)
        try:
            result, actual_model = self.native_result(stdout, session_id, bool(self.config.get('programmatic')))
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('Claude did not return a structured result for the exact requested session: ' + session_id) from exc
        self.native_identity = identity('claude_code', 'session', result['session_id'], self.root)
        cost = result.get('total_cost_usd')
        observed_cost = cost if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 else None
        observe(self.native_identity, 'failed' if result.get('is_error') or self.process.returncode else 'completed',
                {'session_id': result['session_id'], 'subtype': result.get('subtype'),
                 'requested_model': self.config['model'], 'actual_model': actual_model or 'not_reported_by_result',
                 'provider': 'existing_host_configuration_not_verified'})
        output = result.get('result')
        expected = self.config.get('backend_model', self.config['model'])
        observed_context = result.get('_programmatic_native', {})
        route_status = ('unobserved' if not actual_model else 'selected' if
                        native_model_name(actual_model) == native_model_name(expected) else 'different')
        issue = None
        if self.config.get('programmatic') and (reported_cwd := observed_context.get('cwd')):
            if self.config.get('native_cwd') and reported_cwd != self.config['native_cwd']:
                issue = 'Native Claude stream reported a different role working directory.'
        meter = observed_usage(result.get('usage'), CLAUDE_USAGE_FIELDS)
        thinking = ((result.get('usage') or {}).get('output_tokens_details') or {}).get('thinking_tokens')
        if meter and type(thinking) is int and thinking >= 0:
            meter['reasoning_output_tokens'] = thinking
        return {'native_identity': self.native_identity,
                'native_status': 'failed' if result.get('is_error') or self.process.returncode else 'completed',
                'result': output, 'source': 'native_cli_json', 'stop_confirmed': False,
                'requested_session_id': session_id, 'observed_model': actual_model, 'native_route_status': route_status,
                **({'native_context': observed_context} if observed_context else {}),
                **({'tool_failures': result['_programmatic_failures']} if result.get('_programmatic_failures') else {}),
                **({'delivery_issue': issue} if issue else {}),
                'metering': {'native_duration_ms': native_duration(result.get('duration_ms')),
                             'usage': meter, 'input_usage_includes_cache': False, 'cost_usd': observed_cost,
                             'cost_source': 'claude_cli_reported' if observed_cost is not None else None}}

    @staticmethod
    def native_result(output, session_id, stream):
        if stream:
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            results = [event for event in events if event['type'] == 'result']
            result = results[0] if len(results) == 1 else {}
            # modelUsage is keyed by the selected CLI alias. The assistant
            # event supplies the backend's reported model.
            assistants = [event['message'] for event in events if event['type'] == 'assistant'
                          and isinstance(event.get('message'), dict)]
            models = {item['model'] for item in assistants if isinstance(item.get('model'), str)}
            initial = next((event for event in events if event['type'] == 'system' and event.get('subtype') == 'init'), {})
            tools = [item.get('name') for msg in assistants for item in msg.get('content', [])
                     if item.get('type') == 'tool_use']
            replies = [item for event in events if event['type'] == 'user'
                       for item in event.get('message', {}).get('content', []) if item.get('type') == 'tool_result']
            result['_programmatic_failures'] = ClaudeAdapter.tool_failures(assistants, replies)
            result['_programmatic_native'] = {
                'cwd': initial.get('cwd'), 'model_alias': initial.get('model'),
                'discovered_skill_count': len(initial['skills']) if isinstance(initial.get('skills'), list) else None,
                'tools_called': tools, 'tool_result_count': len(replies),
                'tool_error_count': sum(item.get('is_error') is True for item in replies)}
        else:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError('Expected a native result object.')
            models = set((result.get('modelUsage') or {}).keys())
        if result.get('type') != 'result' or result.get('session_id') != session_id:
            raise ValueError('Unexpected result identity.')
        return result, next(iter(models)) if len(models) == 1 else None

    @staticmethod
    def tool_failures(assistants, replies):
        from harness_hook import digest, failure_component
        invoked = {item.get('id'): item for msg in assistants for item in msg.get('content', [])
                   if item.get('type') == 'tool_use'}
        failures = []
        for reply in replies:
            call_id = reply.get('tool_use_id')
            if reply.get('is_error') is not True or not isinstance(call_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', call_id):
                continue
            call = invoked.get(call_id, {})
            tool = call.get('name')
            tool = tool if isinstance(tool, str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,160}', tool) else 'unknown'
            raw_input = call.get('input')
            command = raw_input.get('command') if isinstance(raw_input, dict) else None
            component = failure_component(command) if tool == 'Bash' else None
            message = safe_diagnostic(reply.get('content', '')).casefold()
            structured = reply.get('error')
            code = structured.get('code') if isinstance(structured, dict) else reply.get('code')
            if reply.get('truncated') is True or reply.get('complete') is False:
                error = 'unknown_incomplete_evidence'
            elif isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.:-]+', code):
                error = code
            elif any(term in message for term in ('permission', 'not allowed', 'denied')):
                error = 'permission_denied'
            elif any(term in message for term in ('does not exist', 'file not found', 'no such file')):
                error = 'not_found'
            else:
                error = 'native_tool_error'
            failures.append({'native_item_id': call_id, 'tool_name': tool, 'kind': 'tool_error',
                             'error_class': error, 'input_sha256': digest(raw_input),
                             **({'component': component} if component else {})})
        return failures

    def readresult(self, native, *, boundary=None):
        raise CapabilityUnavailable('Claude print has no verified historical-result CLI. Read the retained original runtime receipt; missing output stays unknown.')

    def stop(self, native, *, boundary=None):
        if self.process is None:
            return {'native_identity': native, 'native_status': 'unknown', 'stop_confirmed': False,
                    'reason': 'No original foreground process handle. Installed claude agents help supplies no verified stop API.'}
        with (boundary or contextlib.nullcontext)():
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return {'native_identity': native, 'native_status': 'unknown', 'stop_confirmed': False, 'reason': 'Foreground termination requested; process exit unconfirmed.'}
        return {'native_identity': native, 'native_status': 'unknown', 'stop_confirmed': False,
                'process_exit_confirmed': True, 'reason': 'Foreground process exited; native session/child termination was not independently confirmed.'}

    def close(self):
        pass
