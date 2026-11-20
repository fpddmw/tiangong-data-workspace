"""Offline only: native transports are fakes; no host/model/API is invoked."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import agent_runtime as runtime
import codex_adapter
import claude_adapter
import collaboration_tools
import harness_tasks as core
import production_review as review
import codex_host


def configuration():
    return {'schema': runtime.CONFIG_SCHEMA, 'version': 'offline-v1',
            'hosts': {'codex': {'adapter': 'codex-app-server', 'binary': 'offline-codex', 'enabled': True},
                      'claude': {'adapter': 'claude-code-print', 'binary': 'offline-claude', 'enabled': True}},
            'providers': {'native': {'codex_model_provider': 'offline-provider'},
                          'claude': {'selection': 'existing-host-configuration'}},
            'roles': {name: {'host': 'codex', 'provider': 'native', 'model': 'exact-' + name,
                             'profile': 'read-only-evidence'} for name in runtime.ROLES}}


class CompletedAdapter:
    def __init__(self, root, config, *, fail=False, author=None, pause=None):
        self.root, self.config, self.fail, self.author, self.pause = root, config, fail, author, pause
        self.starts = 0
        self.closed = False

    def execute(self, text, observe, guard, **kwargs):
        self.starts += 1
        self.text, self.kwargs = text, kwargs
        with kwargs.get('boundary', contextlib.nullcontext)():
            guard()
        if self.fail:
            raise TimeoutError('native response lost')
        native = self.author or runtime.identity('codex', 'thread', 'actual-child', self.root, 'turn-one')
        observe(native, 'inProgress', {})
        if self.pause:
            self.pause()
        with kwargs.get('boundary', contextlib.nullcontext)():
            guard()
        artifact = self.root / 'work/original/fixture-result.json'
        artifact.write_text('{"actual":"saved fixture result"}')
        ref = {'id': 'candidate', 'version': '1', 'path': str(artifact), 'sha256': runtime.digest(artifact.read_bytes())}
        body = ({'verdict': 'accept', 'summary': 'Reviewed exact supplied scope and original evidence; opinion only.',
                 'findings': [], 'coverage': ['science'], 'remaining_scope': []}
                if self.config.get('role') == 'scientific_reviewer' else
                {'summary': 'Actual saved fixture contribution', 'coverage': ['science'], 'remaining_scope': [],
                 'artifact_refs': [ref], 'result_ref': {'path': ref['path'], 'sha256': ref['sha256']}})
        return {'native_identity': native, 'native_status': 'completed', 'source': 'native_rpc',
                'result': json.dumps(body)}

    def readresult(self, native, *, boundary=None):
        return {'native_identity': native, 'native_status': 'completed', 'source': 'native_rpc', 'result': 'recovered original native result'}

    def stop(self, native, *, boundary=None):
        return {'native_identity': native, 'native_status': 'unknown', 'source': 'native_rpc', 'stop_confirmed': False}

    def close(self):
        self.closed = True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'work/original'
        self.home.mkdir(parents=True)
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID': 'lead'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.actor = core.verified_actor(self.root)
        self.config_path = self.root / 'runtime.json'
        self.config_path.write_text(json.dumps(configuration()))
        self.input_path = self.home / 'input.json'
        self.input_path.write_text(json.dumps({'scope': 'offline bounded work'}))
        self.input_ref = self.ref(self.input_path)
        evidence = self.home / 'authorization.md'
        evidence.write_text('Fixture source refactor permission. No real data or model calls.')
        self.evidence = self.ref(evidence)
        core.initialize(self.root, self.actor, self.evidence, 'offline-install')
        core.mutate(self.root, 'grant-role', self.actor, {'id': 'grant', 'actor': self.actor,
                    'org_role': 'task_lead', 'evidence_ref': self.evidence})
        self.task = core.mutate(self.root, 'create', self.actor, {'id': 'task', 'owner_actor': self.actor,
                    'original_ref': self.evidence, 'plan_ref': self.evidence, 'summary': 'Offline complete task', 'required_scope': ['science']})
        import harness_scope
        harness_scope.apply_scope(self.root,'task',{'items':[{'scope_id':'science','kind':'deliverable','label':'Original scientific fixture','source_refs':[self.evidence]}],
            'source_ref':self.evidence,'verified_scope_ids':['science']},self.actor)
        self.create_request()
        self.adapters = []
        def factory(root, config):
            adapter = CompletedAdapter(root, config)
            self.adapters.append(adapter)
            return adapter
        self.factory = factory

    def ref(self, path):
        return {'path': str(path), 'sha256': runtime.digest(path.read_bytes())}

    def create_request(self, **changes):
        payload = {'id': 'request', 'task_id': 'task', 'kind': 'plan', 'recipient_role': 'executor',
                   'input_ref': self.input_ref, 'summary': 'bounded work', 'plan_revision': 1,
                   'required_scope': ['science'],
                   'delivery_contract':{'purpose':'Offline exact scientific fixture','endpoint':'evidence',
                       'scope_boundary':{'in_scope':'Original fixture scope and input.','out_of_scope':'No real host, credentials or remote data writes.'},
                       'acceptance':{'text':'Retain the exact original fixture result.','refs':[self.evidence]},
                       'authority_refs':[],'interfaces':{'text':'','refs':[]}}}
        payload.update(changes)
        return core.mutate(self.root, 'request', self.actor, payload)

    def execute(self, **changes):
        kwargs = {'config_path': 'runtime.json', 'role': 'executor', 'task_id': 'task', 'request_id': 'request',
                  'run_id': 'run', 'plan_revision': 1, 'input_path': str(self.input_path),
                  'input_sha256': self.input_ref['sha256'], 'evidence_home': str(self.home)}
        kwargs.update(changes)
        return runtime.Runtime(self.root, adapter_factory=self.factory).execute(**kwargs)

    def test_distinct_configured_models_and_providers_are_selected(self):
        for role in runtime.ROLES:
            selected = runtime.load_config(self.root, 'runtime.json', role)
            self.assertEqual(selected['model'], 'exact-' + role)
            self.assertEqual(selected['provider']['codex_model_provider'], 'offline-provider')
            self.assertEqual(runtime.capability(selected)['model_availability'], 'not_probed')

    def test_load_config_retains_large_valid_tool_policy_and_exact_path_checks(self):
        document = configuration()
        allowed = [f'Read({self.home}/source-package-{number:05d}/original-evidence-register.json)'
                   for number in range(13000)]
        document['roles']['executor'].update(host='claude', provider='claude', profile='read-only-evidence',
            native_policy={'claude_tools': ['Read'], 'claude_allowed_tools': allowed,
                           'filesystem_boundary': 'host-native-permissions',
                           'network_boundary': 'host-native-permissions'})
        self.config_path.write_text(json.dumps(document))
        self.assertGreater(self.config_path.stat().st_size, 1024 * 1024)
        selected = runtime.load_config(self.root, 'runtime.json', 'executor')
        self.assertEqual(selected['native_policy']['claude_allowed_tools'], allowed)
        self.assertEqual(selected['config_ref'], self.ref(self.config_path))

        alias = self.root / 'runtime-alias.json'
        alias.symlink_to(self.config_path)
        self.assertEqual(runtime.load_config(self.root, alias, 'executor')['config_ref'], selected['config_ref'])
        with self.assertRaisesRegex(runtime.entry.ProductionError, 'regular file'):
            runtime.load_config(self.root, self.home, 'executor')

    def test_missing_role_unknown_provider_and_unverified_tool_scope_fail(self):
        for change in ('role', 'provider', 'tools'):
            value = configuration()
            if change == 'role':
                del value['roles']['executor']
            elif change == 'provider':
                value['roles']['executor']['provider'] = 'missing'
            else:
                value['roles']['executor']['profile'] = 'arbitrary-tools'
            self.config_path.write_text(json.dumps(value))
            with self.assertRaises(runtime.RuntimeError):
                runtime.load_config(self.root, 'runtime.json', 'executor')

    def test_intent_is_canonical_and_native_identity_is_bound(self):
        result = self.execute()
        row = core.read_run(self.root, 'run')
        self.assertEqual(row['outcome'], 'succeeded')
        self.assertEqual(row['native_identity']['id'], 'actual-child')
        self.assertEqual(row['config_ref']['sha256'], runtime.digest(self.config_path.read_bytes()))
        self.assertEqual(result['management_return']['status'], 'registered')
        self.assertTrue(self.adapters[0].closed)
        returned = core.current_view(self.root, task_id='task')['tasks'][0]['returns']
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]['identity_assurance'], 'dispatcher_observed_native_receipt')

    def test_codex_executor_receives_review_service_assembly(self):
        from agent_review import ReviewService
        result = self.execute()
        service = self.adapters[0].review_service
        self.assertIsInstance(service, ReviewService)
        receipt = runtime.read_receipt(self.root, result['receipt'])
        self.assertEqual(receipt['result']['requested_review_ids'], [])
        self.assertEqual(receipt['result']['collected_review_return_ids'], [])

    def test_non_executor_role_gets_no_review_service(self):
        self.create_request(id='lead-request', recipient_role='task_lead')
        self.execute(request_id='lead-request', role='task_lead')
        self.assertFalse(hasattr(self.adapters[0], 'review_service'))

    def test_native_return_does_not_interpret_maintenance_fields_as_commands(self):
        class FrictionAdapter(CompletedAdapter):
            def execute(adapter, *args, **kwargs):
                observed = super().execute(*args, **kwargs)
                body = json.loads(observed['result'])
                path = Path(body['result_ref']['path'])
                path.write_text(json.dumps({'contribution': 'Unaffected scope completed; shared wrapper failed',
                    'maintenance_findings': [{'summary': 'Shared wrapper failed; retain original input and retry point',
                                             'evidence': ['Original native input and error receipt']}]}))
                body['result_ref']['sha256'] = runtime.digest(path.read_bytes())
                body['artifact_refs'][0].update(sha256=body['result_ref']['sha256'])
                body['maintenance_findings'] = json.loads(path.read_text())['maintenance_findings']
                observed['result'] = json.dumps(body)
                return observed

        self.factory = FrictionAdapter
        result = self.execute()
        import harness_maintenance
        queue = harness_maintenance.queue(self.root)
        self.assertEqual(queue['findings'], [])
        # Recovering the saved native return does not relaunch or duplicate pickup.
        recovered = runtime.Runtime(self.root, adapter_factory=lambda *_: self.fail('No new host')).readresult(
            run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
        self.assertEqual(recovered['receipt'], result['receipt'])
        self.assertEqual(harness_maintenance.queue(self.root)['findings'], [])

    def test_same_intent_never_relaunches_after_lost_client_reply(self):
        self.execute()
        with self.assertRaisesRegex(runtime.RuntimeError, 'already exists'):
            self.execute()
        self.assertEqual(len(self.adapters), 1)

    def test_unknown_response_blocks_same_and_different_run_ids(self):
        self.factory = lambda root, config: CompletedAdapter(root, config, fail=True)
        with self.assertRaises(TimeoutError):
            self.execute()
        self.assertEqual(core.read_run(self.root, 'run')['outcome'], 'unknown')
        for run_id in ('run', 'replacement'):
            with self.assertRaises((runtime.RuntimeError, core.WorkConflict)):
                self.execute(run_id=run_id)


    def test_native_start_uses_workspace_and_paths_without_copying_brief(self):
        body = 'Source text stays here: independent evidence with 1000 and 0.001.'
        self.input_path.write_text(body)
        self.input_ref = self.ref(self.input_path)
        self.create_request(id='direct-files', input_ref=self.input_ref)
        self.execute(request_id='direct-files')
        adapter, = self.adapters
        self.assertEqual(adapter.config['native_cwd'], str(self.root))
        for path in (self.root / 'AGENTS.md', self.root / 'roles/data.md', self.input_path):
            self.assertIn(str(path), adapter.text)
        self.assertNotIn(body, adapter.text)
        self.assertEqual(self.input_path.read_text(), body)

    def test_codex_pre_turn_admission_failure_retains_and_resumes_the_exact_thread(self):
        hosts = []

        def factory(root, config):
            host = FakeRPC(root, config)
            hosts.append(host)
            return codex_adapter.CodexAdapter(root, config, host=host)

        self.factory = factory
        admit = core.dispatch_admission

        @contextlib.contextmanager
        def withdraw_after_thread(*args, **kwargs):
            if any(method == 'thread/start' for method, _ in hosts[0].calls):
                core._fail('dependency_wait', 'The original input was withdrawn before the model turn.')
            with admit(*args, **kwargs):
                yield

        with patch.object(core, 'dispatch_admission', withdraw_after_thread):
            with self.assertRaisesRegex(runtime.CapabilityUnavailable, 'dependency_wait'):
                self.execute()
        original = core.read_run(self.root, 'run')
        native = runtime.identity('codex', 'thread', 'thread', self.root)
        self.assertEqual(original['native_identity'], native)
        self.assertEqual(original['outcome'], 'failed')
        self.assertNotIn('turn/start', [method for method, _ in hosts[0].calls])
        worker = runtime.Runtime(self.root, adapter_factory=lambda *_: self.fail('Read the retained pre-turn diagnostic'))
        retained = worker.readresult(run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
        self.assertEqual(retained['native_identity'], native)
        self.assertEqual(retained['observation']['phase'], 'before_turn')
        self.assertEqual(retained['observation']['category'], 'dependency_wait')

        resumed = self.execute(run_id='next-turn', operation='resume', previous_run_id='run')
        self.assertEqual(resumed['outcome'], 'succeeded')
        self.assertEqual(resumed['native_identity']['id'], native['id'])
        self.assertEqual(resumed['native_identity']['turn_id'], 'turn')
        self.assertEqual([params['threadId'] for method, params in hosts[1].calls if method == 'thread/resume'], ['thread'])
        self.assertNotIn('thread/start', [method for method, _ in hosts[1].calls])

    def test_codex_disconnect_after_turn_send_keeps_the_bound_thread_unknown(self):
        class DisconnectedAfterSend(FakeRPC):
            def call(host, method, params):
                response = super().call(method, params)
                if method == 'turn/start':
                    raise runtime.HostError('The original submission response was lost.')
                return response

        self.factory = lambda root, config: codex_adapter.CodexAdapter(root, config, host=DisconnectedAfterSend(root, config))
        with self.assertRaises(runtime.HostError):
            self.execute()
        original = core.read_run(self.root, 'run')
        self.assertEqual(original['outcome'], 'unknown')
        self.assertEqual(original['native_identity'], runtime.identity('codex', 'thread', 'thread', self.root))
        with self.assertRaisesRegex(core.WorkConflict, 'reconcile'):
            self.execute(run_id='replacement')

    def test_readresult_reuses_original_receipt_without_host_or_model(self):
        result = self.execute()
        self.config_path.unlink()
        for saved in self.home.glob('runtime-config-*.json'):
            saved.unlink()
        adapter_factory = lambda *_: self.fail('Retained result must not start a host')
        with patch.dict(os.environ, {'CODEX_THREAD_ID':'successor-chat'}):
            recovered = runtime.Runtime(self.root, adapter_factory=adapter_factory).readresult(
                run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
        self.assertEqual(recovered['receipt'], result['receipt'])
        self.assertEqual(recovered['source'], 'retained_original_receipt')
        self.assertEqual(recovered['management_return']['status'], 'registered')

    def test_inconclusive_poll_does_not_change_the_live_run_or_its_receipt(self):
        class UnfinishedReader(CompletedAdapter):
            def readresult(reader, native, *, boundary=None):
                return {'native_identity': native, 'native_status': 'unknown', 'result': None,
                        'native_diagnostic': 'original_session_still_active_or_terminal_unavailable'}

        def poll_while_executing():
            before = core.read_run(self.root, 'run')
            reader = runtime.Runtime(self.root, adapter_factory=UnfinishedReader)
            polled = reader.readresult(run_id='run', config_path='runtime.json',
                                      role='executor', evidence_home=str(self.home))
            self.assertEqual(polled['outcome'], 'running')
            self.assertEqual(core.read_run(self.root, 'run'), before)
            self.assertEqual(polled['result']['native_status'], 'unknown')

        self.factory = lambda root, config: CompletedAdapter(root, config, pause=poll_while_executing)
        completed = self.execute()
        self.assertEqual(completed['outcome'], 'succeeded')
        self.assertEqual(len(core.read_run(self.root, 'run')['return_ids']), 1)

    def start_running_poll_fixture(self):
        class InitiallyRunning(CompletedAdapter):
            def execute(adapter,*args,**kwargs):
                result=super().execute(*args,**kwargs)
                return {**result,'native_status':'running','result':None}
        self.factory=InitiallyRunning;self.execute()

    def concurrent_terminal_poll(self,stale_status):
        self.start_running_poll_fixture()
        published=[];fixture=self
        class RacingReader(CompletedAdapter):
            def readresult(adapter,native,*,boundary=None):
                with (boundary or contextlib.nullcontext)():pass
                config=runtime.load_config(fixture.root,'runtime.json','executor')
                value=runtime.Runtime(fixture.root)._record_result('run',config,{
                    'native_identity':native,'native_status':'completed','result':'Complete original executor result, with its conditions.'},str(fixture.home))
                published.append(value)
                return {'native_identity':native,'native_status':stale_status,'result':None}
        received=runtime.Runtime(self.root,adapter_factory=RacingReader).readresult(
            run_id='run',config_path='runtime.json',role='executor',evidence_home=str(self.home))
        self.assertEqual(received['outcome'],'succeeded')
        self.assertEqual(received['receipt'],published[0]['receipt'])
        self.assertEqual(received['result']['result'],'Complete original executor result, with its conditions.')
        current=core.read_run(self.root,'run')
        self.assertEqual(current['latest_receipt'],published[0]['receipt'])
        self.assertEqual(len(current['return_ids']),1)

    def test_late_running_poll_returns_the_concurrent_original_terminal(self):
        self.concurrent_terminal_poll('running')

    def test_late_incomplete_completed_poll_cannot_replace_full_executor_receipt(self):
        self.concurrent_terminal_poll('completed')

    def test_poll_of_previous_turn_preserves_concurrent_live_native_advance(self):
        self.start_running_poll_fixture();fixture=self;advanced=[]
        class RacingReader(CompletedAdapter):
            def readresult(adapter,native,*,boundary=None):
                with (boundary or contextlib.nullcontext)():pass
                run=core.read_run(fixture.root,'run')
                doc=runtime.read_receipt(fixture.root,run['latest_receipt'])
                later={**native,'turn_id':'turn-two'}
                doc.update(native_identity=later,native_status='running',observation={
                    'source':'codex_turn_start','previous_turn':{'turn_id':native['turn_id'],'terminal':'completed'}})
                ref=runtime.Runtime(fixture.root)._write(str(fixture.home),'run',doc)
                core.mutate(fixture.root,'run-bind',fixture.actor,{'id':'run','receipt':ref,'summary':'Actual concurrent native turn start'})
                advanced.append(ref)
                return {'native_identity':native,'native_status':'running','result':None}
        observed=runtime.Runtime(self.root,adapter_factory=RacingReader).readresult(
            run_id='run',config_path='runtime.json',role='executor',evidence_home=str(self.home))
        current=core.read_run(self.root,'run')
        self.assertEqual(observed['source'],'concurrent_original_turn_advanced')
        self.assertEqual(current['native_identity']['turn_id'],'turn-two')
        self.assertEqual(current['latest_receipt'],advanced[0])

    def test_live_turn_advance_before_read_boundary_returns_current_identity_without_probe(self):
        self.start_running_poll_fixture();fixture=self
        class RacingReader(CompletedAdapter):
            def readresult(adapter,native,*,boundary=None):
                run=core.read_run(fixture.root,'run')
                doc=runtime.read_receipt(fixture.root,run['latest_receipt'])
                doc.update(native_identity={**native,'turn_id':'turn-two'},native_status='running',
                    observation={'source':'codex_turn_start','previous_turn':{'turn_id':native['turn_id'],'terminal':'completed'}})
                ref=runtime.Runtime(fixture.root)._write(str(fixture.home),'run',doc)
                core.mutate(fixture.root,'run-bind',fixture.actor,{'id':'run','receipt':ref})
                with (boundary or contextlib.nullcontext)():fixture.fail('Stale native read must not be admitted')
        observed=runtime.Runtime(self.root,adapter_factory=RacingReader).readresult(
            run_id='run',config_path='runtime.json',role='executor',evidence_home=str(self.home))
        current=core.read_run(self.root,'run')
        self.assertEqual(observed['source'],'concurrent_original_turn_advanced')
        self.assertEqual(observed['native_identity']['turn_id'],'turn-two')
        self.assertEqual(observed['receipt'],current['latest_receipt'])
        self.assertEqual(current['outcome'],'running')

    def test_tampered_retained_receipt_fails_without_host(self):
        result = self.execute()
        (self.root / result['receipt']['path']).write_text('changed')
        with self.assertRaisesRegex(runtime.RuntimeError, 'receipt changed'):
            runtime.Runtime(self.root, adapter_factory=lambda *_: self.fail('No host allowed')).readresult(
                run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))

    def test_pause_before_launch_prevents_host_creation(self):
        core.mutate(self.root, 'pause', self.actor, {'id': 'pause', 'task_id': 'task', 'expected_task_revision': core.read_task(self.root, 'task')['task']['revision'],
                    'evidence_ref': self.evidence, 'summary': 'user pause'})
        with self.assertRaises(core.WorkConflict):
            self.execute()
        self.assertEqual(self.adapters, [])

    def test_current_pause_between_identity_and_turn_is_rechecked(self):
        def pause():
            core.mutate(self.root, 'pause', self.actor, {'id': 'pause', 'task_id': 'task', 'expected_task_revision': core.read_task(self.root, 'task')['task']['revision'],
                        'evidence_ref': self.evidence, 'summary': 'user pause after thread creation'})
        self.factory = lambda root, config: CompletedAdapter(root, config, pause=pause)
        with self.assertRaises(core.WorkConflict):
            self.execute()
        self.assertEqual(core.read_run(self.root, 'run')['outcome'], 'unknown')

    def test_changed_request_input_cannot_be_substituted(self):
        different = self.home / 'different.json'
        different.write_text('{}')
        with self.assertRaisesRegex(runtime.RuntimeError, 'explicitly selected original instruction'):
            self.execute(input_path=str(different), input_sha256=runtime.digest(different.read_bytes()))
        self.assertEqual(self.adapters, [])

    def test_resume_keeps_exact_previous_native_identity(self):
        self.execute()
        self.execute(run_id='next-turn', operation='resume', previous_run_id='run')
        self.assertEqual(self.adapters[-1].kwargs['previous']['id'], 'actual-child')
        self.assertEqual(core.read_run(self.root, 'next-turn')['request_id'], 'request')

    def test_resume_uses_selected_model_without_changing_native_session(self):
        self.execute()
        value = configuration()
        value['roles']['executor']['model'] = 'new-model'
        self.config_path.write_text(json.dumps(value))
        result=self.execute(run_id='next-turn', operation='resume', previous_run_id='run')
        self.assertEqual(result['outcome'],'succeeded')
        self.assertEqual(self.adapters[-1].kwargs['previous']['id'],'actual-child')
        self.assertEqual(self.adapters[-1].config['model'],'new-model')

    def test_stop_request_is_distinct_from_unconfirmed_native_stop(self):
        # Keep an observed in-flight original identity for stop reconciliation.
        config = runtime.load_config(self.root, 'runtime.json', 'executor')
        self.core_intent(config)
        native = runtime.identity('codex', 'thread', 'original', self.root, 'turn')
        receipt_path = self.home / 'observed.json'
        receipt_path.write_text(json.dumps({'schema': runtime.OBSERVATION_SCHEMA, 'source': 'native_rpc',
            'run_id': 'run', 'request_id': 'request',
            'host': 'codex', 'native_identity': native, 'config_sha256': config['config_ref']['sha256'],
            'native_status': 'inProgress', 'observed_at': runtime.now()}))
        core.mutate(self.root, 'run-bind', self.actor, {'id': 'run', 'native_identity': native, 'receipt': self.ref(receipt_path)})
        stopped = runtime.Runtime(self.root, adapter_factory=self.factory).stop(
            run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
        self.assertTrue(stopped['stop_requested'])
        self.assertFalse(stopped['stop_confirmed'])
        self.assertTrue(core.read_run(self.root, 'run')['stop_requested'])
        self.assertEqual(core.read_run(self.root, 'run')['outcome'], 'unknown')

    def test_control_stop_preserves_unknown_outcome_without_creating_adapter_failure(self):
        case = self

        class StoppingAdapter(CompletedAdapter):
            def execute(self, text, observe, guard, **kwargs):
                core.mutate(case.root, 'run-stop', case.actor, {'id': 'run', 'summary': 'Original owner stop'})
                guard()
                raise AssertionError('Stopped work must not continue')

        self.factory = lambda root, config: StoppingAdapter(root, config)
        with self.assertRaises(runtime.StopRequested):
            self.execute()
        run = core.read_run(self.root, 'run')
        self.assertEqual(run['outcome'], 'unknown')
        self.assertTrue(run['stop_requested'])
        receipt = runtime.read_receipt(self.root, run['latest_receipt'])
        self.assertEqual(receipt['control_stop_run_id'], 'run')
        with core._connect(self.root, True) as c:
            self.assertFalse(c.execute("SELECT 1 FROM work_requests WHERE id='runtime-friction:run'").fetchone())

    def test_unrecorded_control_stop_does_not_hide_a_runtime_failure(self):
        class UnrecordedStop(CompletedAdapter):
            def execute(self, text, observe, guard, **kwargs):
                raise runtime.StopRequested('Unsubstantiated stop', 'run')

        self.factory = lambda root, config: UnrecordedStop(root, config)
        with self.assertRaises(runtime.StopRequested):
            self.execute()
        with core._connect(self.root, True) as c:
            self.assertTrue(c.execute("SELECT 1 FROM work_requests WHERE id='runtime-friction:run'").fetchone())

    def test_terminal_stop_preserves_success_without_starting_observer(self):
        self.execute()
        result = runtime.Runtime(self.root, adapter_factory=lambda *_: self.fail('No host for terminal stop')).stop(
            run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
        self.assertTrue(result['native_terminal'])
        self.assertEqual(core.read_run(self.root, 'run')['outcome'], 'succeeded')

    def test_same_runtime_stops_original_live_handle_and_confirms_exact_turn(self):
        ready, stopped = threading.Event(), threading.Event()
        created, errors = [], []
        native = runtime.identity('codex', 'thread', 'original-live', self.root, 'live-turn')
        class Waiting(CompletedAdapter):
            def execute(adapter, text, observe, guard, **kwargs):
                with kwargs['boundary']():
                    guard()
                observe(native, 'inProgress', {})
                ready.set()
                if not stopped.wait(3):
                    raise AssertionError('Original stop was not delivered')
                return {'native_identity': native, 'native_status': 'interrupted', 'stop_confirmed': True}
            def stop(adapter, actual, *, boundary=None):
                self.assertEqual(actual, native)
                stopped.set()
                return {'native_identity': actual, 'native_status': 'interrupted', 'stop_confirmed': True}
        def factory(root, config):
            adapter = Waiting(root, config)
            created.append(adapter)
            return adapter
        controller = runtime.Runtime(self.root, adapter_factory=factory)
        def run():
            try:
                controller.execute(config_path='runtime.json', role='executor', task_id='task', request_id='request',
                    run_id='run', plan_revision=1, input_path=str(self.input_path), input_sha256=self.input_ref['sha256'],
                    evidence_home=str(self.home))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(ready.wait(3))
            result = controller.stop(run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
            self.assertTrue(result['stop_confirmed'])
        finally:
            stopped.set()
            worker.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(created), 1)
        self.assertEqual(core.read_run(self.root, 'run')['outcome'], 'stopped')

    def test_raw_final_keeps_native_success_and_exposes_pending_delivery(self):
        class Raw(CompletedAdapter):
            def execute(self, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                result['result'] = 'Native finished; no exact artifacts delivered.'
                return result
        self.factory = lambda root, config: Raw(root, config)
        result = self.execute()
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['management_return']['status'], 'registered')
        self.assertEqual(core.read_run(self.root, 'run')['delivery_status'], 'returned')

    def test_return_registration_failure_recovers_without_rerunning_worker(self):
        original_mutate = core.mutate
        def fail_registration(root, action, actor, payload):
            if action == 'return':
                raise ValueError('Fixture interrupted management registration')
            return original_mutate(root, action, actor, payload)
        with patch.object(core, 'mutate', side_effect=fail_registration):
            result = self.execute()
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['management_return']['status'], 'index_pending')
        recovered = runtime.Runtime(self.root, adapter_factory=lambda *_: self.fail('No worker rerun')).readresult(
            run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
        self.assertEqual(recovered['management_return']['status'], 'registered')
        self.assertEqual(recovered['receipt'], result['receipt'])



    def test_material_changed_during_native_run_cannot_establish_delivery(self):
        material = self.home / 'original-material.json'
        material.write_text('{"value":1}')
        ref = self.ref(material)
        class ChangedMaterial(CompletedAdapter):
            def execute(adapter, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                material.write_text('{"value":2}')
                return result
        self.factory = lambda root, config: ChangedMaterial(root, config)
        with patch('harness_package.input_status', return_value={'available':[ref], 'unavailable':[]}):
            result = self.execute()
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['management_return']['status'], 'registered')

        receipt = runtime.read_receipt(self.root, result['receipt'])
        self.assertEqual(receipt['result']['input_issues'][0]['source_ref'], ref)

    def test_disabled_config_never_persists_run_or_constructs_adapter(self):
        value = configuration()
        value['hosts']['codex']['enabled'] = False
        self.config_path.write_text(json.dumps(value))
        with self.assertRaises(runtime.CapabilityUnavailable):
            self.execute()
        self.assertEqual(self.adapters, [])
        self.assertEqual(core.read_task(self.root, 'task')['runs'], [])

    def test_claude_requested_uuid_is_saved_before_native_start(self):
        value = configuration()
        value['roles']['executor'].update(host='claude', provider='claude', native_policy={'claude_tools':['Read'], 'claude_allowed_tools':['Read'], 'filesystem_boundary':'host-native-permissions', 'network_boundary':'host-native-permissions'})
        self.config_path.write_text(json.dumps(value))
        checked = []
        class ClaudeFixture(CompletedAdapter):
            def execute(adapter, text, observe, guard, **kwargs):
                row = core.read_run(self.root, 'run')
                from harness_package import launch_spec
                planned = launch_spec(self.root, row)
                self.assertEqual(planned['run_id'], row['id'])
                self.assertEqual(planned['request_id'], row['request_id'])
                self.assertEqual(planned['requested_session_id'], kwargs['requested_session_id'])
                self.assertEqual(planned['identity_status'], 'requested_only_not_native_observed')
                self.assertIsNone(row['native_identity'], 'Requested identity is not yet native observed')
                checked.append(True)
                native = runtime.identity('claude_code', 'session', kwargs['requested_session_id'], self.root)
                observe(native, 'completed', {})
                return {'native_identity': native, 'native_status': 'completed', 'result': 'raw fixture final'}
        self.factory = lambda root, config: ClaudeFixture(root, config)
        result = self.execute()
        self.assertEqual(checked, [True])
        self.assertEqual(result['native_identity']['host'], 'claude_code')

    def core_intent(self, config):
        core.mutate(self.root, 'run-intent', self.actor, {'id': 'run', 'request_id': 'request', 'plan_revision': 1,
            'config_ref': config['config_ref'], 'input_refs': [self.input_ref], 'target_host': 'codex', 'operation': 'start'})

    def prepare_review(self):
        self.execute()
        author = core.read_run(self.root, 'run')['native_identity']
        original = self.home / 'original-evidence.txt'
        original.write_text('Original quantitative evidence: reference output 1 kg; source table 3.')
        package = self.home / 'review.json'
        artifact_refs = core.current_view(self.root, task_id='task')['tasks'][0]['returns'][0]['artifact_refs']
        package.write_text(json.dumps({'schema': 'data-workspace.review-input.v1',
            'files': [self.ref(original)], 'review_scope': 'Check physical reference and source table 3.',
            'author_run_id': 'run', 'authors': [author], 'artifact_refs': artifact_refs, 'evidence_refs': [self.ref(original)]}))
        self.create_request(id='review', kind='review', independent=True, input_ref=self.ref(package), parent_request_id='request')
        return author, package



    def test_native_spawn_failures_share_exact_not_started_receipts(self):
        for host, adapter in [('codex', 'codex-app-server'), ('claude', 'claude-code-print')]:
            with self.subTest(host=host):
                config = configuration()
                config['hosts'][host]['binary'] = sys.executable
                config['roles']['executor']['host'] = host
                config['roles']['executor']['provider'] = 'native' if host == 'codex' else 'claude'
                if host == 'claude':
                    config['roles']['executor']['native_policy'] = {'claude_tools':['Read'], 'claude_allowed_tools':['Read'], 'filesystem_boundary':'host-native-permissions', 'network_boundary':'host-native-permissions'}
                config_path = self.home / (host + '-runtime.json')
                config_path.write_text(json.dumps(config))
                request_id = host + '-request'
                self.create_request(id=request_id)
                worker = runtime.Runtime(self.root)
                with patch.object(runtime.subprocess, 'Popen', side_effect=FileNotFoundError):
                    with self.assertRaises(runtime.CapabilityUnavailable):
                        worker.execute(config_path=str(config_path), role='executor', task_id='task', request_id=request_id,
                            run_id=host + '-not-started', plan_revision=1, input_path=str(self.input_path),
                            input_sha256=self.input_ref['sha256'], evidence_home=str(self.home))
                run = core.read_run(self.root, host + '-not-started')
                self.assertEqual(run['target_host'], runtime.ADAPTER_HOSTS[adapter])
                self.assertEqual(run['outcome'], 'failed')
                self.assertIsNone(run['native_identity'])
                self.assertTrue(worker.confirmed_start_rejection(run))







class FakeRPC:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.calls = []
        self.on_event = None
        self.status = 'completed'
        self.not_loaded = False

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'mcpServerStatus/list':
            return {'data': []}
        if method in ('thread/start', 'thread/resume'):
            return {'thread': {'id': 'thread', 'cwd': str(self.root)}, 'cwd': str(self.root),
                    'model': self.config['model'], 'modelProvider': self.config['provider']['codex_model_provider'],
                    'activePermissionProfile': {'id': params.get('permissions')}}
        if method == 'turn/start':
            return {'turn': {'id': 'turn', 'status': self.status}}
        if method == 'thread/read':
            return {'thread': {'id': params['threadId'], 'cwd': str(self.root),
                               'status': {'type': 'notLoaded' if self.not_loaded else 'idle'}}}
        if method == 'thread/turns/list':
            return {'data': [{'id': 'turn', 'status': self.status}]}
        if method == 'thread/items/list':
            return {'data': [{'item': {'type': 'agentMessage', 'text': 'native final'}}]}
        if method == 'turn/interrupt':
            self.status = 'interrupted'
            self.on_event({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'interrupted'}}})
            return {}
        raise AssertionError(method)


class AdapterTests(unittest.TestCase):
    def test_claude_program_stream_keeps_backend_and_sanitized_tool_error(self):
        session = 'native-session'
        events = [
            {'type': 'system', 'subtype': 'init', 'cwd': '/isolated/role', 'skills': ['source-skill']},
            {'type': 'assistant', 'message': {'model': 'DeepSeek-V4.1-Flash', 'content': [
                {'type': 'tool_use', 'id': 'tool-1', 'name': 'Read', 'input': {'file_path': 'sensitive-path'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tool-1',
                'is_error': True, 'content': 'Permission denied: private tool message'}]}},
            {'type': 'result', 'session_id': session, 'result': 'Useful final with a retained limitation',
             'modelUsage': {'claude-sonnet-5[1m]': {}}}]
        result, model = claude_adapter.ClaudeAdapter.native_result('\n'.join(map(json.dumps, events)), session, True)
        self.assertEqual(model, 'DeepSeek-V4.1-Flash')
        failure, = result['_programmatic_failures']
        self.assertEqual((failure['native_item_id'], failure['tool_name'], failure['kind'], failure['error_class']),
                         ('tool-1', 'Read', 'tool_error', 'permission_denied'))
        self.assertNotIn('private tool message', json.dumps(failure))

    def setUp(self):
        self.root = Path('/offline/workspace')
        self.config = {'host': {'binary': 'unused'}, 'model': 'exact-configured',
                       'provider': {'codex_model_provider': 'configured-provider'}, 'effort': 'high',
                       'profile': 'read-only-evidence', 'native_policy': {'sandbox':'read-only', 'network_access':False, 'writable_roots':[], 'claude_tools':['Read'], 'claude_allowed_tools':['Read']}}

    def test_codex_uses_verified_model_provider_rpc_and_exact_native_return(self):
        host = FakeRPC(self.root, self.config)
        adapter = codex_adapter.CodexAdapter(self.root, self.config, host=host)
        observed, guarded = [], []
        result = adapter.execute('frozen', lambda *args: observed.append(args), lambda: guarded.append(True))
        start = next(params for method, params in host.calls if method == 'thread/start')
        self.assertEqual(start['model'], 'exact-configured')
        self.assertEqual(start['modelProvider'], 'configured-provider')
        self.assertEqual(start['sandbox'], 'read-only')
        self.assertTrue(start['config']['features']['shell_tool'])
        self.assertFalse(start['config']['features']['multi_agent'])
        self.assertEqual(result['native_identity']['turn_id'], 'turn')
        self.assertEqual(result['result'], 'native final')
        self.assertEqual(len(guarded), 2)
        self.assertEqual(len(observed), 2)

    def test_codex_read_keeps_late_tool_policy_and_rejects_repeated_cursors(self):
        for issue in ('cursor',):
            with self.subTest(issue=issue):
                host = FakeRPC(self.root, self.config)
                original_call = host.call

                def call(method, params):
                    if method != 'thread/items/list':
                        return original_call(method, params)
                    if 'cursor' not in params:
                        final = {'type': 'agentMessage', 'phase': 'final', 'text': 'Cannot be used without all items'}
                        return {'data': [{'item': final}], 'nextCursor': 'next'}
                    if issue == 'tool':
                        return {'data': [{'item': {'type': 'commandExecution'}}]}
                    return {'data': [], 'nextCursor': 'next'}

                host.call = call
                adapter = codex_adapter.CodexAdapter(self.root, self.config, host=host)
                identity = runtime.identity('codex', 'thread', 'thread', self.root, 'turn')
                expected = runtime.RuntimeError if issue == 'tool' else runtime.HostError
                message = 'tool-free profile' if issue == 'tool' else 'repeated cursor'
                with self.assertRaisesRegex(expected, message):
                    adapter.readresult(identity)
                self.assertEqual([method for method, _ in host.calls], ['thread/read', 'thread/turns/list'])

    def test_codex_wait_beyond_former_elapsed_limit_keeps_original_turn(self):
        host = FakeRPC(self.root, self.config)
        host.status = 'inProgress'
        adapter = codex_adapter.CodexAdapter(self.root, self.config, host=host)
        elapsed = [0]
        guarded = []

        def wait(interval):
            self.assertEqual(interval, 1.0)
            elapsed[0] += 500
            if elapsed[0] <= 1000:
                return False
            host.status = 'completed'
            adapter.event({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed'}}})
            return True

        with patch.object(adapter.done, 'wait', side_effect=wait), \
                patch.object(runtime.time, 'monotonic', side_effect=lambda: elapsed[0]):
            result = adapter.execute('frozen', lambda *_: None, lambda: guarded.append(elapsed[0]))
        self.assertEqual(result['native_status'], 'completed')
        self.assertIn(1000, guarded)
        self.assertFalse(any(method == 'turn/interrupt' for method, _ in host.calls))

    def test_claude_resumes_original_role_cwd_beyond_former_elapsed_limit(self):
        cwd = '/isolated/original-author'
        config = {**self.config, 'programmatic': True, 'native_cwd': cwd}
        adapter = claude_adapter.ClaudeAdapter(self.root, config)
        native = runtime.identity('claude_code', 'session', 'original-session', self.root)
        rows = [{'type': 'system', 'subtype': 'init', 'cwd': cwd},
                {'type': 'assistant', 'message': {'model': config['model'], 'content': []}},
                {'type': 'result', 'session_id': native['id'], 'result': 'completed original'}]
        elapsed = [0]
        guarded = []
        process = unittest.mock.Mock(returncode=0)

        def communicate(text, timeout):
            self.assertEqual(timeout, 1.0)
            self.assertEqual(text, 'original brief' if elapsed[0] == 0 else None)
            elapsed[0] += 500
            if elapsed[0] <= 1000:
                raise subprocess.TimeoutExpired(cmd='offline-native-transport', timeout=timeout)
            return '\n'.join(map(json.dumps, rows)), ''

        process.communicate.side_effect = communicate
        with patch.object(runtime.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(runtime.time, 'monotonic', side_effect=lambda: elapsed[0]):
            result = adapter.execute('original brief', lambda *_: None, lambda: guarded.append(elapsed[0]),
                                     previous=native)
        command = spawn.call_args.args[0]
        self.assertEqual(command[command.index('--resume') + 1], native['id'])
        self.assertEqual(spawn.call_args.kwargs['cwd'], cwd)
        self.assertEqual(result['native_context']['cwd'], cwd)
        self.assertEqual(result['native_status'], 'completed')
        self.assertIn(1000, guarded)
        process.terminate.assert_not_called()

    def test_observer_notloaded_never_proves_stopped(self):
        host = FakeRPC(self.root, self.config)
        host.not_loaded = True
        adapter = codex_adapter.CodexAdapter(self.root, self.config, host=host)
        result = adapter.stop(runtime.identity('codex', 'thread', 'thread', self.root, 'turn'))
        self.assertFalse(result['stop_confirmed'])
        self.assertFalse(any(method == 'turn/interrupt' for method, _ in host.calls))

    def test_original_live_codex_interrupt_requires_terminal_readback(self):
        host = FakeRPC(self.root, self.config)
        host.status = 'inProgress'
        adapter = codex_adapter.CodexAdapter(self.root, self.config, host=host)
        adapter.native_identity = runtime.identity('codex', 'thread', 'thread', self.root, 'turn')
        result = adapter.stop(adapter.native_identity)
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(result['native_status'], 'interrupted')

    def test_claude_start_and_resume_use_explicit_identity_and_no_fork(self):
        adapter = claude_adapter.ClaudeAdapter(self.root, self.config)
        first = adapter.command(session_id='exact-uuid')
        resumed = adapter.command(session_id='exact-uuid', previous=True)
        self.assertIn('--session-id', first)
        self.assertIn('--resume', resumed)
        self.assertNotIn('--fork-session', resumed)
        self.assertEqual(resumed[resumed.index('--model') + 1], 'exact-configured')
        self.assertEqual(resumed[resumed.index('--tools') + 1], 'Read')
        self.assertIn('--safe-mode', resumed)

    def test_claude_result_native_session_mismatch_is_unknown(self):
        adapter = claude_adapter.ClaudeAdapter(self.root, self.config)
        process = unittest.mock.Mock(returncode=0)
        process.communicate.return_value = ('{"type":"result","session_id":"different","result":"done"}', '')
        with patch.object(runtime.subprocess, 'Popen', return_value=process), patch.object(runtime.uuid, 'uuid4', return_value='expected'):
            with self.assertRaisesRegex(runtime.RuntimeError, 'exact requested session'):
                adapter.execute('frozen', lambda *_: self.fail('Identity mismatch cannot bind'), lambda: None)

    def test_claude_historical_stop_and_read_are_explicitly_unavailable(self):
        adapter = claude_adapter.ClaudeAdapter(self.root, self.config)
        native = runtime.identity('claude_code', 'session', 'original', self.root)
        self.assertFalse(adapter.stop(native)['stop_confirmed'])
        with self.assertRaises(runtime.CapabilityUnavailable):
            adapter.readresult(native)

    def test_codex_native_worker_uses_explicit_sandbox_and_single_writer_tools(self):
        config = {**self.config, 'profile': 'native-workspace', 'native_policy': {
            'sandbox': 'workspace-write', 'network_access': False, 'writable_roots': [str(self.root / 'work')]}}
        host = FakeRPC(self.root, config)
        adapter = codex_adapter.CodexAdapter(self.root, config, host=host)
        adapter.execute('exact work and native tools', lambda *_: None, lambda: None)
        start = next(p for m, p in host.calls if m == 'thread/start')
        turn = next(p for m, p in host.calls if m == 'turn/start')
        self.assertTrue(start['config']['features']['shell_tool'])
        self.assertTrue(start['config']['features']['unified_exec'])
        self.assertFalse(start['config']['features']['multi_agent'])
        profile = start['config']['permissions'][start['permissions']]
        self.assertEqual(profile['filesystem'], {str(self.root / 'work'): 'write'})
        self.assertFalse(profile['network']['enabled'])
        self.assertEqual(turn['permissions'], start['permissions'])


    def test_codex_goal_native_continuation_keeps_one_thread_and_one_explicit_start(self):
        config = {**self.config, 'programmatic': True, 'role': 'executor'}
        class GoalHost(FakeRPC):
            def __init__(host, *args):
                super().__init__(*args)
                host.goal_reads = 0
            def call(host, method, params):
                if method == 'thread/goal/get':
                    host.calls.append((method, params))
                    host.goal_reads += 1
                    if host.goal_reads == 1:
                        host.on_event({'method': 'turn/started', 'params': {'threadId': 'thread', 'turn': {'id': 'turn-2', 'status': 'inProgress'}}})
                        host.on_event({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn-2', 'status': 'completed'}}})
                    return {'goal': {'status': 'active' if host.goal_reads == 1 else 'complete'}}
                if method == 'thread/turns/list':
                    return {'data': [{'id': 'turn-2', 'status': 'completed'}, {'id': 'turn', 'status': 'completed'}]}
                result = super().call(method, params)
                if method in ('thread/start', 'thread/resume'):
                    result['reasoningEffort'] = config['effort']
                return result
        host = GoalHost(self.root, config)
        observed = []
        adapter = codex_adapter.CodexAdapter(self.root, config, host=host)
        result = adapter.execute('Finish the original work using your native Goal.',
                                 lambda *args: observed.append(args), lambda: None)
        self.assertEqual(result['native_identity']['turn_id'], 'turn-2')
        self.assertEqual(result['native_goal_observation']['goal']['status'], 'complete')
        self.assertEqual(sum(method == 'turn/start' for method, _ in host.calls), 1)
        self.assertTrue(next(params for method, params in host.calls if method == 'thread/start')['config']['features']['goals'])
        self.assertEqual(observed[-1][2]['previous_turn'], {'turn_id': 'turn', 'terminal': 'completed'})

    def test_claude_native_worker_uses_explicit_allow_patterns_without_bypass(self):
        config = {**self.config, 'profile': 'native-workspace', 'native_policy': {
            'claude_tools': ['Read', 'Bash'], 'claude_allowed_tools': ['Read', 'Bash(python3 bin/context.py *)']}}
        command = claude_adapter.ClaudeAdapter(self.root, config).command(session_id='exact')
        self.assertEqual(command[command.index('--tools') + 1], 'Read,Bash')
        self.assertIn('Bash(python3 bin/context.py *)', command[command.index('--allowedTools') + 1])
        self.assertEqual(command[command.index('--permission-mode') + 1], 'dontAsk')
        self.assertNotIn('--dangerously-skip-permissions', command)
        self.assertNotIn('--bg', command)


class FakeReviewService:
    def request_review(self, args):
        return {'state': 'requested', 'review_id': 'offline-review', 'args': args}

    def result(self, args, *, cancelled=lambda: False):
        return {'state': 'received', 'review_id': args.get('review_id'), 'wait_cancelled': cancelled()}

    def report_finding(self, args):
        return {'state': 'reported', 'finding_id': args.get('finding_id', 'new')}


class ControlledToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path('/offline/workspace')
        self.config = {'host': {'binary': 'unused'}, 'model': 'exact-configured',
                       'provider': {'codex_model_provider': 'configured-provider'},
                       'profile': 'read-only-evidence', 'native_policy': {'sandbox': 'read-only', 'network_access': False, 'writable_roots': []}}

    def test_shared_dispatch_preserves_rejection_and_error_semantics(self):
        tools = collaboration_tools.ControlledTools(self.root, self.root, check_available=True)
        self.assertEqual(tools.dispatch('request_review', '{}'), {'accepted': False, 'diagnostic': None})
        self.assertEqual(tools.dispatch('not_a_controlled_tool', '{}'), {'accepted': False, 'diagnostic': None})
        tools.review_service = FakeReviewService()
        reply = tools.dispatch('review_result', json.dumps({'review_id': 'original-review'}))
        self.assertEqual(reply, {'accepted': True, 'diagnostic': {'state': 'received',
                               'review_id': 'original-review', 'wait_cancelled': False}})
        reply = tools.dispatch('local_check', json.dumps({'argv': 'not-a-list'}))
        self.assertFalse(reply['accepted'])
        self.assertEqual(reply['diagnostic']['scope'], 'this local check only')
        self.assertEqual(tools.offered(), ['local_check', 'request_review', 'review_result', 'report_finding'])
        names = {item['name'] for item in tools.declarations()}
        self.assertIn('request_review', names)
        self.assertNotIn('refresh_editor_identity', names)
        schema = next(item for item in tools.declarations() if item['name'] == 'request_review')
        self.assertEqual(schema['type'], 'function')
        self.assertEqual(schema['inputSchema']['required'], ['artifacts', 'question'])

    def test_codex_declares_dynamic_tools_and_returns_shared_envelope(self):
        host = FakeRPC(self.root, self.config)
        adapter = codex_adapter.CodexAdapter(self.root, self.config, host=host)
        adapter.review_service = FakeReviewService()
        self.assertIs(adapter.controlled.review_service, adapter.review_service)
        self.assertEqual(host.on_tool, adapter._tool_call)
        result = adapter.execute('frozen', lambda *_: None, lambda: None)
        self.assertEqual(result['native_status'], 'completed')
        start = next(params for method, params in host.calls if method == 'thread/start')
        declared = {item['name']: item for item in start.get('dynamicTools', [])}
        self.assertIn('request_review', declared)
        self.assertIn('review_result', declared)
        self.assertIn('report_finding', declared)
        self.assertNotIn('refresh_editor_identity', declared)
        reply = host.on_tool({'tool': 'request_review', 'callId': 'c1', 'turnId': 'turn',
                              'arguments': {'artifacts': ['saved.md'], 'question': 'offline scope?'}})
        self.assertTrue(reply['success'])
        body = json.loads(reply['contentItems'][0]['text'])
        self.assertTrue(body['accepted'])
        self.assertEqual(body['diagnostic']['review_id'], 'offline-review')
        reply = host.on_tool({'tool': 'review_result', 'arguments': {'review_id': 'original-review'}})
        self.assertTrue(json.loads(reply['contentItems'][0]['text'])['accepted'])
        reply = host.on_tool({'tool': 'unqualified_shell', 'arguments': {}})
        self.assertFalse(reply['success'])
        self.assertFalse(json.loads(reply['contentItems'][0]['text'])['accepted'])
        reply = host.on_tool({'tool': 'request_review'})  # service remains set; missing args still dispatches
        self.assertTrue(json.loads(reply['contentItems'][0]['text'])['accepted'])

    def test_codex_tool_dispatch_is_rejected_without_service(self):
        host = FakeRPC(self.root, self.config)
        adapter = codex_adapter.CodexAdapter(self.root, self.config, host=host)
        reply = adapter._tool_call({'tool': 'request_review', 'arguments': {}})
        self.assertTrue(reply['success'])
        body = json.loads(reply['contentItems'][0]['text'])
        self.assertFalse(body['accepted'])
        self.assertIsNone(body['diagnostic'])
        reply = adapter._tool_call({'tool': 'refresh_editor_identity'})
        self.assertFalse(json.loads(reply['contentItems'][0]['text'])['accepted'])
        adapter.execute('frozen', lambda *_: None, lambda: None)
        start = next(params for method, params in host.calls if method == 'thread/start')
        self.assertNotIn('request_review', {item['name'] for item in start.get('dynamicTools', [])})
        adapter.close()
        self.assertIsNone(host.on_tool)

    def test_unified_credential_boundary_blocks_session_and_key_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for rel in ('private-session.json', 'manager-session.json', '.secrets/user.session.json',
                        'cert.pem', 'signing.key', '.env.local', '.secrets/token.txt'):
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('secret')
                digest = runtime.digest(b'secret')
                with self.assertRaises(ValueError):
                    core.evidence_ref(root, {'path': rel, 'sha256': digest})
                with self.assertRaises(ValueError):
                    review.scientific_path(root, rel)


class ChildIdentityTests(unittest.TestCase):
    def environment(self):
        return {'CODEX_THREAD_ID': 'parent-actor', 'CODEX_TURN_ID': 'parent-turn', 'CLAUDE_SESSION_ID': 'parent-session',
                'ANTHROPIC_API_KEY': 'offline-fixture-value', 'TIANGONG_LCA_SESSION_FILE': 'offline-session-file',
                'PATH': 'offline-tool-path', 'CUSTOM_TOOL_SETTING': 'preserve-setting'}

    def assert_clean(self, received, original):
        self.assertTrue(all(key not in received for key in runtime.PARENT_IDENTITY_ENV))
        self.assertEqual(received, {k: v for k, v in original.items() if k not in runtime.PARENT_IDENTITY_ENV})
        # Attribution records the actual local invoker until its native host
        # supplies a Session id; it never impersonates its parent.
        with patch.dict(os.environ, received, clear=True):
            observed = core.verified_actor(Path('/offline/workspace'))
            self.assertEqual((observed['host'], observed['kind']), ('local', 'process'))
            self.assertNotEqual(observed['id'], 'parent-actor')

    def test_claude_child_cannot_inherit_parent_actor_but_keeps_tool_settings(self):
        original = self.environment()
        config = {'host': {'binary': 'offline-claude'}, 'model': 'offline-model'}
        adapter = claude_adapter.ClaudeAdapter(Path('/offline/workspace'), config)
        process = unittest.mock.Mock(returncode=0)
        process.communicate.return_value = ('{"type":"result","session_id":"observed-child","result":"complete"}', '')
        with patch.dict(os.environ, original, clear=True), patch.object(runtime.subprocess, 'Popen', return_value=process) as spawn:
            adapter.execute('dispatched task evidence', lambda *_: None, lambda: None, requested_session_id='observed-child')
        self.assert_clean(spawn.call_args.kwargs['env'], original)
        self.assertEqual(adapter.native_identity['id'], 'observed-child')

    def test_new_codex_adapter_removes_parent_identity_without_assuming_native_override(self):
        original = self.environment()
        config = {'host': {'binary': 'offline-codex'}, 'model': 'offline-model'}
        with patch.dict(os.environ, original, clear=True), patch.object(codex_adapter, 'CodexHost') as host:
            codex_adapter.CodexAdapter(Path('/offline/workspace'), config)
        self.assert_clean(host.call_args.kwargs['environment'], original)

    def test_codex_transport_environment_is_opt_in_and_legacy_default_stays_unchanged(self):
        def process():
            return unittest.mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
        with patch.object(codex_host.subprocess, 'Popen', side_effect=lambda *a, **kw: process()) as spawn, \
             patch.object(codex_host.CodexHost, 'call', return_value={}), patch.object(codex_host.CodexHost, 'send'):
            native = codex_host.CodexHost(binary='offline-codex')
            self.assertNotIn('env', spawn.call_args.kwargs)
            native.close()
            native = codex_host.CodexHost(binary='offline-codex', environment={'PATH': 'offline-only'})
            self.assertEqual(spawn.call_args.kwargs['env'], {'PATH': 'offline-only'})
            native.close()
            native = codex_host.CodexHost(binary='offline-codex', role_cwd='/isolated/role')
            self.assertEqual(spawn.call_args.kwargs['cwd'], '/isolated/role')
            self.assertNotIn('project_doc_max_bytes=0', spawn.call_args.args[0])
            native.close()


if __name__ == '__main__':
    unittest.main()
