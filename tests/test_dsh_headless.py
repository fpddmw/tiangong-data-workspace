"""Offline DSH NDJSON through the original program and run store."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import Mock, patch

import test_agent_runtime as fixtures
import dsh_headless
import harness_entry
import harness_hook
import harness_sessions as sessions
import harness_tasks as tasks
import run_worker

runtime = fixtures.runtime
NATIVE_ID = 'opaque:dsh.session-77'


class DeepSeekProgramTests(unittest.TestCase):
    ref = fixtures.RuntimeTests.ref

    def create_request(self):
        full = self._testMethodName == 'test_same_family_dsh_reviewer_has_a_fresh_readonly_session'
        if self._testMethodName == 'test_read_profile_uses_native_tools_and_scoped_cli_guidance':
            self.input_path.write_text('# Synthetic fixture\n\n## Evidence\n[Original](authorization.md)\n')
        else:
            self.input_path.write_text(json.dumps({'data_support': not full, 'purpose': 'Synthetic source check',
                'consumer': 'Offline fixture', 'endpoint': 'Local proof only', 'evidence': [str(self.evidence['path'])]}))
        self.input_ref = self.ref(self.input_path)
        fixtures.RuntimeTests.create_request(self, finite=True, programmatic=True, requires_review=full)

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        roles = self.root / 'roles'
        roles.mkdir()
        originals = Path(__file__).resolve().parents[1] / 'roles'
        for name in ('data.md', 'data-support.md', 'scientific-production.md', 'reviewer.md'):
            shutil.copyfile(originals / name, roles / name)
        cli = self.home / 'native-headless.py'
        self.state = self.home / 'native-events.json'
        self.argv_log = self.home / 'launches.jsonl'
        self.candidate = run_worker.program_home(tasks.read_request(self.root, 'request'), root=self.root) / 'work/candidate.json'
        cli.write_text("""import json, os, sys, time
from pathlib import Path
argv = sys.argv[1:]
assert not any(os.environ.get(key) for key in ('CODEX_THREAD_ID', 'DSH_SESSION_ID', 'DSH_SHELL'))
if '--patch' in argv:
    patch = json.loads(Path(argv[argv.index('--patch') + 1]).read_bytes())
    policy = patch[-1]['insert'][0]['config']
    policy = {**policy, 'allow': [t for t in policy['allow'] if t not in ('get_goal', 'create_goal', 'update_goal', 'local_check', 'check_delivery', 'refresh_editor_identity', 'request_review', 'review_result', 'report_finding', 'tiangong_cli')]}
    if '--add-dir' in argv:
        assert argv[argv.index('--permission-mode') + 1] == 'workspace-write'
        work = next(argv[i+1] for i,a in enumerate(argv) if a=='--add-dir' and '/cli-state/' not in argv[i+1] and '/.local/state/tiangong-lca/' not in argv[i+1])
        assert Path(work).is_dir()
        assert policy['allow'] == ['read', 'write', 'edit']
    else:
        assert policy['allow'] in (['read', 'bash'], ['read'])
with Path(os.environ['HTAM_FIXTURE_DSH_ARGV']).open('a') as log:
    print(json.dumps({'cwd': str(Path.cwd()), 'argv': argv, 'policy': policy,
                      'input': sys.stdin.read()}), file=log)
value = json.loads(Path(os.environ['HTAM_FIXTURE_DSH_EVENTS']).read_bytes())
if value.get('stderr'):
    print(value['stderr'], file=sys.stderr, flush=True)
if policy['allow'] == ['read'] and 'reviewer' in value:
    value = value['reviewer']
if '--session-id' in argv and 'resumed' in value:
    value = value['resumed']
if value.get('write_artifact'):
    Path(os.environ['HTAM_FIXTURE_ARTIFACT']).write_text(value.get('artifact_body', '{"synthetic":8}'))
if value.get('checks'):
    opening = [e for e in value['events'] if e['type'] == 'session' or e.get('phase') == 'turn_start']
    for event in opening:
        print(json.dumps({**event, 'cwd': str(Path.cwd())} if event['type'] == 'session' else event), flush=True)
    value['events'] = [e for e in value['events'] if e not in opening]
    import socket
    control = socket.socket(fileno=patch[-1]['insert'][0]['config']['controlFd'])
    replies = control.makefile('r')
    review_id = None
    for number, check in enumerate(value['checks']):
        if check['kind'] == 'review_result':
            check['text'] = json.dumps({'review_id': review_id, 'wait_seconds': check.get('wait_seconds', 10)})
        print(json.dumps({'type':'htam_control','id':number,'kind':check['kind'],'text':check.get('text')}),flush=True)
        reply = json.loads(replies.readline())
        assert reply['accepted'] == check['accepted'], reply
        assert reply.get('diagnostic') is not None, reply
        if reply['accepted'] and check['kind'] == 'request_review':
            review_id = reply['diagnostic']['review_id']
        if check['kind'] == 'review_result':
            assert reply['diagnostic']['state'] == 'received', reply
            if expected := check.get('expected_opinion'):
                assert reply['diagnostic']['opinion']['text'] == expected, reply
                Path(check['received_path']).write_text(json.dumps(reply['diagnostic']))
if release := value.get('release_events'):
    Path(value['before_events_path']).touch()
    deadline = time.monotonic() + 60
    while not Path(release).exists():
        if time.monotonic() > deadline:
            raise TimeoutError('Isolated native events were not released')
        time.sleep(.03)
for event in value['events']:
    print(json.dumps({**event, 'cwd': str(Path.cwd())} if event['type'] == 'session' else event), flush=True)
raise SystemExit(value['exit'])
""")
        cli.chmod(0o700)
        env = patch.dict(os.environ, {'HTAM_FIXTURE_DSH_EVENTS': str(self.state),
                                      'HTAM_FIXTURE_DSH_ARGV': str(self.argv_log), 'HTAM_FIXTURE_ARTIFACT': str(self.candidate),
                                      'DSH_SESSION_ID': 'unrelated-parent', 'DSH_SHELL': '1'})
        env.start()
        self.addCleanup(env.stop)
        config = fixtures.configuration()
        config['programmatic'] = True
        config['hosts']['dsh'] = {'adapter': 'dsh-headless', 'binary': str(cli), 'node': sys.executable,
                                 'dsh_home': str(self.home), 'entry': 'headless', 'enabled': True}
        config['providers']['dsh'] = {'selection': 'existing-host-configuration', 'dsh_provider': 'configured-dsh'}
        config['roles']['executor'] = {'host': 'dsh', 'provider': 'dsh', 'model': 'deepseek-fixture',
                                        'profile': 'read-only-evidence', 'effort': 'max'}
        self.config_path.write_text(json.dumps(config))
        (self.root/'.control/runtime.json').write_text(json.dumps({'programmatic_worker':{'path':str(self.config_path)}}))
        query = patch.object(dsh_headless.DeepSeekAdapter, '_query', side_effect=lambda native: {
            'source': 'dsh_session_query', 'terminal': 'completed',
            'route': {'provider': 'configured-dsh', 'model': 'deepseek-fixture', 'reasoningEffort': 'max'}})
        query.start()
        self.addCleanup(query.stop)

    def _dispatch(self, *, explicit=True, instructions=None, run_id=None):
        return sessions.dispatch(self.root, self.actor, 'request', execute=True,
                                 config=str(self.config_path) if explicit else None, instructions=str(instructions) if instructions else None, run_id=run_id)


    def test_identity_refresh_uses_original_parent_without_an_extra_author(self):
        self._stream(final='Original retained delivery.')
        value = json.loads(self.state.read_bytes())
        value['checks'] = [{'kind': 'refresh_editor_identity', 'accepted': True}]
        self.state.write_text(json.dumps(value))
        with patch.object(dsh_headless.DeepSeekAdapter, '_editor_preflight', return_value={'status': 'passed'}) as identity:
            result = self._dispatch()
        self.assertEqual(result['outcome'], 'succeeded', result)
        self.assertEqual(identity.call_count, 2)
        receipt = runtime.read_receipt(self.root, tasks.read_run(self.root, result['run_id'])['latest_receipt'])
        self.assertEqual(receipt['result']['editor_session_refreshes'], [{'status': 'passed'}])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_finding_tool_records_native_author_without_host_identity_environment(self):
        report=self.home/'shared-error.md'
        report.write_text('Original public tool failure; no management envelope.')
        value=self._stream(final='Continue independent work after reporting the shared failure.')
        value['checks']=[{'kind':'report_finding','text':json.dumps({'report':str(report)}),'accepted':True},
                         {'kind':'report_finding','text':json.dumps({'report':str(self.root/'.env')}),'accepted':False}]
        self.state.write_text(json.dumps(value))
        result=self._dispatch()
        self.assertEqual(result['outcome'],'succeeded',result)
        findings=[q for q in tasks.read_task(self.root,'task')['requests'] if q['kind']=='finding']
        self.assertEqual(len(findings),1)
        self.assertEqual(findings[0]['sender_actor']['id'],NATIVE_ID)
        self.assertEqual(findings[0]['sender_actor']['host'],'deepseek_harness')
        self.assertEqual(findings[0]['input_ref'],self.ref(report))
        self.assertEqual(len(self.argv_log.read_text().splitlines()),1)

    def test_desktop_launcher_dispatches_without_node_prefix(self):
        config = json.loads(self.config_path.read_bytes())
        host = config['hosts']['dsh']
        host.update(launch_mode='desktop-cli', module_entry='/installed/app.asar/dsh/cli.js',
                    tool_path=[str(self.home)])
        cli = Path(host['binary'])
        cli.write_text('#!' + sys.executable + '\n' + cli.read_text())
        self.config_path.write_text(json.dumps(config))
        self._stream(final={'contribution': 'Desktop launcher returned.', 'complete': True})
        result = self._dispatch()
        self.assertEqual(result['outcome'], 'succeeded', result)
        selected = runtime.load_config(self.root, str(self.config_path), 'executor')
        adapter = dsh_headless.DeepSeekAdapter(self.root, selected)
        self.assertEqual(adapter.env['ELECTRON_RUN_AS_NODE'], '1')
        self.assertEqual(adapter.env['PATH'].split(os.pathsep)[0], str(self.home))
        selected['host'] = {**selected['host'], 'tool_path': ['/different/runtime']}
        updated = dsh_headless.DeepSeekAdapter(self.root, selected)
        self.assertEqual(updated.env['PATH'].split(os.pathsep)[0], '/different/runtime')

    def test_desktop_requires_module_entry_and_absolute_tool_paths(self):
        config = json.loads(self.config_path.read_bytes())
        config['hosts']['dsh']['launch_mode'] = 'desktop-cli'
        self.config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.CapabilityUnavailable, 'module entry'):
            runtime.load_config(self.root, str(self.config_path), 'executor')

        config['hosts']['dsh'].update(module_entry='/installed/app.asar/dsh/cli.js', tool_path=['relative'])
        self.config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.CapabilityUnavailable, 'absolute runtime'):
            runtime.load_config(self.root, str(self.config_path), 'executor')

    def test_readresult_requires_persisted_abort_for_stop_confirmation(self):
        config = runtime.load_config(self.root, str(self.config_path), 'executor')
        adapter = dsh_headless.DeepSeekAdapter(self.root, config)
        native = {'id': NATIVE_ID, 'turn_id': '1'}
        for source, terminal, expected in [
                ('dsh_session_query', 'interrupted', 'unknown'),
                ('dsh_persisted_session_log', None, 'unknown'),
                ('dsh_persisted_session_log', 'aborted', 'interrupted')]:
            with patch.object(adapter, '_query', return_value={'source': source, 'terminal': terminal, 'latest_turn': 1}):
                self.assertEqual(adapter.readresult(native)['native_status'], expected)

    def test_editor_refresh_is_parent_owned_and_never_runs_for_readonly_reviewer(self):
        (self.root / 'bin').mkdir(exist_ok=True)
        (self.root / 'bin/launch.py').write_text('# fixture public entry')
        (self.root / '.env').write_text('# fixture; no secrets')
        (self.root / '.control').mkdir(exist_ok=True)
        (self.root / '.control/accounts.json').write_text(json.dumps({'project_ref':'fixture','editor':{'user_id':'fixture-user'}}))
        config = runtime.load_config(self.root, str(self.config_path), 'executor')
        adapter = dsh_headless.DeepSeekAdapter(self.root, config)
        receipt = {'schema': 'tiangong-lca.auth-identity-receipt.v1', 'status': 'passed',
                   'assertions': {'passed': True, 'mode':'intent-bound'}, 'session': {'expires_at_utc': 'fixture-expiry'},
                   'untrusted_extra': 'must-not-copy'}
        with patch.object(dsh_headless.subprocess, 'run', return_value=Mock(
                returncode=0, stdout=json.dumps({'data':{'receipt':receipt}}))) as invoked:
            observed = adapter._editor_preflight()
            self.assertEqual(observed['session']['expires_at_utc'], 'fixture-expiry')
            self.assertNotIn('untrusted_extra', observed)
            self.assertIn('identity-receipt', invoked.call_args.args[0])
            for changes in ({'role': 'scientific_reviewer'}, {'native_policy': {'network_access': False}}):
                with self.subTest(changes=changes), patch.dict(adapter.config, changes):
                    self.assertIsNone(adapter._editor_preflight())
                    self.assertNotIn('refresh_editor_identity', adapter._patch()[-1]['insert'][0]['config']['allow'])
            self.assertEqual(invoked.call_count, 1)

    def test_same_family_dsh_reviewer_has_a_fresh_readonly_session(self):
        config = json.loads(self.config_path.read_bytes())
        config['roles']['executor'].update(profile='native-workspace', native_policy={
            'sandbox': 'workspace-write', 'network_access': False, 'writable_roots': [str(self.home)]})
        config['roles']['scientific_reviewer'] = {**config['roles']['executor'],
            'profile': 'read-only-evidence', 'native_policy': {'network_access': False}}
        self.config_path.write_text(json.dumps(config))
        first = self._stream(final={'complete': True, 'contribution': 'Fixed candidate.',
                                    'artifacts': [str(self.candidate)]})
        first['write_artifact'] = True
        first['events'][-1]['text'] = 'The candidate and source checks are ready for independent review.'
        first['checks'] = [
            {'kind': 'request_review', 'text': json.dumps({'artifacts': ['work/missing.json'],
             'question': 'Check the saved candidate against its source.'}), 'accepted': False},
            {'kind': 'request_review', 'text': json.dumps({'artifacts': [str(self.candidate)],
             'question': 'Check the saved candidate against its source.'}), 'accepted': True},
            {'kind': 'review_result', 'accepted': True}]
        reviewer = self._stream(final='The denominator is wrong. Recompute using dry mass.')
        reviewer['events'] = [e for e in reviewer['events'] if e['type'] not in
                              ('tool_call', 'htam_shell_result', 'tool_result')]
        for event in reviewer['events']:
            if 'sessionId' in event:
                event['sessionId'] = 'independent-dsh-reviewer'
        final = self._stream(turn=5, final={'contribution': 'Reviewed candidate delivered.', 'complete': True})
        self.state.write_text(json.dumps({**first, 'reviewer': reviewer, 'resumed': final}))
        delivery = self._dispatch()
        self.assertEqual(delivery['outcome'], 'succeeded', delivery)
        author, review = [json.loads(line) for line in self.argv_log.read_text().splitlines()]
        self.assertNotIn('--session-id', review['argv'])
        self.assertEqual(review['cwd'], author['cwd'])
        self.assertEqual(Path(review['cwd']), self.root)
        self.assertEqual(review['policy']['allow'], ['read'])
        self.assertIn(str(self.root / 'roles/reviewer.md'), review['input'])
        self.assertNotIn('check_delivery', review['input'])
        self.assertNotIn('"required": ["severity", "location", "reason", "required_change"]', review['input'])
        self.assertNotIn('never prose descriptions', review['input'])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2)
        runs = tasks.read_task(self.root, 'task')['runs']
        checked = next(r for r in runs if r['request_id'] != 'request')
        self.assertEqual(checked['native_identity']['id'], 'independent-dsh-reviewer')
        self.assertEqual(checked['outcome'], 'succeeded')
        self.assertTrue(checked['return_ids'])
        original = next(r for r in runs if r['request_id'] == 'request')
        retained = runtime.read_receipt(self.root, original['latest_receipt'])['result']
        self.assertEqual(retained['result'], first['events'][-1]['text'])
        self.assertEqual(retained['collected_review_return_ids'], checked['return_ids'])

    def _stream(self, *, turn=4, final=None, exit_code=0, before_open=None, not_started=False):
        if before_open is None:
            events = [{'type': 'session', 'sessionId': NATIVE_ID},
                      {'type': 'status', 'phase': 'turn_start', 'turn': turn},
                      {'type': 'status', 'phase': 'step_start', 'turn': turn, 'step': 0},
                      {'type': 'tool_call', 'tool': 'bash', 'callId': 'shell-1|opaque'},
                      {'type': 'htam_shell_result', 'callId': 'shell-1|opaque', 'exitCode': 7},
                      {'type': 'tool_result', 'callId': 'shell-1|opaque', 'status': 'completed'},
                      {'type': 'status', 'phase': 'step_end', 'turn': turn, 'step': 0,
                       'usage': {'inputTokens': 5, 'outputTokens': 3}},
                      {'type': 'status', 'phase': 'turn_end', 'turn': turn, 'reason': {'kind': 'completed'}}]
            if final is not None:
                events.append({'type': 'final', 'sessionId': NATIVE_ID,
                               'reason': {'kind': 'completed'}, 'text': final if isinstance(final, str) else json.dumps(final)})
        else:
            events = [{'type': 'error', 'message': before_open, **({'native_status':'not_started'} if not_started else {})}]
        value = {'events': events, 'exit': exit_code}
        self.state.write_text(json.dumps(value))
        return value

    def test_goal_continuation_accepts_multiple_turns_and_sums_usage(self):
        value = self._stream(final={'contribution': 'Goal finished.', 'complete': True})
        events = value['events']
        more = [dict(e, turn=5) if 'turn' in e else e for e in events[1:-1]]
        value['events'] = events[:-1] + more + events[-1:]
        self.state.write_text(json.dumps(value))
        result = self._dispatch()
        run = tasks.read_run(self.root, result['run_id'])
        self.assertEqual(run['native_identity']['turn_id'], '5')
        receipt = runtime.read_receipt(self.root, run['latest_receipt'])['result']
        self.assertEqual(receipt['native_status'], 'completed')
        self.assertEqual(receipt['metering']['usage']['input_tokens'], 10)

    def test_goal_tools_belong_to_author_only(self):
        c = runtime.load_config(self.root, str(self.config_path), 'executor')
        def allowed(config):
            return dsh_headless.DeepSeekAdapter(self.root, config)._patch()[-1]['insert'][0]['config']['allow']
        self.assertIn('create_goal', allowed(c))
        self.assertNotIn('create_goal', allowed({**c, 'role': 'scientific_reviewer'}))
        self.assertNotIn('check_delivery', allowed(c))

    def test_normal_dispatch_binds_session_reads_original_facts_and_resumes(self):
        self._stream(final='Exact original result. Scientific work may continue.')
        original_boundary = tasks.dispatch_admission
        admissions = []
        @contextmanager
        def boundary(*args, **kwargs):
            with original_boundary(*args, **kwargs):
                admissions.append(kwargs['run_id'])
                yield
        with patch.object(tasks, 'dispatch_admission', boundary):
            first = self._dispatch()
        self.assertEqual(first['outcome'], 'succeeded')
        self.assertEqual(admissions, [first['run_id']])
        initial = tasks.read_run(self.root, first['run_id'])
        native = initial['native_identity']
        self.assertEqual((initial['target_host'], native['id'], native['turn_id']), ('deepseek_harness', NATIVE_ID, '4'))
        result = runtime.read_receipt(self.root, initial['latest_receipt'])['result']
        self.assertEqual(result['metering']['usage']['input_tokens'], 5)
        self.assertEqual(result['tool_failures'][0]['exit_code'], 7)
        self.assertEqual(result['tool_failures'][0]['kind'], 'nonzero_exit')
        observations = list(harness_hook.receipt_home(self.root, {key: value for key, value in native.items() if key != 'turn_id'}).glob('*.json'))
        self.assertEqual(len(observations), 1)
        self.assertEqual(json.loads(observations[0].read_bytes())['failure']['exit_code'], 7)
        with patch.dict(os.environ, {'DSH_SHELL': '1', 'DSH_SESSION_ID': NATIVE_ID}):
            del os.environ['CODEX_THREAD_ID']
            view = harness_entry.current(self.root, request_id='request')['work']
            self.assertEqual([r['id'] for r in view['runs']], [initial['id']])
            self.assertEqual(tasks.verified_actor(self.root)['id'], NATIVE_ID)
        note = self.home / 'continue.md'
        note.write_text('Continue the remaining original comparison.')
        self._stream(turn=5, final='Completed remaining comparison.')
        final = self._dispatch(explicit=False, instructions=note)
        second = tasks.read_run(self.root, final['run_id'])
        self.assertEqual((second['native_identity']['id'], second['native_identity']['turn_id']), (NATIVE_ID, '5'))
        lines = [json.loads(line) for line in self.argv_log.read_text().splitlines()]
        self.assertNotIn('--session-id', lines[0]['argv'])
        self.assertEqual(lines[1]['argv'][lines[1]['argv'].index('--session-id') + 1], NATIVE_ID)
        self.assertEqual(lines[0]['cwd'], lines[1]['cwd'])
        recovered = run_worker.programmatic(self.root, 'request', read_result=True)
        self.assertEqual(recovered['receipt'], second['latest_receipt'])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2)

    def test_missing_native_final_remains_unknown_and_is_not_replayed(self):
        self._stream()
        first = self._dispatch()
        second = self._dispatch(explicit=False)
        run = tasks.read_run(self.root, first['run_id'])
        self.assertEqual(run['outcome'], 'unknown')
        self.assertEqual(run['native_identity']['id'], NATIVE_ID)
        self.assertEqual(second['run_id'], run['id'])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_explicit_continuation_retains_unknown_attempt_and_original_session(self):
        self._stream(final='Original report.')
        first = self._dispatch()
        note = self.home / 'continue.md'
        note.write_text('Continue this same Session; inspect unknown effects before any data retry.')
        self._stream(before_open='fixture transport exited before an opening was observed', exit_code=1)
        failed = self._dispatch(instructions=note)
        original = tasks.read_run(self.root, failed['run_id'])
        self.assertEqual(original['outcome'], 'unknown')
        self.assertIsNone(original['native_identity'])
        self.assertEqual(self._dispatch(explicit=False)['run_id'], original['id'])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2)
        self._stream(turn=5, final='Continued original work after inspecting the retained attempt.')
        result = self._dispatch(explicit=False, instructions=note)
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(tasks.read_run(self.root, result['run_id'])['native_identity']['id'], NATIVE_ID)
        self.assertEqual(tasks.read_run(self.root, original['id']), original)
        self._dispatch(explicit=False, instructions=note,run_id=original['id'])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 3)
        # Identical instructions can continue a later selected attempt.
        self._stream(turn=6, final='Original work complete.')
        self.assertEqual(self._dispatch(explicit=False, instructions=note)['outcome'], 'succeeded')
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 4)

    def test_preopening_error_retains_bounded_stderr_and_native_message(self):
        self._stream(before_open='native startup fixture failed', exit_code=1)
        value = json.loads(self.state.read_text())
        value['stderr'] = 'x' * 100000 + '\nnative startup diagnostic'
        self.state.write_text(json.dumps(value))
        result = self._dispatch()
        receipt = runtime.read_receipt(self.root, tasks.read_run(self.root,result['run_id'])['latest_receipt'])
        self.assertIn('native startup fixture failed', receipt['detail'])
        diagnostics=receipt['observation']['stderr']
        self.assertTrue(all(item['diagnostic_available'] for item in diagnostics))
        text=''.join(Path(item['diagnostic_ref']['path']).read_text() for item in diagnostics)
        self.assertIn('native startup diagnostic',text)
        self.assertEqual(receipt['observation']['exit_code'], 1)

    def test_host_identity_failure_keeps_safe_native_code(self):
        adapter = object.__new__(dsh_headless.DeepSeekAdapter)
        adapter.root = self.root
        adapter.config = {'role': 'executor', 'profile': 'native-workspace', 'native_policy': {'network_access': True}}
        (self.root / 'bin').mkdir(exist_ok=True)
        (self.root / 'bin/launch.py').touch()
        (self.root / '.env').touch()
        (self.root / '.control').mkdir(exist_ok=True)
        (self.root / '.control/accounts.json').write_text(json.dumps({'project_ref':'fixture','editor':{'user_id':'fixture-user'}}))
        for body, expected in [
            ({'error': {'code':'AUTH_IDENTITY_REMOTE_REQUEST_FAILED', 'details': {'cause_code':'ECONNRESET', 'status':503, 'token':'private'}}}, 'AUTH_IDENTITY_REMOTE_REQUEST_FAILED'),
            ([], 'IDENTITY_COMMAND_FAILED'),
            ('raw private failure', 'IDENTITY_COMMAND_FAILED'),
        ]:
            with patch.object(dsh_headless.subprocess, 'run', return_value=Mock(returncode=1, stdout=json.dumps(body))), patch('collaboration_tools.time.sleep'):
                result = adapter._editor_preflight()
            self.assertEqual(result['code'], expected)
            self.assertEqual(result['phase'], 'identity')
            self.assertNotIn('private', json.dumps(result))

    def test_unknown_first_launch_cannot_create_a_replacement_session(self):
        self._stream(before_open='unobserved creation', exit_code=1)
        original = self._dispatch()
        note = self.home / 'continue.md'; note.write_text('Continue the original conversation.')
        with self.assertRaisesRegex(tasks.WorkConflict, 'no observed Session'):
            self._dispatch(instructions=note)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)
        self.assertEqual(tasks.read_run(self.root, original['run_id'])['outcome'], 'unknown')

    def test_markdown_final_is_saved_without_projection_or_model_reexpression(self):
        text = '# Completed\nThe original results and limitations are retained.\n'
        self._stream(final=text)
        completed = self._dispatch()
        original = tasks.read_run(self.root, completed['run_id'])
        retained = runtime.read_receipt(self.root, original['latest_receipt'])['result']
        self.assertEqual(retained['result'], text)
        returned = tasks.read_return(self.root, completed['management_return']['return_id'])
        self.assertEqual(Path(returned['result_ref']['path']).read_text(), text)
        self.assertEqual(self._dispatch(explicit=False)['run_id'], original['id'])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_author_return_survives_missing_route_and_reads_back_same_run(self):
        self._stream(final={'contribution': 'Retain this completed original.', 'complete': True})
        with patch.object(dsh_headless.DeepSeekAdapter, '_query', return_value={}):
            first = self._dispatch()
        self.assertEqual(first['outcome'], 'succeeded', first)
        run = tasks.read_run(self.root, first['run_id'])
        original = run['latest_receipt']
        self.assertEqual(runtime.read_receipt(self.root, original)['result']['native_route_status'], 'unobserved')
        def offline_adapter(*args):
            raise runtime.CapabilityUnavailable('Host offline')

        cached = run_worker.programmatic(self.root, 'request', read_result=True,
            runtime_factory=lambda root: runtime.Runtime(root, adapter_factory=offline_adapter))
        self.assertEqual(cached['receipt'], original)
        final = run_worker.programmatic(self.root, 'request', read_result=True)
        self.assertEqual(final['run_id'], first['run_id'])
        self.assertEqual(final['management_return']['return_id'], first['management_return']['return_id'])
        result = runtime.read_receipt(self.root, final['receipt'])['result']
        self.assertEqual(result['native_route_status'], 'unobserved')
        self.assertEqual(final['receipt'], original)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_resumed_stream_cannot_reuse_original_turn(self):
        self._stream(final='Original completed report.')
        self._dispatch()
        note = self.home / 'continue.md'
        note.write_text('Continue the remaining comparison.')
        self._stream(final='Stale terminal response with the old turn ID.')
        current = self._dispatch(explicit=False, instructions=note)
        run = tasks.read_run(self.root, current['run_id'])
        self.assertEqual(run['outcome'], 'unknown')
        self.assertNotIn('turn_id', run['native_identity'])
        self.assertFalse(run['return_ids'])
        self._dispatch(explicit=False)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2)

    def test_prelaunch_admission_rejection_can_resume_without_native_replay(self):
        @contextmanager
        def reject(*args, **kwargs):
            tasks._fail('dependency_wait', 'The original dependency was just withdrawn.')
            yield

        self._stream(final={'contribution': 'Ready after original admission returns.', 'complete': True})
        with patch.object(tasks, 'dispatch_admission', reject):
            blocked = self._dispatch()
        original = tasks.read_run(self.root, blocked['run_id'])
        self.assertEqual(original['outcome'], 'failed')
        self.assertIsNone(original['native_identity'])
        self.assertFalse(self.argv_log.exists())
        receipt = runtime.read_receipt(self.root, original['latest_receipt'])
        self.assertEqual(receipt['observation']['category'], 'dependency_wait')
        self.assertEqual(self._dispatch(explicit=False)['outcome'], 'failed')
        note=self.home/'resume-rejected.md';note.write_text('The original launch dependency is available; continue this attempt.')
        self.assertEqual(self._dispatch(explicit=False,instructions=note)['outcome'], 'succeeded')
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_confirmed_startup_rejection_stays_a_host_diagnostic(self):
        self._stream(before_open='Native setup unavailable', exit_code=1, not_started=True)
        first = self._dispatch()
        run = tasks.read_run(self.root, first['run_id'])
        self.assertEqual(first['outcome'], 'failed')
        self.assertEqual(run['outcome'], 'failed')
        self.assertIsNone(run['native_identity'])
        self.assertFalse(run['return_ids'])
        recovered = run_worker.programmatic(self.root, 'request', read_result=True)
        self.assertEqual(recovered['observation']['native_submission'], 'not_started')
        self._stream(final={'contribution': 'Read-only recovery after startup fix.',
                            'complete': True, 'execution_end': 'complete'})
        self.assertEqual(self._dispatch(explicit=False)['outcome'],'failed')
        note=self.home/'resume-after-install.md';note.write_text('The host capability is restored; continue the original task.')
        completed = self._dispatch(explicit=False,instructions=note)
        self.assertEqual(completed['outcome'], 'succeeded')
        self.assertFalse(completed['management_return'].get('optional_issues'))
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2)

    def test_unusable_write_root_is_rejected_before_native_launch(self):
        blocked = self.home / 'not-a-directory'
        blocked.write_text('Original file stays intact.')
        config = json.loads(self.config_path.read_text())
        config['roles']['executor'].update(profile='native-workspace', native_policy={
            'sandbox': 'workspace-write', 'network_access': False,
            'writable_roots': [str(blocked / 'output')]})
        self.config_path.write_text(json.dumps(config))
        result = self._dispatch()
        self.assertEqual(result['outcome'], 'failed', result)
        run = tasks.read_run(self.root, result['run_id'])
        self.assertIsNone(run['native_identity'])
        receipt = runtime.read_receipt(self.root, run['latest_receipt'])
        self.assertEqual(receipt['native_status'], 'not_started')
        self.assertIn('write_root_unavailable', receipt['detail'])
        self.assertFalse(self.argv_log.exists())
        self.assertEqual(blocked.read_text(), 'Original file stays intact.')

    def test_direct_result_after_confirmed_pre_open_failure_prevents_a_second_launch(self):
        self._stream(before_open='Native setup unavailable', exit_code=1, not_started=True)
        failed = self._dispatch()
        original = tasks.read_run(self.root, failed['run_id'])
        self.assertEqual(original['outcome'], 'failed')

        result = self.home / 'alternate-method.json'
        result.write_text(json.dumps({'contribution': 'Checked the original source by an authorized local method.',
                                      'coverage': ['science'], 'artifacts': [self.evidence['path']]}))
        direct = run_worker.return_result(self.root, self.actor, str(result), request_id='request')
        self._stream(final={'contribution': 'A second native launch would replace the useful result.',
                            'complete': True})
        displayed = self._dispatch(explicit=False)
        self.assertEqual(displayed['state'], 'returned')
        self.assertEqual(displayed['management_return']['return_id'], direct['id'])
        self.assertEqual(displayed['original_run']['outcome'], 'failed')
        self.assertEqual(tasks.read_run(self.root, original['id']), original)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)


    def test_read_profile_uses_native_tools_and_scoped_cli_guidance(self):
        config = json.loads(self.config_path.read_bytes())
        config['roles'] = {'executor': config['roles']['executor']}
        self.config_path.write_text(json.dumps(config))
        self._stream(final={'contribution': 'The original is sufficient for a later step.', 'coverage': [],
                            'execution_end': 'external_wait', 'resume_when': 'The fixture is continued.'})
        self.assertEqual(self._dispatch()['outcome'], 'succeeded')
        launch = json.loads(self.argv_log.read_text())
        self.assertEqual(launch['policy']['allow'], ['read', 'bash'])
        self.assertIn(str(self.input_path), launch['input'])
        self.assertIn('[Original](authorization.md)', self.input_path.read_text())
        self.assertNotIn('--query', launch['input'])


    def test_persisted_completion_recovers_exact_report_but_not_active_or_later_work(self):
        config = runtime.load_config(self.root, str(self.config_path), 'executor')
        adapter = dsh_headless.DeepSeekAdapter(self.root, config)
        native = {'id': NATIVE_ID, 'turn_id': '4'}
        observed = {'source': 'dsh_persisted_session_log', 'terminal': 'completed',
                    'latest_turn': 4, 'native_goal': {'phase': 'complete'},
                    'final_text': '# Original report\nUnchanged.\n'}
        with patch.object(adapter, '_query', return_value=observed):
            result = adapter.readresult(native)
        self.assertEqual(result['native_status'], 'completed')
        self.assertEqual(result['result'], observed['final_text'])
        for change in ({'latest_turn': 5}, {'terminal': None},
                       {'native_goal': {'phase': 'active', 'roundsStarted': 32}},
                       {'native_goal': {'phase': 'active', 'activation': 'armed'}},
                       {'goal_observation_issue': 'Original Goal unavailable'}):
            with self.subTest(change=change), patch.object(adapter, '_query', return_value={**observed, **change}):
                value = adapter.readresult(native)
                self.assertEqual(value['native_status'], 'unknown')
                self.assertEqual(value['result'], observed['final_text'])

    def test_persisted_native_error_is_failed_even_with_an_active_goal(self):
        config = runtime.load_config(self.root, str(self.config_path), 'executor')
        adapter = dsh_headless.DeepSeekAdapter(self.root, config)
        native = {'id': NATIVE_ID, 'turn_id': '4'}
        observed = {'source': 'dsh_persisted_session_log', 'terminal': 'error',
                    'latest_turn': 4, 'native_error': {'code': 'TIMEOUT'},
                    'native_goal': {'phase': 'active', 'activation': 'armed'}}
        with patch.object(adapter, '_query', return_value=observed):
            result = adapter.readresult(native)
        self.assertEqual(result['native_status'], 'failed')
        self.assertEqual(result['native_diagnostic'], 'TIMEOUT')
        self.assertIsNone(result['result'])
        for change in ({'latest_turn': 5}, {'terminal': None}, {'source': 'synthetic_closer'}):
            with self.subTest(change=change), patch.object(adapter, '_query', return_value={**observed, **change}):
                self.assertEqual(adapter.readresult(native)['native_status'], 'unknown')

    def test_live_goal_observation_is_bound_to_exact_turn_and_does_not_claim_delivery(self):
        value = self._stream(final='A stage report; the agreed scientific work remains.')
        observed = {'type': 'htam_goal_state', 'sessionId': NATIVE_ID, 'turn': 4,
                    'source': 'dsh_live_agent', 'goal': {'id': 'original-goal',
                    'phase': 'active', 'activation': 'disarmed', 'roundsStarted': 32}}
        value['events'][-1:-1] = [observed,
            {**observed, 'sessionId': 'another-author', 'goal': {'phase': 'complete'}},
            {**observed, 'turn': 3, 'goal': {'phase': 'complete'}}]
        self.state.write_text(json.dumps(value))
        completed = self._dispatch()
        self.assertEqual(completed['outcome'], 'succeeded')
        run = tasks.read_run(self.root, completed['run_id'])
        retained = runtime.read_receipt(self.root, run['latest_receipt'])['result']
        self.assertEqual(retained['native_goal_observation'], observed)
        self.assertEqual(tasks.read_task(self.root, 'task')['task']['state'], 'active')

    def test_old_error_wording_does_not_authorize_replay(self):
        self._stream(before_open='headless --permission-mode requires the permissionPresets service',exit_code=1)
        result=self._dispatch()
        self.assertEqual(result['outcome'],'unknown')

    def test_missing_shell_exit_observation_preserves_unknown_and_original_report(self):
        value=self._stream(final='Original result survives missing shell metadata.')
        value['events']=[e for e in value['events'] if e['type']!='htam_shell_result']
        self.state.write_text(json.dumps(value))
        result=self._dispatch()
        run=tasks.read_run(self.root,result['run_id'])
        observed=runtime.read_receipt(self.root,run['latest_receipt'])['result']
        self.assertEqual(result['outcome'],'succeeded')
        self.assertEqual(observed['shell_results'],[{'native_item_id':'shell-1|opaque','exit_code':None}])
        self.assertEqual(observed['tool_failures'],[])
        self.assertEqual(observed['result'],'Original result survives missing shell metadata.')
