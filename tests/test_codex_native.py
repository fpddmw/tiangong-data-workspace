"""Installed Codex host with a loopback model; no paid model or production data."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import test_agent_runtime as fixtures
from task_ledger import sessions
import harness_tasks as tasks
from codex_scripted_provider import ScriptedProvider
from codex_adapter import CodexAdapter


@unittest.skipUnless(shutil.which('codex'), 'Requires the installed Codex app-server')
class CodexNativeTests(unittest.TestCase):
    def native_home(self, root, provider):
        home = root / 'codex-home'; home.mkdir()
        (home / 'config.toml').write_text(
            'model_provider="offline-provider"\n'
            '[model_providers.offline-provider]\nname="Loopback fixture"\n'
            f'base_url={json.dumps(provider.url)}\nwire_api="responses"\n'
            'requires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n')
        return home

    def test_old_thread_without_dynamic_tools_keeps_original_identity(self):
        def respond(request):
            self.assertNotIn('local_check', [t.get('name') for t in request['tools']])
            return ScriptedProvider.message('Use the public workspace CLI for review.')
        with tempfile.TemporaryDirectory() as temp, ScriptedProvider(respond) as provider:
            root = Path(temp).resolve(); home = self.native_home(root, provider)
            config = fixtures.configuration(); config['hosts']['codex']['binary'] = shutil.which('codex')
            path = root / 'runtime.json'; path.write_text(json.dumps(config))
            selected = fixtures.runtime.load_config(root, str(path), 'executor')
            previous = None
            with patch.dict(os.environ, CODEX_HOME=str(home)):
                for turn in range(2):
                    adapter = CodexAdapter(root, selected)
                    if turn == 0:
                        adapter.controlled.check_available = False
                    try:
                        result = adapter.execute('Continue with the public CLI.', lambda *a: None, lambda: None, previous=previous)
                        self.assertEqual(result['native_status'], 'completed', result)
                        if previous:
                            self.assertEqual(result['native_identity']['id'], previous['id'])
                        previous = result['native_identity']
                    finally:
                        adapter.close()
            self.assertEqual(len(provider.requests), 2)
            self.assertFalse(provider.errors, provider.errors)

    def test_controlled_tool_survives_original_thread_resume(self):
        calls = []
        def respond(request):
            tools = request.get('tools', [])
            names = [t.get('name') for t in tools]
            self.assertIn('local_check', names)
            outputs = [x for x in request['input'] if x.get('type') == 'function_call_output']
            if len(outputs) < len(calls) + 1:
                return ScriptedProvider.tool('local_check', {'argv': ['/usr/bin/printf', 'native-check']})
            calls.append(outputs[-1])
            return ScriptedProvider.message('Native controlled check complete.')

        with tempfile.TemporaryDirectory() as temp, ScriptedProvider(respond) as provider:
            root = Path(temp).resolve()
            home = self.native_home(root, provider)
            path = root / 'runtime.json'
            config = fixtures.configuration()
            config['hosts']['codex']['binary'] = shutil.which('codex')
            path.write_text(json.dumps(config))
            selected = fixtures.runtime.load_config(root, str(path), 'executor')
            previous = None
            with patch.dict(os.environ, CODEX_HOME=str(home)):
                for turn in range(2):
                    adapter = CodexAdapter(root, selected)
                    try:
                        deadline = time.monotonic() + 30
                        def guard():
                            if time.monotonic() > deadline:
                                raise TimeoutError('Loopback native turn timed out')
                        result = adapter.execute('Run the local check.', lambda *a: None, guard, previous=previous)
                        self.assertEqual(result['native_status'], 'completed', result)
                        self.assertIn('Native controlled check complete.', result['result'])
                        if previous:
                            self.assertEqual(result['native_identity']['id'], previous['id'])
                            self.assertNotEqual(result['native_identity']['turn_id'], previous['turn_id'])
                        previous = result['native_identity']
                    finally:
                        adapter.close()
            self.assertFalse(provider.errors, provider.errors)
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertIn('native-check', str(call))
                self.assertIn('accepted', str(call))

    def test_resumed_author_requests_review_revises_and_lead_adopts(self):
        f = fixtures.RuntimeTests('runTest'); f.setUp()
        self.addCleanup(f.doCleanups)
        candidate = f.home / 'measurement-session.json'
        candidate.write_text('{"basis":"wet","value":8}')
        phase = ['seed']
        author_step = [0]
        def payload(item):
            body = item['output']
            if isinstance(body, list):
                body = ''.join(x.get('text', '') for x in body)
            return json.loads(body)
        def respond(request):
            if request['model'] == 'exact-scientific_reviewer':
                self.assertNotIn('request_review', [t.get('name') for t in request['tools']])
                return ScriptedProvider.message('The wet value needs conversion to the dry basis before adoption.')
            if phase[0] == 'seed':
                return ScriptedProvider.message('Original candidate retained for continuation.')
            step = author_step[0]; author_step[0] += 1
            outputs = [x for x in request['input'] if x.get('type') == 'function_call_output']
            if step == 0:
                return ScriptedProvider.tool('request_review', {'artifacts': [str(candidate)], 'question': 'Check the mass basis.'})
            response = payload(outputs[-1])
            self.assertTrue(response['accepted'], response)
            if step == 1:
                return ScriptedProvider.tool('review_result', {'review_id': response['diagnostic']['review_id'],'wait_seconds':10})
            if step == 2:
                self.assertEqual(response['diagnostic']['state'], 'received', response)
                self.assertIn('dry basis', str(response))
                return ScriptedProvider.tool('local_check', {'argv': [fixtures.sys.executable, '-c',
                    'from pathlib import Path; Path(' + repr(str(candidate)) + ').write_text(\'{"basis":"dry","value":4}\')']})
            self.assertEqual(step, 3)
            self.assertEqual(response['diagnostic']['exit_code'], 0, response)
            return ScriptedProvider.message('Revised to dry basis; evidence and independent opinion retained for the lead.')

        with ScriptedProvider(respond) as provider:
            home = self.native_home(f.root, provider)
            config = fixtures.configuration(); config['programmatic'] = True
            config['hosts']['codex']['binary'] = shutil.which('codex')
            config['roles']['executor'].update(profile='native-workspace', native_policy={
                'sandbox': 'workspace-write', 'network_access': False, 'writable_roots': [str(f.home)]})
            f.config_path.write_text(json.dumps(config))
            with patch.dict(os.environ, CODEX_HOME=str(home)):
                first = sessions.dispatch(f.root, f.actor, 'request', execute=True, config=str(f.config_path))
                self.assertEqual(first['outcome'], 'succeeded', first)
                original = tasks.read_run(f.root, first['run_id'])
                phase[0] = 'resume'
                note = f.home / 'continue.md'; note.write_text('Complete the original local package: obtain review, revise and deliver.')
                final = sessions.dispatch(f.root, f.actor, 'request', execute=True, config=str(f.config_path),
                    instructions=str(note), run_id=original['id'])
                self.assertEqual(final['outcome'], 'succeeded', final)
                author = tasks.read_run(f.root, final['run_id'])
                self.assertEqual(author['native_identity']['id'], original['native_identity']['id'])
                snapshot = tasks.read_task(f.root, 'task')
                reviewer, = [r for r in snapshot['runs'] if r['request_id'] != 'request']
                self.assertEqual(reviewer['outcome'], 'succeeded')
                self.assertNotEqual(author['native_identity']['id'], reviewer['native_identity']['id'])
                self.assertEqual(json.loads(candidate.read_text()), {'basis': 'dry', 'value': 4})
                packet = json.loads(Path(tasks.read_request(f.root, reviewer['request_id'])['input_ref']['path']).read_text())
                self.assertEqual(json.loads(Path(packet['artifact_refs'][0]['path']).read_text()), {'basis': 'wet', 'value': 8})
                adopted = f.home / 'adoption.md'; adopted.write_text('Adopt the revised dry-basis fixture for the original local purpose.')
                result = fixtures.subprocess.run([fixtures.sys.executable, str(Path(tasks.__file__).with_name('harness.py')),
                    '--root', str(f.root), 'work-dispose', '--return-id', author['return_ids'][0], '--use', 'adopt',
                    '--decision', str(adopted)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(tasks.read_task(f.root, 'task')['runs']), 3)
            self.assertFalse(provider.errors, provider.errors)


if __name__ == '__main__':
    unittest.main()
