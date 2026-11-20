"""Original CLI routes with isolated legacy Sessions; no production/model calls."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import execution_lease
import harness_tasks as tasks
import test_dsh_headless as fixture

REPO = Path(__file__).resolve().parents[1]


class DispatchBasisRaceTests(unittest.TestCase):
    ref = fixture.fixtures.RuntimeTests.ref
    create_request = fixture.fixtures.RuntimeTests.create_request
    setUp = fixture.fixtures.RuntimeTests.setUp
    execute = fixture.fixtures.RuntimeTests.execute
    prepare_review = fixture.fixtures.RuntimeTests.prepare_review

    def test_unrelated_parent_report_arriving_before_native_start_does_not_reject_review(self):
        _, packet = self.prepare_review()
        report = self.home / 'parent-report.md'
        report.write_text('Author returned a stage report while the independent launch was checking.')
        case = self
        class ConcurrentReturn(fixture.fixtures.CompletedAdapter):
            def execute(self, *args, **kwargs):
                tasks.mutate(case.root, 'return', case.actor, {'id': 'late-parent-return',
                    'request_id': 'request', 'result_ref': case.ref(report),
                    'summary': report.read_text(), 'artifact_refs': [], 'coverage': [], 'remaining_scope': []})
                return super().execute(*args, **kwargs)
        self.factory = lambda root, config: ConcurrentReturn(root, config,
            author=fixture.runtime.identity('codex', 'thread', 'independent-reviewer', root, '1'))
        self.assertEqual(self.execute(role='scientific_reviewer', request_id='review', run_id='review-run',
            input_path=str(packet), input_sha256=self.ref(packet)['sha256'])['outcome'], 'succeeded')

    def test_changed_frozen_review_packet_still_blocks_native_start(self):
        _, packet = self.prepare_review()
        original_ref = self.ref(packet)
        class ChangedPacket(fixture.fixtures.CompletedAdapter):
            def execute(self, *args, **kwargs):
                packet.write_text('{"changed":true}')
                return super().execute(*args, **kwargs)
        self.factory = lambda root, config: ChangedPacket(root, config,
            author=fixture.runtime.identity('codex', 'thread', 'independent-reviewer', root, '1'))
        with self.assertRaises(fixture.runtime.CapabilityUnavailable):
            self.execute(role='scientific_reviewer', request_id='review', run_id='review-run',
                input_path=str(packet), input_sha256=original_ref['sha256'])
        self.assertIsNone(tasks.read_run(self.root, 'review-run')['native_identity'])


class LegacyContinuationTests(unittest.TestCase):
    ref = fixture.DeepSeekProgramTests.ref
    _stream = fixture.DeepSeekProgramTests._stream
    _dispatch = fixture.DeepSeekProgramTests._dispatch

    def create_request(self):
        if self._testMethodName != 'test_public_cli_review_outlives_author_and_returns_on_same_session':
            return fixture.DeepSeekProgramTests.create_request(self)
        self.input_path.write_text(json.dumps({'data_support': False, 'purpose': 'Isolated author review continuation',
            'consumer': 'Offline fixture', 'endpoint': 'Local proof only', 'evidence': [self.evidence['path']]}))
        self.input_ref = self.ref(self.input_path)
        fixture.fixtures.RuntimeTests.create_request(self, finite=True, programmatic=True, requires_review=True)

    def setUp(self):
        fixture.DeepSeekProgramTests.setUp(self)
        self.native_query = self.home / 'native-query.json'
        self.artifact = self.home / 'sessions/project/original/session.v4.jsonl.zstd'
        self.artifact.parent.mkdir(parents=True)
        self.artifact.write_bytes(b'Isolated query fixture; installed persistence is qualified separately.')
        self.native_lock = self.artifact.with_name('session.lock')
        self.native_lock.touch()
        self.observation = {'source': 'dsh_persisted_session_log', 'session_id': fixture.NATIVE_ID,
                            'cwd': str(self.root), 'terminal': 'completed', 'latest_turn': 4,
                            'latest_terminal': 'completed',
                            'artifact_path': str(self.artifact),
                            'route': {'provider': 'configured-dsh', 'model': 'deepseek-fixture', 'reasoningEffort': 'max'},
                            'native_goal': {'id': 'original-goal', 'phase': 'active', 'revision': 7}}
        self.native_query.write_text(json.dumps(self.observation))
        shim = self.home / 'node-shim'
        shim.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
from pathlib import Path
if '--query' in sys.argv:
    print(Path(os.environ['HTAM_LEGACY_QUERY']).read_text())
else:
    os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
''')
        shim.chmod(0o700)
        config = json.loads(self.config_path.read_bytes())
        config['hosts']['dsh']['node'] = str(shim)
        self.config_path.write_text(json.dumps(config))
        env = patch.dict(os.environ, {'HTAM_LEGACY_QUERY': str(self.native_query)})
        env.start()
        self.addCleanup(env.stop)
        # These native persistence/query records are synthetic. Their normal
        # ledger/Goal tests need a synthetic quiescent OS fact too; a busy real
        # desktop can legitimately return unknown before those branches run.
        # The descendant case keeps the real scanner, and the explicit EPERM
        # case below still verifies that unknown never releases execution.
        self.os_fixture = self._testMethodName != 'test_session_owned_descendant_blocks_without_its_original_parent'
        if self.os_fixture:
            observed = patch.object(execution_lease, 'session_process_observation', return_value={
                'state': 'clear', 'source': 'isolated synthetic Session fixture; not a production OS qualification'})
            observed.start()
            self.addCleanup(observed.stop)
            self.fixture_entry = self.home / 'harness-fixture-entry.py'
            self.fixture_entry.write_text('import runpy,sys\n'
                + 'sys.path.insert(0,' + repr(str(REPO / 'bin')) + ')\n'
                + 'import execution_lease\n'
                + "execution_lease.session_process_observation=lambda *_: {'state':'clear','source':'isolated synthetic Session fixture'}\n"
                + 'sys.argv=sys.argv[1:]\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')

    def legacy(self):
        self._stream(final='Original report remains usable.')
        result = self._dispatch()
        run = tasks.read_run(self.root, result['run_id'])
        from harness_package import launch_spec
        self.metadata = execution_lease.metadata_path(launch_spec(self.root, run)['evidence_home'], run['id'])
        self.assertTrue(self.metadata.is_file())
        self.metadata.unlink()
        self.original = run
        return run

    def command(self, run, *extra):
        prefix = [sys.executable, str(self.fixture_entry)] if self.os_fixture else [sys.executable]
        return [*prefix, str(REPO / 'bin/harness.py'), '--root', str(self.root),
                'work-dispatch', '--request', 'request', '--execute', '--continue',
                '--run-id', run['id'], *extra]

    def cli(self, run):
        return subprocess.run(self.command(run), cwd=self.root, env=os.environ.copy(),
                              capture_output=True, text=True, timeout=30)

    def test_public_cli_legacy_continuation_retains_session_goal_report_without_new_contract(self):
        run = self.legacy()
        with tasks.transaction(self.root) as c:
            c.execute('DELETE FROM work_delivery_contracts')
        old_bytes = Path(run['latest_receipt']['path']).read_bytes()
        self._stream(turn=5, final='Continued the same original delivery.')
        result = self.cli(run)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        latest = tasks.read_run(self.root, json.loads(result.stdout)['run_id'])
        self.assertEqual(latest['native_identity']['id'], run['native_identity']['id'])
        self.assertEqual(Path(run['latest_receipt']['path']).read_bytes(), old_bytes)
        self.assertFalse(self.metadata.exists(), 'Observation must not fabricate old execution metadata')
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2)

    def test_terminal_receipt_and_empty_lock_cannot_replace_native_readback(self):
        run = self.legacy()
        self.native_query.write_text(json.dumps({'read_observation_issue': 'Original artifact unavailable'}))
        result = self.cli(run)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown', result.stdout + result.stderr)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_original_native_writer_lock_blocks_continuation(self):
        run = self.legacy()
        with self.native_lock.open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli(run)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_session_owned_descendant_blocks_without_its_original_parent(self):
        run = self.legacy()
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
            env={**os.environ, 'DSH_SESSION_ID': fixture.NATIVE_ID, 'DSH_HOME': str(self.home)},
            start_new_session=True)
        try:
            for _ in range(2):
                result = self.cli(run)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('active', result.stdout + result.stderr)
            self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_current_paused_goal_is_preserved(self):
        run = self.legacy()
        self.observation['native_goal']['phase'] = 'paused'
        self.native_query.write_text(json.dumps(self.observation))
        result = self.cli(run)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('paused', result.stdout + result.stderr)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_later_native_turn_without_terminal_can_continue_after_os_exit(self):
        run = self.legacy()
        self.observation.update(latest_turn=5, latest_terminal=None)
        self.native_query.write_text(json.dumps(self.observation))
        self._stream(turn=6, final='Continued after the retained later native turn.')
        result = self.cli(run)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tasks.read_run(self.root, run['id']), run)

    def test_os_attribution_failure_does_not_release_legacy_execution(self):
        run = self.legacy()
        with patch.object(execution_lease, 'session_process_observation', return_value={
                'state': 'unknown', 'reason': 'EPERM observing original Session process ownership'}):
            observed = execution_lease.observe_run(self.root, run)
        self.assertEqual(observed['state'], 'unknown')
        self.assertIn('EPERM', observed['reason'])
        self.assertFalse(self.metadata.exists())

    def test_native_artifact_and_lock_are_both_required(self):
        run = self.legacy()
        self.native_lock.unlink()
        observed = execution_lease.observe_run(self.root, run)
        self.assertEqual(observed['state'], 'unknown')
        self.assertFalse(self.native_lock.exists(), 'Readback cannot manufacture a native lock')

    def test_unreadable_metadata_uses_original_evidence_but_valid_launch_intent_stays_unknown(self):
        run = self.legacy()
        self.metadata.write_text('[]')
        self.assertEqual(execution_lease.observe_run(self.root, run)['state'], 'exited')
        self.assertEqual(self.metadata.read_text(), '[]')
        self.metadata.write_text(json.dumps({'phase': 'launch_intent', 'manager': {'pid': 99999999}}))
        self.assertEqual(execution_lease.observe_run(self.root, run)['state'], 'unknown')

    def test_explicit_original_stop_still_blocks_technical_continue(self):
        run = self.legacy()
        tasks.mutate(self.root, 'run-stop', self.actor, {'id': run['id'], 'summary': 'Explicit fixture stop'})
        result = self.cli(run)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stop', result.stdout + result.stderr)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_confirmed_os_exit_does_not_clear_unknown_data_effect(self):
        run = self.legacy()
        attempt = self.home / 'original-unknown-data-attempt.json'
        attempt.write_text(json.dumps({'schemaVersion': 1, 'operation': 'dataset process save',
            'outcome': 'effect_unknown', 'status': 'unknown', 'data': {'attempt_id': 'original-attempt'}}))
        original_bytes = attempt.read_bytes()
        tasks.mutate(self.root, 'return', self.actor, {'id': 'unknown-effect-report',
            'request_id': 'request', 'result_ref': self.ref(attempt), 'summary': 'Reconcile the original exact data attempt.',
            'artifact_refs': [], 'coverage': [], 'remaining_scope': []})
        self.assertEqual(execution_lease.observe_run(self.root, run)['state'], 'exited')
        import harness_scope
        checked = harness_scope.check(self.root, 'request', 'request', 'handoff')
        self.assertTrue(any(item['id'].endswith('unknown_data_effect') for item in checked['missing']))
        self.assertEqual(attempt.read_bytes(), original_bytes)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)

    def test_two_public_continues_only_start_one_native_execution(self):
        run = self.legacy()
        self._stream(turn=5, final='Only one continuation.')
        first = subprocess.Popen(self.command(run), cwd=self.root, env=os.environ.copy(),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(self.command(run), cwd=self.root, env=os.environ.copy(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        replies = [p.communicate(timeout=30) for p in (first, second)]
        self.assertTrue(any(p.returncode == 0 for p in (first, second)), replies)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2, replies)

    def test_public_cli_review_outlives_author_and_returns_on_same_session(self):
        config = json.loads(self.config_path.read_bytes())
        config['roles']['executor'].update(profile='native-workspace', native_policy={
            'sandbox': 'workspace-write', 'network_access': False, 'writable_roots': [str(self.home)]})
        config['roles']['scientific_reviewer'] = {**config['roles']['executor'],
            'profile': 'read-only-evidence', 'native_policy': {'network_access': False}}
        self.config_path.write_text(json.dumps(config))
        native_script = Path(config['hosts']['dsh']['binary'])
        native_script.write_text(native_script.read_text().replace('review_id = None',
            "review_id = os.environ.get('HTAM_FIXTURE_REVIEW_ID')").replace(
            "if policy['allow'] == ['read'] and 'reviewer' in value:",
            "if policy['allow'] == ['read'] and 'reviewer' in value:\n    import time; time.sleep(1)"))
        first = self._stream(final='Author stage report; independent review remains in progress.')
        first['write_artifact'] = True
        first['checks'] = [{'kind': 'request_review', 'accepted': True, 'text': json.dumps({
            'artifacts': [str(self.candidate)], 'question': 'Check the dry mass denominator.'})}]
        reviewer = self._stream(final='Use dry mass in the denominator; retain the exact frozen candidate.')
        reviewer['events'] = [e for e in reviewer['events'] if e['type'] not in ('tool_call', 'htam_shell_result', 'tool_result')]
        for event in reviewer['events']:
            if 'sessionId' in event:
                event['sessionId'] = 'isolated-independent-reviewer'
        self.state.write_text(json.dumps({**first, 'reviewer': reviewer}))
        command = [sys.executable, str(REPO / 'bin/harness.py'), '--root', str(self.root),
                   'work-dispatch', '--request', 'request', '--execute']
        started = subprocess.run(command, cwd=self.root, env=os.environ.copy(), capture_output=True, text=True, timeout=20)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        author = tasks.read_run(self.root, json.loads(started.stdout)['run_id'])
        from run_worker import _selected_run
        reviews = [q for q in tasks.read_task(self.root, 'task')['requests'] if q['independent']]
        self.assertEqual(len(reviews), 1)
        review_id = reviews[0]['id']
        early = _selected_run(self.root, review_id)
        self.assertTrue(early is None or early['outcome'] in ('intent', 'running'), 'Review must outlive the author CLI')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            reviewed = _selected_run(self.root, review_id)
            if reviewed and (reviewed['outcome'] in ('failed', 'unknown') or
                             reviewed['outcome'] == 'succeeded' and reviewed['return_ids']):
                break
            time.sleep(0.05)
        diagnostic = (Path(reviewed['latest_receipt']['path']).read_text() if reviewed.get('latest_receipt') else str(reviewed))
        self.assertEqual(reviewed['outcome'], 'succeeded', diagnostic)
        self.assertNotEqual(reviewed['native_identity']['id'], author['native_identity']['id'])
        self.assertTrue(reviewed['return_ids'])
        from harness_package import launch_spec
        execution_lease.metadata_path(launch_spec(self.root, author)['evidence_home'], author['id']).unlink()
        resumed = self._stream(turn=5, final='Author received the original opinion and continued the original candidate.')
        resumed['checks'] = [{'kind': 'review_result', 'accepted': True}]
        self.state.write_text(json.dumps(resumed))
        with patch.dict(os.environ, {'HTAM_FIXTURE_REVIEW_ID': review_id}):
            result = self.cli(author)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        continued = tasks.read_run(self.root, json.loads(result.stdout)['run_id'])
        self.assertEqual(continued['native_identity']['id'], author['native_identity']['id'])
        from agent_runtime import read_receipt
        body = read_receipt(self.root, continued['latest_receipt'])['result']
        self.assertEqual(body['collected_review_return_ids'], reviewed['return_ids'])
        self.assertEqual(body['requested_review_ids'], [review_id])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 3)


if __name__ == '__main__':
    unittest.main()
