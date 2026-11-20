"""Boundary regressions; real original-task readbacks are recorded separately."""
import argparse
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_agent_runtime as fixtures
from test_agent_runtime import CompletedAdapter
import agent_runtime as runtime
import codex_adapter
import harness_tasks as core
import run_worker
import harness_entry
import harness_discovery
import harness_knowledge
import harness_maintenance
import harness_business
import context


class DailyWorkflowTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    ref = fixtures.RuntimeTests.ref
    create_request = fixtures.RuntimeTests.create_request
    execute = fixtures.RuntimeTests.execute




    def test_invalid_optional_skill_metadata_keeps_native_success_and_return(self):
        class OptionalMetadata(CompletedAdapter):
            def execute(adapter, *args, **kwargs):
                value = super().execute(*args, **kwargs)
                value['result'] = json.dumps({'contribution': 'Original result', 'coverage': ['science'],
                    'artifacts': [str(self.input_path)], 'skill_uses': None})
                return value
        self.factory = OptionalMetadata
        result = self.execute()
        self.assertEqual(result['management_return']['status'], 'registered')
        self.assertEqual(core.read_run(self.root, 'run')['outcome'], 'succeeded')
        self.assertEqual(len(core.read_task(self.root, 'task')['returns']), 1)
        self.assertEqual(harness_maintenance.queue(self.root)['findings'], [])

    def test_one_inline_result_and_one_inline_decision_are_idempotent(self):
        self.create_request(id='direct', recipient=self.actor, recipient_role='task_lead')
        body = {'contribution': 'Original usable evidence', 'coverage': ['science'],
                'artifacts': [{'path': str(self.input_path)}], 'remaining_issues': []}
        def stdin(value):
            return io.TextIOWrapper(io.BytesIO(json.dumps(value).encode()))
        with patch('sys.stdin', stdin(body)):
            returned = run_worker.return_result(self.root, self.actor, '-', request_id='direct')
        with patch('sys.stdin', stdin(body)):
            again = run_worker.return_result(self.root, self.actor, '-', request_id='direct')
        self.assertEqual(returned['id'], again['id'])
        decision = {'decision': 'adopt', 'scope': ['science'], 'completion_scope': ['science'], 'reason': 'Exact original supports this local purpose', 'conditions': []}
        with patch('sys.stdin', stdin(decision)):
            adopted = core.dispose_result(self.root, self.actor, returned['id'], '-', disposition='adopt')
        with patch('sys.stdin', stdin(decision)):
            self.assertEqual(core.dispose_result(self.root, self.actor, returned['id'], '-', disposition='adopt')['id'], adopted['id'])
        self.assertEqual(len(list(self.home.glob('result-*'))), 1)
        self.assertEqual(len(list(self.home.glob('decision-*'))), 1)


    def test_native_minimal_final_is_the_return_and_friction_original(self):
        class Minimal(CompletedAdapter):
            def execute(adapter, *args, **kwargs):
                value = super().execute(*args, **kwargs)
                value['result'] = json.dumps({'contribution': 'Bounded original output with remaining tool issue',
                    'coverage': ['science'], 'artifacts': [str(self.input_path)], 'remaining_issues': [],
                    'maintenance_findings': [{'summary': 'Original wrapper failed; exact native receipt retained'}]})
                return value
        self.factory = Minimal
        result = self.execute()
        self.assertEqual(result['management_return']['status'], 'registered')
        returned = core.read_task(self.root, 'task')['returns'][0]
        self.assertNotEqual(returned['result_ref'], result['receipt'])
        original = (self.root / returned['result_ref']['path']).read_text()
        self.assertIn('Bounded original output', original)
        self.assertEqual(core.read_run(self.root, 'run')['latest_receipt'], result['receipt'])
        self.assertEqual(harness_maintenance.queue(self.root)['findings'], [])
        runtime.Runtime(self.root, adapter_factory=lambda *_: self.fail('Must reuse original result')).readresult(
            run_id='run', config_path='runtime.json', role='executor', evidence_home=str(self.home))
        self.assertEqual(len(core.read_task(self.root, 'task')['returns']), 1)
        self.assertEqual(harness_maintenance.queue(self.root)['findings'], [])

    def test_lost_native_reply_has_one_atomic_untriaged_finding(self):
        self.factory = lambda root, config: CompletedAdapter(root, config, fail=True)
        with self.assertRaises(TimeoutError):
            self.execute()
        run = core.read_run(self.root, 'run')
        self.assertEqual(run['outcome'], 'unknown')
        self.assertEqual(json.loads(Path(run['latest_receipt']['path']).read_bytes())['source'], 'adapter_exception')
        queue = harness_maintenance.queue(self.root)['findings']
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['parent_request_id'], 'request')
        self.assertEqual(queue[0]['pickup'], 'unassigned')
        with self.assertRaises(runtime.RuntimeError):
            self.execute()
        self.assertEqual(len(harness_maintenance.queue(self.root)['findings']), 1)

    def test_forged_failure_receipt_cannot_create_a_finding(self):
        self.factory = lambda root, config: CompletedAdapter(root, config, fail=True)
        with self.assertRaises(TimeoutError):
            self.execute()
        forged = self.home / 'unregistered.json'
        forged.write_text(json.dumps({'run_id': 'run', 'request_id': 'request', 'source': 'adapter_exception',
            'config_sha256': self.ref(self.config_path)['sha256']}))
        with self.assertRaisesRegex(ValueError, 'exact original'):
            core.mutate(self.root, 'run-friction', self.actor, {'id': 'runtime-friction:run', 'run_id': 'run',
                'receipt': self.ref(forged), 'summary': 'unregistered claim'})

    def test_business_native_writes_are_confined_and_missing_browser_is_explicit(self):
        value = {'role': 'executor', 'profile': 'native-workspace', 'host': {'adapter': 'codex-app-server'},
                 'native_policy': {'sandbox': 'workspace-write', 'writable_roots': [str(self.root)]}}
        service = runtime.Runtime(self.root)
        request = core.read_request(self.root, 'request')
        selected = service._execution_scope(value, request, '{}', str(self.home))
        self.assertEqual(set(selected['native_policy']['writable_roots']), {str(self.root),str(self.root/'.harness-local/cli-state/tiangong-lca-cli'),str(Path.home()/'.local/state/tiangong-lca/dataset-operations'),str(Path.home()/'.local/state/tiangong-lca/review-operations')})
        self.assertEqual(value['native_policy']['writable_roots'], [str(self.root)])
        with self.assertRaisesRegex(runtime.CapabilityUnavailable, 'browser'):
            service._execution_scope(value, request, '{"required_capabilities":["browser"]}', str(self.home))
        moved = service._execution_scope(value, request, '{}', str(self.root / 'work/other-task'))
        self.assertEqual(moved['native_policy'], selected['native_policy'])
        self.assertEqual(core.read_task(self.root, 'task')['runs'], [])

    def test_package_profile_has_no_implicit_cwd_write_and_requires_native_confirmation(self):
        config = runtime.load_config(self.root, 'runtime.json', 'executor')
        config.update(profile='native-workspace', native_policy={'sandbox': 'workspace-write',
            'network_access': False, 'writable_roots': [str(self.home)]})
        class Confirmed(fixtures.FakeRPC):
            def call(host, method, params):
                response = super().call(method, params)
                if method in ('thread/start', 'thread/resume'):
                    response['activePermissionProfile'] = {'id': params['permissions'], 'extends': ':read-only'}
                return response
        host = Confirmed(self.root, config)
        codex_adapter.CodexAdapter(self.root, config, host=host).execute('bounded', lambda *_: None, lambda: None)
        start = next(p for m, p in host.calls if m == 'thread/start')
        turn = next(p for m, p in host.calls if m == 'turn/start')
        profile = start['config']['permissions'][start['permissions']]
        self.assertEqual(profile, {'extends': ':read-only', 'filesystem': {str(self.home): 'write'}, 'network': {'enabled': False}})
        self.assertNotIn('sandbox', start)
        self.assertNotIn('sandboxPolicy', turn)
        self.assertEqual(turn['permissions'], start['permissions'])
        class Unconfirmed(fixtures.FakeRPC):
            def call(host, method, params):
                result = super().call(method, params)
                result.pop('activePermissionProfile', None)
                return result
        old_host = Unconfirmed(self.root, config)
        with self.assertRaisesRegex(runtime.CapabilityUnavailable, 'no model turn admitted'):
            codex_adapter.CodexAdapter(self.root, config, host=old_host).execute('bounded', lambda *_: None, lambda: None)
        self.assertFalse(any(m == 'turn/start' for m, _ in old_host.calls))

    def test_run_cli_derives_original_arguments_and_same_intent(self):
        def args():
            return argparse.Namespace(action='start', config='runtime.json', org_role=None, run_id=None,
                request_id='request', task_id=None, plan_revision=None, input=None, input_sha256=None, evidence_home=None)
        first = runtime.original_arguments(self.root, args())
        second = runtime.original_arguments(self.root, args())
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.input_sha256, self.input_ref['sha256'])
        self.assertEqual(first.task_id, 'task')
        self.assertEqual(first.evidence_home, str(self.home))





    def test_native_cli_use_is_indexed_and_discovered_from_another_task(self):
        method = self.root / 'bin/flow_unit.py'
        method.parent.mkdir(); method.write_text('print("unit conversion")')
        observed = harness_discovery.observed_commands(self.root, [
            {'type': 'commandExecution', 'id': 'tool-1', 'command': 'python3 -B bin/flow_unit.py --unit kg',
             'cwd': str(self.root), 'status': 'completed', 'exitCode': 0},
            {'type': 'commandExecution', 'command': 'cat bin/flow_unit.py'},
            {'type': 'commandExecution', 'command': 'python3 bin/flow_unit.py; other-command'}])
        self.assertEqual(len(observed), 1)
        self.assertNotIn('command', observed[0])
        class Using(CompletedAdapter):
            def execute(adapter, *args, **kwargs):
                value = super().execute(*args, **kwargs)
                value['tool_observations'] = observed
                return value
        self.factory = Using
        self.execute()
        core.mutate(self.root, 'create', self.actor, {'id': 'other', 'owner_actor': self.actor,
            'original_ref': self.evidence, 'plan_ref': self.evidence, 'summary': 'Other task', 'required_scope': ['science']})
        candidates = harness_knowledge.discover(self.root, 'other', query='flow_unit')['links']
        self.assertTrue(any(r['task_id'] == 'task' and r['relation'] == 'runtime' for r in candidates))
        self.assertTrue(all('recommendation_eligibility' not in r for r in candidates))


if __name__ == '__main__':
    unittest.main()
