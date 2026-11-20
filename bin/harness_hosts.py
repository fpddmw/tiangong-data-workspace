"""Identity and observation shapes for the built-in managed adapters."""

from dataclasses import dataclass


@dataclass(frozen=True)
class NativeHost:
    host: str
    kind: str
    adapter: str
    source: str
    route: str
    implementation: tuple[str, str]
    managed_entry: str | None = None
    shell_env: tuple[str, str, str] | None = None
    capabilities: frozenset[str] = frozenset()


MANAGEMENT_HOST = NativeHost('codex', 'thread', 'codex-app-server', 'native_rpc', 'S',
                             ('codex_adapter', 'CodexAdapter'), capabilities=frozenset({'author_review', 'native_goal'}))
HOSTS = {item.host: item for item in (
    MANAGEMENT_HOST,
    NativeHost('claude_code', 'session', 'claude-code-print', 'native_cli_json', 'D',
               ('claude_adapter', 'ClaudeAdapter')),
    NativeHost('deepseek_harness', 'session', 'dsh-headless', 'native_cli_json', 'DSH',
               ('dsh_headless', 'DeepSeekAdapter'), managed_entry='headless', shell_env=('DSH_SESSION_ID', 'DSH_SHELL', '1'),
               capabilities=frozenset({'author_review', 'native_goal', 'interrupted_resume', 'tracked_processes'})),
    NativeHost('devin_cli', 'session', 'devin-acp', 'native_rpc', 'DEVIN',
               ('devin_adapter', 'DevinAdapter'),
               capabilities=frozenset({'author_review', 'interrupted_resume', 'tracked_processes'})),
)}
ADAPTERS = {item.adapter: item for item in HOSTS.values()}
ADAPTER_HOSTS = {adapter: item.host for adapter, item in ADAPTERS.items()}
ROUTE_ADAPTERS = {item.route: item.adapter for item in HOSTS.values()}


def supports(host, capability):
    selected = HOSTS.get(host)
    return selected is not None and capability in selected.capabilities


def is_observed_actor(host, kind):
    selected = HOSTS.get(host)
    return selected is not None and selected.kind == kind


def observed_shell_identity(environ):
    hints = []
    for item in HOSTS.values():
        if item.shell_env is not None:
            identity_key, marker_key, marker = item.shell_env
            if (identity := environ.get(identity_key)) and environ.get(marker_key) == marker:
                hints.append((item.host, item.kind, identity))
    # A shell hint can only help locate a matching retained observation.
    return hints[0] if len(hints) == 1 else None


def confirmed_not_started(observation, *, host, run_id, request_id, config_sha256, native_identity=None):
    if not isinstance(observation, dict) or host not in HOSTS:
        return False
    expected = {'schema': 'data-workspace.agent-runtime-observation.v1', 'source': 'adapter_exception',
                'host': host, 'run_id': run_id, 'request_id': request_id, 'config_sha256': config_sha256,
                'native_status': 'not_started', 'outcome': 'failed'}
    facts = observation.get('observation')
    if (not all(observation.get(key) == value for key, value in expected.items())
            or not isinstance(facts, dict) or facts.get('native_submission') != 'not_started'):
        return False
    if native_identity is None:
        return not observation.get('native_identity') and 'phase' not in facts
    ready_kind,phase=('thread','before_turn') if host=='codex' else ('session','before_prompt')
    return (host in ('codex','devin_cli') and native_identity.get('host') == host
            and native_identity.get('kind') == ready_kind and 'turn_id' not in native_identity
            and observation.get('native_identity') == native_identity and facts.get('phase') == phase)


class NativeStartRejected(Exception):
    """The adapter can establish that the original invocation never opened."""

    def __init__(self, category, exit_code=None, *, ready_thread=None, phase='before_turn'):
        super().__init__('Native invocation did not start: ' + category)
        self.observation = {'native_submission': 'not_started', 'category': category, 'exit_code': exit_code}
        self.ready_thread = ready_thread
        if ready_thread:
            self.observation['phase'] = phase


def installed_program(root):
    from pathlib import Path
    import json
    import hashlib
    path = Path(root) / '.control/runtime.json'
    if not path.is_file():
        return None
    binding = json.loads(path.read_bytes()).get('programmatic_worker')
    if binding is None:
        return None
    selected = Path(root) / binding['path']
    return {'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()}
