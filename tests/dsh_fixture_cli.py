#!/usr/bin/env python3
"""Launch the installed DSH CLI with a test-owned loopback model plugin.

The runtime configuration selects this executable only inside a marked temporary
fixture. Detached review jobs inherit the same fixture, without Python mocks or
changes to the installed application or production runtime configuration.
"""
import json
import os
from pathlib import Path
import sys
import uuid


def main(argv):
    state = json.loads(Path(os.environ['HTAM_NATIVE_FIXTURE_CONFIG']).read_bytes())
    root = Path(state['root']).resolve()
    if Path.cwd().resolve() != root or not (root / 'ISOLATED-NATIVE-COLLABORATION').is_file():
        raise ValueError('Installed native fixture requires its marked temporary workspace')
    index = argv.index('--patch') + 1
    entries = json.loads(Path(argv[index]).read_bytes())
    if state.get('broken_policy'):
        for row in entries:
            for plugin in row.get('insert', []):
                if plugin['id'] == 'htam-write-policy':
                    plugin['config']['moduleEntry'] = str(root / 'unavailable-native-module.js')
    config = {key: value for key, value in state.items()
              if key not in ('root', 'node', 'provider', 'broken_policy')}
    if state.get('wire'):
        entries.append({'id': 'llm-pi-ai', 'config': {'providers': {'htam-offline': {
            'api': 'openai-responses', 'transport': 'sse', 'baseURL': state['wire'],
            'apiKeyEnv': 'HTAM_OFFLINE_DSH_KEY', 'models': [{'id': state['wire_model'], 'reasoningEfforts': False}],
            'streamIdleTimeoutMs': state.get('wire_idle_timeout_ms', 300_000),
            'retryPolicy': {'mode': 'normal', 'maxRetries': state.get('wire_retries', 0),
                            'retryableCodes': ['TIMEOUT'],
                            'backoff': {'initialDelayMs': 10, 'maxDelayMs': 10,
                                        'jitterRatio': 0}}}}}})
    else:
        entries.append({'insert': [{'id': 'offline-model', 'name': state['provider'],
                                   'inject': ['llm', 'sessions'], 'config': config}]})
    control = next(plugin['config']['controlFd'] for row in entries
                   for plugin in row.get('insert', []) if plugin['id'] == 'htam-native-tool-profile')
    directory=root/'fixture-native-patches';directory.mkdir(exist_ok=True)
    patch=directory/(uuid.uuid4().hex+'.json')
    patch.write_text(json.dumps(entries))
    argv[index]=str(patch)
    # Retain one actual CLI process/PID/group and one signal delivery. The
    # fixture's existing temporary-root cleanup owns this patch file.
    os.set_inheritable(control,True)
    os.execve(state['node'],[state['node'],state['moduleEntry'],*argv],
              {**os.environ,'ELECTRON_RUN_AS_NODE':'1','HTAM_OFFLINE_DSH_KEY':'isolated-fixture'})


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
