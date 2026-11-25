#!/usr/bin/env python3
"""Shared primitives for native host adapters and the runtime dispatcher.

Leaf layer: no bin-internal imports at module level. Ledger, package and
host-specific helpers stay lazy inside their single call site.
"""
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import workspace_files as entry

digest = entry.sha256

PARENT_IDENTITY_ENV = ('CODEX_THREAD_ID', 'CODEX_TURN_ID', 'CLAUDE_SESSION_ID',
                       'DSH_SESSION_ID', 'DSH_SHELL', 'DSH_PROFILE', 'DSH_PROFILE_DIR')


class RuntimeError(entry.ProductionError):
    pass


class StopRequested(RuntimeError):
    def __init__(self, message, run_id):
        super().__init__(message)
        self.run_id = run_id


class CapabilityUnavailable(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def child_environment():
    """A child must not authenticate management writes as its parent actor.

    Credential/provider/tool settings are otherwise inherited byte for byte.
    Actual child identity is supplied by its native runtime or observed receipt;
    this function never invents or exports a claimed replacement identity.
    """
    return {key: value for key, value in os.environ.items() if key not in PARENT_IDENTITY_ENV}


def native_cli_environment(root, config):
    from env_run import cli_state_home
    env = child_environment()
    from delivery_evidence import context_environment
    env.update(context_environment(root,config.get('_execution_lease')))
    if config.get('role') == 'executor' and config.get('profile') == 'native-workspace':
        env['XDG_STATE_HOME'] = str(cli_state_home(root))
    return env


CODEX_USAGE_FIELDS = {
    'inputTokens': 'input_tokens', 'cachedInputTokens': 'cached_input_tokens',
    'cacheWriteInputTokens': 'cache_write_input_tokens', 'outputTokens': 'output_tokens',
    'reasoningOutputTokens': 'reasoning_output_tokens', 'totalTokens': 'total_tokens',
}
CLAUDE_USAGE_FIELDS = {
    'input_tokens': 'input_tokens', 'cache_read_input_tokens': 'cached_input_tokens',
    'cache_creation_input_tokens': 'cache_write_input_tokens', 'output_tokens': 'output_tokens',
}


def observed_usage(values, fields):
    if not isinstance(values, dict):
        return None
    return {key: values[name] for name, key in fields.items()
            if type(values.get(name)) is int and values[name] >= 0} or None


def native_duration(value):
    return value if type(value) is int and value >= 0 else None


def native_model_name(value):
    if not isinstance(value, str):
        return None
    return value.casefold().removesuffix('[1m]')


def program_original_path(root, cwd, value, *, allow_directory=False):
    """Resolve a return against its actual working directory and original workspace."""
    from production_review import scientific_path
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Identify a saved original file path.')
    supplied = Path(value)
    if not supplied.is_absolute():
        relative = supplied
        supplied = cwd / relative
        if not supplied.exists() and not supplied.is_symlink():
            supplied = root / relative
    try:
        canonical = Path(os.path.realpath(supplied, strict=True))
    except FileNotFoundError as exc:
        raise ValueError('Identify the actual saved original path for ' + value) from exc
    return str(scientific_path(root, canonical, must_exist=not allow_directory))


def read_receipt(root, ref):
    path = entry.safe_path(root, ref['path'], must_exist=False)
    raw = path.read_bytes()
    if digest(raw) != ref['sha256']:
        raise RuntimeError('Original runtime receipt changed.')
    return json.loads(raw)


def identity(host, kind, native_id, root, turn_id=None):
    if not isinstance(native_id, str) or not native_id:
        raise RuntimeError('Native host did not return a session identity.')
    value = {'host': host, 'kind': kind, 'id': native_id, 'instance': native_id, 'cwd': str(root)}
    if turn_id:
        value['turn_id'] = turn_id
    return value


def same_actor(left, right):
    return all(left.get(k) == right.get(k) for k in ('host', 'kind', 'id'))


@contextlib.contextmanager
def native_start_boundary(guard, boundary, *, ready_thread=None):
    """Only rejection before the adapter sends or spawns the native action is certain."""
    from harness_hosts import NativeStartRejected
    with contextlib.ExitStack() as stack:
        try:
            guard()
            stack.enter_context((boundary or contextlib.nullcontext)())
        except (ValueError, RuntimeError) as exc:
            from safe_diagnostics import safe_text
            rejected = NativeStartRejected(getattr(exc, 'code', 'original_dispatch_guard_rejected'),
                                           ready_thread=ready_thread)
            rejected.observation['reason'] = safe_text(str(exc))
            raise rejected from exc
        yield


def configure_workspace_sandbox(root, config):
    policy = config['native_policy']
    if policy.get('sandbox') not in ('read-only', 'workspace-write') or type(policy.get('network_access')) is not bool:
        raise RuntimeError('Native worker needs explicit read-only/workspace-write and network_access policy.')
    roots = policy.get('writable_roots', [])
    if not isinstance(roots, list):
        raise RuntimeError('Native writable_roots must be explicit workspace paths.')
    config['native_policy'] = {**policy, 'writable_roots': [str(entry.safe_path(root, item, must_exist=False)) for item in roots]}


def scope_workspace_writes(root, config, *, original, work, receipt):
    """Use explicit filesystem permission, independently of the brief's location."""
    policy = config['native_policy']
    if policy['sandbox'] == 'read-only':
        return config
    selected = list(policy['writable_roots'])
    if config.get('role') == 'executor':
        from env_run import cli_write_directories
        selected.extend(map(str, cli_write_directories(root)))
    return {**config, 'native_policy': {**policy, 'writable_roots': sorted(set(selected))}}


def production_skill(root, name):
    directory = (root / '.agents' / 'skills').resolve(strict=True)
    path = (directory / name / 'SKILL.md').resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(directory):
        raise CapabilityUnavailable('Selected installed Skill resolves outside the original Skill directory.')
    return path
