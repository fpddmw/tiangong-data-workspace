#!/usr/bin/env python3
"""Controlled author tools shared by native host adapters.

Each adapter owns admission and transport. This module holds one dispatch so
the DSH control channel and Codex item/tool/call expose identical capabilities
with the same accepted/diagnostic envelope. No business decision runs here.
"""
import json
from pathlib import Path
import subprocess
import sys
import time

from agent_base import StopRequested, child_environment

# Shared declarations for the native delivery channels.
CONTROLLED_TOOLS = {
    'tiangong_cli': {
        'description': 'Execute the current public TianGong CLI through the original trusted role launcher. Supply role, surface and CLI arguments only; no executable, shell, environment or cwd. Use absolute paths and the CLI --output option for files. Receiving the tool is separate from CLI success: inspect exit_code, complete output files and original operation receipts. Existing account/data authorization and unknown attempts remain unchanged; never retry an uncertain write.',
        'inputSchema': {'type': 'object', 'properties': {
            'role': {'type': 'string', 'enum': ['editor', 'research']},
            'surface': {'type': 'string', 'enum': ['cli', 'ai']},
            'argv': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}},
            'required': ['role', 'surface', 'argv'], 'additionalProperties': False}},
    'local_check': {
        'description': 'Run a local calculation or offline CLI check with network access denied by the OS. Reviewer candidates remain read-only. Save temporary reports under the HTAM_CHECK_OUTPUT environment directory; the result returns that path. This grants no database or credential access.',
        'inputSchema': {'type': 'object',
                        'properties': {'argv': {'type': 'array', 'items': {'type': 'string'},
                                       'description': 'Executable followed by arguments; no implicit shell.'}},
                        'required': ['argv'], 'additionalProperties': False}},
    'request_review': {
        'description': 'Start independent review of local candidates and supporting files. The review service freezes the files you select. Continue independent work in this Session and Goal while review runs; read the opinion with review_result, judge it and revise as needed. This does not submit your final delivery or end your Goal.',
        'inputSchema': {'type': 'object',
                        'properties': {'artifacts': {'type': 'array', 'items': {'type': 'string'},
                                       'description': 'Paths to local candidates and relevant existing calculations, source evidence or prior dispositions.'},
                                       'question': {'type': 'string',
                                       'description': 'Intended use and review question. For a revision, explain what changed and which prior findings or dependencies it affects.'},
                                       'supersedes_review_id': {'type':'string',
                                       'description':'Optional exact unstarted review to replace for the same author and objects.'},
                                       'prior_review_id': {'type':'string',
                                       'description':'Optional exact returned independent review for a focused comparison with the prior opinion and frozen candidates.'}},
                        'required': ['artifacts', 'question'], 'additionalProperties': False}},
    'review_result': {
        'description': 'Read the independent opinion and current native observation. Default wait_seconds is 0; explicit waits are bounded to 600 seconds and cancellable. Continue independent work and retain the original review_id. Author completion does not cancel the review. Judge the opinion and revise; this does not decide adoption.',
        'inputSchema': {'type': 'object',
                        'properties': {'review_id': {'type': 'string'},
                                       'wait_seconds': {'type': 'number',
                                       'minimum': 0, 'maximum': 600,
                                       'description': 'Observation timeout, 0–600 seconds; default 0.'}},
                        'required': ['review_id'], 'additionalProperties': False}},
    'report_finding': {
        'description': 'Report a shared tool or runtime problem on the original task board. Save the observed error, affected action and original evidence in a file, then give its path. The host records your actual Session and task; continue independent work while maintenance handles the finding.',
        'inputSchema': {'type': 'object',
                        'properties': {'report': {'type': 'string',
                                       'description': 'Path to the saved original problem report.'},
                                       'finding_id': {'type': 'string',
                                       'description': 'Existing finding to append this observation to; omit for a new problem.'}},
                        'required': ['report'], 'additionalProperties': False}},
    'refresh_editor_identity': {
        'description': 'Ask the original trusted launcher to check and refresh its existing TianGong editor login through the official CLI. No account switching, credentials access or data writes. Use after expiry or a sandbox refresh error; continue local work if unavailable.',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
}

REVIEW_METHODS = {'request_review': 'request_review', 'review_result': 'result',
                  'report_finding': 'report_finding'}


def identity_note(receipt):
    state = receipt.get('identity_state', '身份已核' if receipt.get('status') == 'passed' else '需刷新')
    return ('\n编辑身份：' + state + '；检查时点：' + str(receipt.get('captured_at_utc', '未取得')) +
            '。身份有效期不代表任务授权期限。在线身份或沙箱刷新失败时用 refresh_editor_identity，'
            '由可信宿主调用原正式CLI；保留原数据attempt，继续本地工作。')


def _session_stat(root):
    # Only the trusted launcher reads its selected path. Never read token bytes.
    try:
        from env_run import read_env
        value = read_env(root / '.env').get('DATA_EDITOR_SESSION_FILE')
        if not value:
            return {'state': 'selection_unknown'}
        path = Path(value).expanduser()
        try:
            info = path.stat()
            return {'path': str(path), 'exists': True, 'mtime_ns': info.st_mtime_ns}
        except FileNotFoundError:
            return {'path': str(path), 'exists': False}
    except (ValueError, OSError):
        return {'state': 'selection_or_stat_unavailable'}


def editor_preflight(root, config, capabilities):
    # Refresh belongs to the trusted dispatcher, not the sandboxed worker.
    # The public CLI owns locking, atomic writes and expected identity checks.
    launch = root / 'bin/launch.py'
    if (config.get('role') != 'executor'
            or 'native_cli' not in capabilities
            or not launch.is_file() or not (root / '.env').is_file()):
        return None
    try:
        accounts = json.loads((root / '.control/accounts.json').read_text())
        command = [sys.executable, str(launch), '--role', 'editor', 'cli',
                                  'auth', 'identity-receipt', '--expected-project-ref', accounts['project_ref'],
                                  '--expected-user-id', accounts['editor']['user_id'], '--json']
        before = _session_stat(root)
        errors = []
        for delay in (0, 1, 3, 10):
            if delay:
                time.sleep(delay)
            try:
                checked = subprocess.run(command, cwd=root, env=child_environment(), capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                checked = subprocess.CompletedProcess(command,124,
                    json.dumps({'error':{'code':'IDENTITY_COMMAND_TIMEOUT','message':'Public identity read timed out'}}),'')
            try:
                body = json.loads(checked.stdout)
                error = body.get('error', {}) if isinstance(body, dict) else {}
            except ValueError:
                error = {}
            if not isinstance(error, dict):
                error = {}
            from safe_diagnostics import safe_value
            errors.append(safe_value({key:error[key] for key in ('code', 'category', 'message', 'details', 'cause') if key in error},omit_transport_payloads=True))
            text = json.dumps(errors[-1]).casefold()
            transient = any(v in text for v in ('econnreset', 'econnrefused', 'etimedout', 'timeout', 'network', '503', '502', 'service_unavailable'))
            if checked.returncode == 0 or not transient or any(v in text for v in ('invalid_grant', 'login-required', 'missing')):
                break
        if checked.returncode:
            # Native identity diagnostics are already safe projections. Keep
            # code/phase, never raw stderr or credential-bearing responses.
            try:
                body = json.loads(checked.stdout)
                error = body.get('error', {}) if isinstance(body, dict) else {}
            except ValueError:
                error = {}
            result = {'status': 'unavailable', 'phase': 'identity', 'exit_code': checked.returncode,
                    'code': error.get('code', 'IDENTITY_COMMAND_FAILED') if isinstance(error, dict) else 'IDENTITY_COMMAND_FAILED',
                    'details': {key: error['details'][key] for key in ('cause_code', 'status')
                                if isinstance(error, dict) and isinstance(error.get('details'), dict)
                                and isinstance(error['details'].get(key), (str, int))},
                    'safe_errors': errors, 'session_path_before': before, 'session_path_after': _session_stat(root)}
            result['identity_state'] = '服务不可达' if transient else '需刷新'
            if not transient or any(v in text for v in ('invalid_grant', 'session_failed', 'login-required', 'missing')):
                try:
                    status = subprocess.run([sys.executable, str(launch), '--role', 'editor', 'cli', 'auth', 'status', '--json'],
                        cwd=root, env=child_environment(), capture_output=True, text=True, timeout=30)
                    doc = json.loads(status.stdout)
                    result['public_status'] = safe_value(doc.get('data', {}),omit_transport_payloads=True) if isinstance(doc, dict) else {}
                except ValueError:
                    result['public_status'] = {'state': 'unreadable'}
                except (OSError,subprocess.TimeoutExpired) as exc:
                    result['public_status'] = {'state': 'unavailable','code':type(exc).__name__}
                status_text = json.dumps(result['public_status']).casefold()
                if any(s in status_text + text for s in ('login-required', 'invalid_grant', 'missing')):
                    result.update(identity_state='需登录', cause='unknown; absence/mtime alone does not establish removal cause',
                        recovery='bin/launch.py --role editor cli auth login (original account owner); retain original attempts and continue local work')
                elif 'eperm' in text or 'eacces' in text:
                    result.update(identity_state='需刷新', cause='sandbox session access; trusted refresh_editor_identity bridge required')
            return result
        receipt = json.loads(checked.stdout).get('data', {}).get('receipt', {})
        if (isinstance(receipt, dict) and checked.returncode == 0 and receipt.get('schema') == 'tiangong-lca.auth-identity-receipt.v1'
                and receipt.get('status') == 'passed' and receipt.get('assertions', {}).get('passed') is True
                and receipt.get('assertions', {}).get('mode') == 'intent-bound'):
            return safe_value({key: receipt[key] for key in ('schema', 'status', 'captured_at_utc', 'project',
                                                 'identity', 'session', 'assertions') if key in receipt},omit_transport_payloads=True) | {'identity_state':'身份已核'}
    except subprocess.TimeoutExpired:
        return {'status': 'unavailable', 'phase': 'identity', 'code': 'IDENTITY_COMMAND_TIMEOUT'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {'status': 'unavailable', 'phase': 'identity', 'code': type(exc).__name__}
    return {'status': 'unavailable', 'recovery': 'Retain the original failed operation for maintenance of bin/launch.py --role editor cli auth identity-receipt --json; local work may continue.'}


class ControlledTools:
    """Dispatch the shared controlled tools; every call is explicit and bounded."""

    def __init__(self, root, cwd, *, env=None, write_roots=(), check_available=False,
                 preflight=None, identity_refreshes=None, execution_lease=None, cli_available=False,
                 cli_network_access=False):
        self.root, self.cwd = root, cwd
        self.env = env or {}
        self.write_roots = write_roots
        self.check_available = check_available
        self.preflight = preflight or (lambda: None)
        self.review_service = None
        self.auth_available = False
        self.identity_refreshes = identity_refreshes if identity_refreshes is not None else []
        self.execution_lease = execution_lease
        self.cli_available = cli_available
        self.cli_network_access = cli_network_access

    def offered(self):
        kinds = []
        if self.cli_available:
            kinds.append('tiangong_cli')
        if self.check_available:
            kinds.append('local_check')
        if self.review_service:
            kinds.extend(REVIEW_METHODS)
        if self.auth_available:
            kinds.append('refresh_editor_identity')
        return kinds

    def declarations(self):
        return [{'type': 'function', 'name': kind, **CONTROLLED_TOOLS[kind]} for kind in self.offered()]

    def dispatch(self, kind, text=None, *, guard=lambda: None, cancelled=lambda: False):
        accepted, diagnostic = False, None
        if kind == 'tiangong_cli' and self.cli_available:
            from tiangong_cli_tool import execute
            try:
                diagnostic = execute(self.root, json.loads(text or '{}'), guard=guard, cancelled=cancelled,
                                     lease=self.execution_lease, writable_roots=self.write_roots,
                                     network_access=self.cli_network_access)
                accepted = True  # Native exit_code still decides CLI success.
            except StopRequested:
                raise
            except (ValueError, TypeError, OSError) as exc:
                from safe_diagnostics import safe_text
                diagnostic = {'error': safe_text(exc), 'scope': 'this CLI call only; no automatic retry'}
        elif kind == 'local_check' and self.check_available:
            from local_check import execute
            try:
                args = json.loads(text or '{}')
                diagnostic = execute(self.root, self.cwd, args.get('argv'), writable_roots=self.write_roots, guard=guard,
                    env={key: self.env[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in self.env},
                    **({'execution_lease': self.execution_lease} if self.execution_lease else {}))
                accepted = True  # Receiving a check is separate from its exit code.
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                diagnostic = {'error': str(exc), 'scope': 'this local check only'}
        elif kind == 'refresh_editor_identity' and self.auth_available:
            diagnostic = self.preflight()
            self.identity_refreshes.append(diagnostic)
            accepted = bool(diagnostic and diagnostic.get('status') == 'passed')
        elif kind in REVIEW_METHODS and self.review_service:
            try:
                args = json.loads(text or '{}')
                method = REVIEW_METHODS[kind]
                handler = getattr(self.review_service, method)
                diagnostic = (handler(args, cancelled=cancelled) if method == 'result' else handler(args))
                accepted = True
            except (ValueError, TypeError, KeyError, OSError) as exc:
                diagnostic = str(exc)
        return {'accepted': bool(accepted), 'diagnostic': diagnostic}
