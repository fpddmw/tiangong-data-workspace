"""Lifecycle observations and once-only maintenance delivery, without real hosts."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import test_harness_tasks as fixtures
import test_agent_runtime as runtime_fixtures
import harness_tasks as tasks
import harness_hook as hooks
from task_ledger import maintenance
import harness_feedback as feedback
import run_worker
from workspace_queries import discovery


class HostEntryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskCoreTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.request()
        self.f.call(self.f.main, 'grant-role', {'id': 'maint', 'actor': self.f.other,
            'org_role': 'maintainer', 'evidence_ref': self.f.evidence})
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID': 'worker'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def event(self, **changes):
        value = {'hook_event_name': 'PostToolUse', 'session_id': 'worker', 'cwd': str(self.f.root),
                 'turn_id': 'actual-turn', 'tool_use_id': 'actual-tool', 'tool_name': 'Bash',
                 'tool_input': {'command': 'tool --token KEEP-SECRET'},
                 'tool_response': {'exit_code': 3, 'output': 'KEEP-PRIVATE-OUTPUT'}}
        value.update(changes)
        return value

    def pending(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.f.other['id']}):
            return hooks.pending_observations(self.f.root, self.f.other, all_actors=True)

    def triage(self, path, **kwargs):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.f.other['id']}):
            return hooks.link_observation(self.f.root, self.f.other, path, **kwargs)

    def test_observed_failure_is_available_to_maintenance_without_automatic_finding(self):
        self.assertEqual(hooks.handle(self.f.root, self.event()), {})
        self.assertEqual(maintenance.queue(self.f.root)['findings'], [])
        original = Path(self.pending()[0]['source_ref']['path'])
        raw = original.read_text()
        self.assertNotIn('KEEP-SECRET', raw)
        self.assertNotIn('KEEP-PRIVATE-OUTPUT', raw)
        self.assertIn('actual-tool', raw)
        self.triage(original)
        item = maintenance.queue(self.f.root)['findings'][0]
        self.assertEqual(item['task_id'], 'task')
        self.assertEqual(item['parent_request_id'], 'request')
        self.assertIsNone(item['recipient_actor'])
        self.assertEqual(tasks.read_task(self.f.root, 'task')['returns'], [])

    def test_ordinary_maintenance_cli_consumes_retained_observations_after_author_leaves(self):
        hooks.handle(self.f.root, self.event())
        hooks.handle(self.f.root, self.event(tool_use_id='same-failure-later'))
        broken = hooks.receipt_home(self.f.root, self.f.worker) / 'unavailable.json'
        broken.write_text('corrupt original')
        command = [sys.executable, '-B', str(fixtures.BIN / 'harness.py'), '--root', str(self.f.root), 'work-maintenance']
        environment = os.environ | {'CODEX_THREAD_ID': self.f.other['id']}
        def pickup():
            result = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        initial = pickup()
        self.assertEqual(initial['findings'], [])
        observations = initial['tool_observations']
        self.assertEqual(observations['total'], 2)
        pending = next(item for item in observations['items'] if item['status'] == 'observed')
        self.assertEqual(pending['occurrences'], 2)
        self.assertIn('--task task', pending['link_command'])
        self.assertTrue(any(item['status'] == 'unavailable' for item in observations['items']))
        self.triage(pending['source_ref']['path'])
        triaged = pickup()
        self.assertEqual(len(triaged['findings']), 1)
        self.assertEqual(triaged['tool_observations']['total'], 1)
        self.assertEqual(triaged['tool_observations']['items'][0]['receipt'], str(broken))

    def test_retry_and_recurrence_are_grouped_and_use_original_finding(self):
        event = self.event()
        hooks.handle(self.f.root, event)
        hooks.handle(self.f.root, event)
        hooks.handle(self.f.root, self.event(tool_use_id='second-tool', turn_id='later-turn'))
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0]['occurrences'], 2)
        self.triage(self.pending()[0]['source_ref']['path'])
        self.assertEqual(self.pending(), [])
        hooks.handle(self.f.root, self.event(tool_use_id='third-tool', turn_id='later-turn'))
        self.triage(self.pending()[0]['source_ref']['path'])
        queue = maintenance.queue(self.f.root)['findings']
        self.assertEqual(len(queue), 1)
        self.assertEqual(len(queue[0]['observations']), 2)

    def test_cross_task_similarity_requires_confirmation_and_preserves_original_return_points(self):
        f = self.f
        worker = f.actor('different-worker')
        f.call(f.main, 'grant-role', {'id': 'grant-different-worker', 'actor': worker,
            'org_role': 'executor', 'evidence_ref': f.evidence})
        f.call(f.main, 'create', {'id': 'second-task', 'owner_actor': f.lead, 'original_ref': f.brief,
            'plan_ref': f.plan, 'summary': 'Separate original task', 'required_scope': ['a']})
        f.call(f.lead, 'request', {'id': 'second-request', 'task_id': 'second-task', 'kind': 'plan',
            'recipient': worker, 'recipient_role': 'executor', 'input_ref': f.brief,
            'summary': 'A separate exact input', 'plan_revision': 1, 'required_scope': ['a']})
        (f.root / '.control' / 'runtime.json').write_text('{"binding": "same configured runtime"}')
        hooks.handle(f.root, self.event(tool_name='functions.exec_command',
            tool_input={'cmd': f'python3 -B {f.root}/bin/lca_gate.py --rows ORIGINAL-ONE'}))
        with patch.dict(os.environ, {'CODEX_THREAD_ID': worker['id']}):
            hooks.handle(f.root, self.event(session_id=worker['id'], tool_use_id='different-tool',
                tool_input={'command': 'python3 -B bin/lca_gate.py --rows ORIGINAL-TWO'}))
        def run_cli(script, *args, actor=None):
            result = subprocess.run([sys.executable, '-B', str(fixtures.BIN / script),
                '--root', str(f.root), *(['--json'] if script == 'context.py' else []), *args],
                env=os.environ | {'CODEX_THREAD_ID': (actor or f.other)['id']},
                capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        queue = run_cli('harness.py', 'work-maintenance')
        group, = queue['tool_observations']['items']
        self.assertEqual(group['problem']['component'], 'bin/lca_gate.py')
        self.assertEqual(group['exact_group_count'], 2)
        self.assertEqual({(item['task_id'], tuple(item['request_ids'])) for item in group['affected']},
                         {('task', ('request',)), ('second-task', ('second-request',))})
        self.assertEqual(queue['findings'], [])
        details = run_cli('harness_hook.py', 'pending', '--all', '--problem', group['problem_id'])['items']
        by_task = {item['candidates'][0]: item['source_ref']['path'] for item in details}
        first = self.triage(by_task['task'])
        self.assertEqual(self.pending()[0]['existing_findings'],
                         [{'task_id': 'task', 'request_id': first['request_id']}])
        self.assertNotIn('ORIGINAL-TWO', Path(by_task['second-task']).read_text())
        reused = self.triage(by_task['second-task'], finding_id=first['request_id'])
        self.assertEqual(reused['finding_task_id'], 'task')
        self.assertEqual(self.triage(by_task['second-task'], finding_id=first['request_id'])['status'], 'already_triaged')
        finding, = run_cli('harness.py', 'work-maintenance')['findings']
        affected, = finding['observations']
        self.assertEqual(affected['affected']['task_id'], 'second-task')
        self.assertEqual(affected['affected']['request_ids'], ['second-request'])
        self.assertEqual(affected['affected']['original_request_commands'], ['python3 bin/context.py --request second-request'])
        self.assertEqual(self.pending(), [])
        self.assertEqual(tasks.read_task(f.root, 'second-task')['task']['state'], 'active')
        self.assertEqual(tasks.read_task(f.root, 'second-task')['returns'], [])

        reported = f.save('related-task-observation', {'contribution': 'Follow-up at the same affected request',
            'coverage': [], 'artifacts': [], 'maintenance_findings': [
                {'existing_request_id': first['request_id'], 'summary': 'The same confirmed cause persisted'}]})
        business_return = run_cli('harness.py', 'work-return', '--request', 'second-request',
                                  '--result', reported['path'], actor=worker)
        self.assertNotIn('finding_request_ids', business_return)
        f.call(f.lead, 'request', {'id': 'unlinked-request', 'task_id': 'second-task', 'kind': 'plan',
            'recipient': worker, 'recipient_role': 'executor', 'input_ref': f.brief,
            'summary': 'Unrelated request on the same task', 'plan_revision': 1, 'required_scope': ['a']})
        unlinked = f.save('unrelated-task-observation', {'contribution': 'A different original request',
            'coverage': [], 'artifacts': [], 'maintenance_findings': [
                {'existing_request_id': first['request_id'], 'summary': 'The connection still needs confirmation'}]})
        unrelated_return = run_cli('harness.py', 'work-return', '--request', 'unlinked-request',
                                   '--result', unlinked['path'], actor=worker)
        self.assertNotIn('optional_issues', unrelated_return)
        still_one, = run_cli('harness.py', 'work-maintenance')['findings']
        self.assertEqual(still_one['id'], first['request_id'])
        evidence = [entry['result_ref'] for entry in still_one['observations']]
        self.assertNotIn(reported, evidence)
        unrelated_context = run_cli('context.py', '--request', 'unlinked-request', actor=worker)['current']['work']
        self.assertNotIn('related_maintenance', unrelated_context)

        revision = tasks.read_task(f.root, 'second-task')['task']['revision']
        f.call(f.lead, 'pause', {'id': 'pause-second', 'task_id': 'second-task', 'expected_task_revision': revision,
            'evidence_ref': f.evidence, 'summary': 'Preserve the affected original pause'})
        run_cli('harness.py','work-claim-finding','--request',first['request_id'])
        note = f.root / 'repair.md'; note.write_text('Waiting for the original runtime, without resuming science.')
        run_cli('harness.py','work-maintenance-note','--request',first['request_id'],'--note',str(note),'--status','deferred')
        linked, = feedback.related_maintenance(f.root,'second-task',request_ids=['second-request'])
        self.assertEqual(linked['decision']['state'],'deferred')
        self.assertEqual(linked['finding_task_id'],'task')
        self.assertEqual(feedback.related_maintenance(f.root,'second-task',request_ids=['unlinked-request']),[])
        self.assertEqual(tasks.read_task(f.root,'second-task')['task']['state'],'paused')

    def test_same_task_repair_returns_to_first_parent_and_confirmed_sibling_only(self):
        f = self.f
        worker = f.actor('second-affected-worker')
        f.call(f.main, 'grant-role', {'id': 'second-affected-role', 'actor': worker,
            'org_role': 'executor', 'evidence_ref': f.evidence})
        f.call(f.lead, 'request', {'id': 'second-affected', 'task_id': 'task', 'kind': 'plan',
            'recipient': worker, 'recipient_role': 'executor', 'input_ref': f.brief,
            'summary': 'Second affected request under the same task', 'plan_revision': 1, 'required_scope': ['a']})
        hooks.handle(f.root, self.event(tool_input={'command': 'python3 -B bin/lca_gate.py --rows FIRST'},
                                       tool_version='1.2.3'))
        with patch.dict(os.environ, {'CODEX_THREAD_ID': worker['id']}):
            hooks.handle(f.root, self.event(session_id=worker['id'], tool_use_id='other-tool',
                tool_input={'command': 'python3 -B bin/lca_gate.py --rows SECOND'}, tool_version='1.2.3'))
        group, = self.pending()
        self.assertEqual(group['exact_group_count'], 2)
        first = hooks.receipt_home(f.root, f.worker)
        second = hooks.receipt_home(f.root, worker)
        finding_id = self.triage(next(first.glob('*.json')))['request_id']
        self.triage(next(second.glob('*.json')), finding_id=finding_id)
        f.call(f.lead, 'request', {'id': 'unaffected', 'task_id': 'task', 'kind': 'plan',
            'recipient': worker, 'recipient_role': 'executor', 'input_ref': f.brief,
            'summary': 'Unrelated sibling request', 'plan_revision': 1, 'required_scope': ['a']})

        def cli(script, *args, actor=None):
            result = subprocess.run([sys.executable, '-B', str(fixtures.BIN / script), '--root', str(f.root),
                *(['--json'] if script == 'context.py' else []), *args],
                env=os.environ | {'CODEX_THREAD_ID': (actor or f.other)['id']}, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        cli('harness.py','work-claim-finding','--request',finding_id)
        note = f.root / 'shared-repair.md'; note.write_text('Verified the shared fix at the original entry.')
        cli('harness.py','work-maintenance-note','--request',finding_id,'--note',str(note),'--status','verified')
        for req in ('request','second-affected'):
            item, = feedback.related_maintenance(f.root,'task',request_ids=[req])
            self.assertEqual(item['request_id'],finding_id)
            self.assertEqual(item['decision']['state'],'verified')
        self.assertEqual(feedback.related_maintenance(f.root,'task',request_ids=['unaffected']),[])

    def test_native_known_component_does_not_copy_command_arguments(self):
        failures = discovery.observed_failures([
            {'id': 'with-component', 'type': 'commandExecution', 'exitCode': 4,
             'command': 'python3 -B bin/lca_gate.py --rows PRIVATE-ORIGINAL'},
            {'id': 'generic', 'type': 'commandExecution', 'exitCode': 4,
             'command': 'unrecognized --token PRIVATE-ORIGINAL'},
            {'id': 'foreign', 'type': 'commandExecution', 'exitCode': 4,
             'command': 'python3 -B /tmp/other-project/bin/lca_gate.py --rows PRIVATE-ORIGINAL'}])
        self.assertEqual(failures[0]['component'], 'bin/lca_gate.py')
        self.assertNotIn('component', failures[1])
        self.assertNotIn('component', failures[2])
        self.assertNotIn('PRIVATE-ORIGINAL', json.dumps(failures))

    def test_changed_runtime_binding_keeps_reproduction_separate_for_triage(self):
        config = self.f.root / '.control' / 'runtime.json'
        config.write_text('{"selected": "v1"}')
        hooks.handle(self.f.root, self.event(tool_version='1.0.0'))
        config.write_text('{"selected": "v2"}')
        hooks.handle(self.f.root, self.event(tool_use_id='second-runtime', tool_version='2.0.0'))
        pending = self.pending()
        self.assertEqual(len(pending), 2)
        bindings = [item['runtime_binding'] for item in pending]
        self.assertEqual({item['reported_tool_version'] for item in bindings}, {'1.0.0', '2.0.0'})
        self.assertEqual(len({item['config_sha256'] for item in bindings}), 2)
        finding = self.triage(pending[0]['source_ref']['path'])
        self.assertEqual(len(self.pending()), 1)
        self.triage(pending[1]['source_ref']['path'], finding_id=finding['request_id'])
        self.assertEqual(len(maintenance.queue(self.f.root)['findings']), 1)
        self.assertEqual(self.pending(), [])

    def test_legacy_receipt_groups_under_explicitly_unknown_runtime(self):
        hooks.handle(self.f.root, self.event())
        original = Path(self.pending()[0]['source_ref']['path'])
        value = json.loads(original.read_bytes())
        value.pop('runtime_binding')
        original.write_text(json.dumps(value))
        hooks.handle(self.f.root, self.event(tool_use_id='known-format-unknown-version'))
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['runtime_binding'], hooks.UNKNOWN_RUNTIME)
        self.assertEqual(pending[0]['occurrences'], 2)

    def test_paused_original_keeps_failure_but_never_resumes(self):
        self.f.call(self.f.lead, 'pause', {'id': 'pause', 'task_id': 'task', 'expected_task_revision': 1,
            'evidence_ref': self.f.evidence, 'summary': 'Keep pause'})
        hooks.handle(self.f.root, self.event())
        self.triage(self.pending()[0]['source_ref']['path'])
        self.assertEqual(maintenance.queue(self.f.root)['findings'][0]['task_state'], 'paused')
        self.assertEqual(tasks.read_task(self.f.root, 'task')['task']['state'], 'paused')

    def test_ambiguous_task_is_retained_and_requires_exact_routing(self):
        f = self.f
        f.call(f.main, 'create', {'id': 'other-task', 'owner_actor': f.lead, 'original_ref': f.brief,
            'plan_ref': f.plan, 'summary': 'Different task', 'required_scope': ['a']})
        f.call(f.lead, 'request', {'id': 'other-request', 'task_id': 'other-task', 'kind': 'plan',
            'recipient': f.worker, 'recipient_role': 'executor', 'input_ref': f.brief,
            'summary': 'Other scope', 'plan_revision': 1, 'required_scope': ['a']})
        hooks.handle(f.root, self.event())
        pending = self.pending()[0]
        self.assertEqual(len(pending['candidates']), 2)
        self.assertEqual(self.triage(pending['source_ref']['path'])['status'], 'needs_task')
        self.assertEqual(maintenance.queue(f.root)['findings'], [])
        self.triage(pending['source_ref']['path'], task_id='task')
        self.assertEqual(self.pending(), [])
        self.assertEqual(maintenance.queue(f.root)['findings'][0]['task_id'], 'task')

    def test_dismiss_records_ordinary_invocation_without_finding(self):
        hooks.handle(self.f.root, self.event())
        ref = self.pending()[0]['source_ref']
        self.triage(ref['path'], dismiss='Original tool invocation corrected by its author')
        self.assertEqual(self.pending(), [])
        self.assertEqual(maintenance.queue(self.f.root)['findings'], [])

    def test_closed_history_does_not_route_a_new_unrelated_failure(self):
        f = self.f
        with tasks.transaction(f.root) as c:
            c.execute("DELETE FROM work_requests WHERE id='request'")
            c.execute("UPDATE work_tasks SET state='cancelled' WHERE id='task'")
        with patch.dict(os.environ, {'CODEX_THREAD_ID': f.lead['id']}):
            hooks.handle(self.f.root, self.event(session_id=f.lead['id']))
        self.assertEqual(self.pending()[0]['candidates'], [])
        self.assertEqual(maintenance.queue(f.root)['findings'], [])

    def test_unreadable_database_retains_observation_before_routing(self):
        with patch.object(tasks, '_connect', side_effect=sqlite3.DatabaseError('original database unavailable')):
            hooks.handle(self.f.root, self.event())
        pending = self.pending()[0]
        self.assertEqual(pending['candidates'], [])
        self.assertTrue(Path(pending['source_ref']['path']).is_file())

    def test_locked_database_keeps_receipt_within_host_timeout(self):
        with sqlite3.connect(self.f.root / '.control/harness.sqlite', isolation_level=None) as locked:
            locked.execute('PRAGMA journal_mode=DELETE')
            locked.execute('BEGIN EXCLUSIVE')
            try:
                self.assertEqual(hooks.handle(self.f.root, self.event()), {})
            finally:
                locked.execute('ROLLBACK')
        pending = self.pending()[0]
        self.assertEqual(pending['candidates'], [])
        self.assertTrue(Path(pending['source_ref']['path']).is_file())

    def test_receipt_directory_cannot_escape_through_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.f.root / '.harness-local').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                hooks.handle(self.f.root, self.event())
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_parent_or_foreign_identity_does_not_write(self):
        for event in [self.event(session_id='main'), self.event(agent_id='child'), self.event(cwd='/other')]:
            with self.assertRaises(ValueError):
                hooks.handle(self.f.root, event)
        self.assertFalse((self.f.root / '.harness-local').exists())

    def test_expected_search_and_success_do_not_create_findings(self):
        for event in [self.event(tool_response={'exit_code': 0}),
                      self.event(tool_input={'command': 'rg missing file'}, tool_response={'exit_code': 1}),
                      self.event(tool_response={'session_id': 42, 'output': 'still running'}),
                      self.event(tool_response={'content': [{'type': 'text', 'text': 'The data has scientific errors'}]})]:
            self.assertEqual(hooks.handle(self.f.root, event), {})
        self.assertEqual(maintenance.queue(self.f.root)['findings'], [])
        self.assertIsNotNone(hooks.failed_tool(self.event(tool_response={'isError': True})))
        self.assertIsNotNone(hooks.failed_tool(self.event(tool_response='Process exited with code 2\nerror')))
        self.assertIsNotNone(hooks.failed_tool(self.event(tool_input={'command': ['native-tool']}, tool_response={'exit_code': 1})))

    def test_lifecycle_hooks_do_not_read_or_inject_context(self):
        with patch.object(hooks.subprocess, 'run', side_effect=AssertionError('No context subprocess')):
            for name in ('SessionStart', 'UserPromptSubmit'):
                self.assertEqual(hooks.handle(self.f.root, self.event(hook_event_name=name)), {})
        self.assertFalse(list(self.f.root.rglob('*.cursor')))

    def test_unbound_failure_remains_explicit_without_invented_task(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'unbound'}):
            hooks.handle(self.f.root, self.event(session_id='unbound'))
        self.assertEqual(self.pending()[0]['candidates'], [])
        self.assertEqual(maintenance.queue(self.f.root)['findings'], [])


class MaintenanceReturnTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskCoreTests(methodName='runTest')
        self.f.setUp(); self.addCleanup(self.f.tearDown)
        f = self.f; f.request()
        f.call(f.main, 'grant-role', {'id':'maint','actor':f.other,'org_role':'maintainer','evidence_ref':f.evidence})
        with patch.dict(os.environ, CODEX_THREAD_ID='worker'):
            self.finding = maintenance.report(f.root,f.worker,'task',f.save('finding',{'summary':'Tool failed'})['path'])['id']
        with patch.dict(os.environ, CODEX_THREAD_ID='other'):
            maintenance.claim(f.root,f.other,self.finding)

    def test_report_cannot_close_finding_or_supersede_owner_note(self):
        f = self.f
        old = f.save('legacy', {'maintenance':{'disposition':{'kind':'resolved'}}})
        note = f.root / 'verified.md'; note.write_text('Original caller verified the repaired public entry.')
        with patch.dict(os.environ, CODEX_THREAD_ID='other'):
            returned = run_worker.return_result(f.root,f.other,old['path'],request_id=self.finding)
            self.assertEqual(len(maintenance.queue(f.root)['findings']),1)
            maintenance.record_note(f.root,f.other,self.finding,str(note),'verified')
            run_worker.return_result(f.root,f.other,old['path'],request_id=self.finding)
        self.assertEqual(maintenance.queue(f.root)['findings'],[])
        self.assertEqual(tasks.read_task(f.root,'task')['returns'][0]['id'],returned['id'])

    def test_large_plain_report_is_retained_exactly(self):
        f = self.f
        report = f.root / 'large.md'; original = 'Reproduction and evidence.\n' * 50000
        report.write_text(original)
        with patch.dict(os.environ, CODEX_THREAD_ID='other'):
            returned = run_worker.return_result(f.root,f.other,str(report),request_id=self.finding)
        self.assertEqual(Path(returned['result_ref']['path']).read_text(), original)
        self.assertFalse(returned.get('optional_issues'))
        self.assertEqual(len(maintenance.queue(f.root)['findings']),1)



class NativeFailureTests(unittest.TestCase):
    def setUp(self):
        self.f = runtime_fixtures.RuntimeTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        tasks.mutate(f.root, 'grant-role', f.actor, {'id': 'maint', 'actor': f.actor,
                     'org_role': 'maintainer', 'evidence_ref': f.evidence})

    def test_successful_native_final_cannot_hide_failed_tool(self):
        f = self.f
        class Adapter(runtime_fixtures.CompletedAdapter):
            def execute(self, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                result['tool_failures'] = discovery.observed_failures([
                    {'id': 'failed-tool', 'type': 'commandExecution', 'command': 'native-cli get', 'exitCode': 2}])
                return result
        f.factory = lambda root, config: Adapter(root, config)
        result = f.execute()
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['management_return']['status'], 'registered')
        queue = maintenance.queue(f.root)
        self.assertEqual(queue['findings'], [])
        observation, = queue['tool_observations']['items']
        self.assertEqual(observation['candidates'], ['task'])
        self.assertEqual(observation['native_source']['receipt'], result['receipt'])
        self.assertEqual(observation['native_source']['request_id'], 'request')

    def test_explicit_mcp_and_dynamic_failures_survive_completed_item_status(self):
        values = discovery.observed_failures([
            {'id': 'dynamic', 'type': 'dynamicToolCall', 'status': 'completed', 'success': False},
            {'id': 'mcp', 'type': 'mcpToolCall', 'status': 'completed', 'error': {'message': 'failure'}},
            {'id': 'nested', 'type': 'mcpToolCall', 'status': 'completed', 'result': {'isError': True}},
            {'id': 'ok', 'type': 'dynamicToolCall', 'status': 'completed', 'success': True}])
        self.assertEqual([x['native_item_id'] for x in values], ['dynamic', 'mcp', 'nested'])

    def test_legacy_finding_keeps_old_evidence_and_new_failure_awaits_triage(self):
        f = self.f
        class Adapter(runtime_fixtures.CompletedAdapter):
            def execute(self, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                result.update(native_status='inProgress', tool_failures=[{'native_item_id': 'first', 'kind': 'tool_error'}])
                return result
        f.factory = Adapter
        finding = 'runtime-friction:run'
        with patch.object(runtime_fixtures.runtime.Runtime, '_retain_tool_observations'):
            original = f.execute()
        tasks.mutate(f.root, 'run-friction', f.actor, {'id': finding, 'run_id': 'run',
                     'receipt': original['receipt'], 'summary': 'Existing legacy finding'})
        maintenance.claim(f.root, f.actor, finding)
        path = f.home / 'diagnosis.json'
        path.write_text(json.dumps({'contribution': 'First failure not reproduced', 'coverage': [], 'artifacts': [],
            'maintenance': {'disposition': {'kind': 'not_reproduced', 'rationale': 'Only first failure covered',
                                           'evidence_refs': [original['receipt']]}}}))
        run_worker.return_result(f.root, f.actor, str(path), request_id=finding)
        maintenance.record_note(f.root,f.actor,finding,str(path),'verified')
        self.assertEqual(maintenance.queue(f.root)['findings'], [])
        document = json.loads(Path(original['receipt']['path']).read_bytes())
        result = document['result']
        runner = runtime_fixtures.runtime.Runtime(f.root, adapter_factory=Adapter)
        config = runtime_fixtures.runtime.load_config(f.root, 'runtime.json', 'executor')
        # Different saved snapshot, same explicit failure: preserve closure.
        runner._record_result('run', config, result, str(f.home))
        self.assertEqual(maintenance.queue(f.root)['findings'], [])
        result = {**result, 'native_status': 'completed', 'tool_failures': [*result['tool_failures'],
                  {'native_item_id': 'second', 'kind': 'nonzero_exit', 'exit_code': 2}]}
        terminal = runner._record_result('run', config, result, str(f.home))
        queue = maintenance.queue(f.root)
        self.assertEqual(queue['findings'], [])
        pending, = queue['tool_observations']['items']
        self.assertEqual(pending['native_source']['related_finding_id'], finding)
        ref, observed = hooks.read_observation(f.root, pending['source_ref']['path'])
        self.assertEqual(observed['tool_use_id'], 'second')
        self.assertEqual(hooks.link_observation(f.root, f.actor, ref['path'], finding_id=finding)['request_id'], finding)
        queue = maintenance.queue(f.root)['findings']
        self.assertEqual([x['id'] for x in queue], [finding])
        self.assertEqual(len(queue[0]['observations']), 1)
        self.assertEqual(terminal['management_return']['status'], 'registered')
        runner.readresult(run_id='run', config_path='runtime.json', role='executor', evidence_home=str(f.home))
        self.assertEqual(len(maintenance.queue(f.root)['findings'][0]['observations']), 1)

    def test_retained_terminal_receipt_recovers_interrupted_failure_registration(self):
        f = self.f
        class Adapter(runtime_fixtures.CompletedAdapter):
            def execute(self, *args, **kwargs):
                value = super().execute(*args, **kwargs)
                value['tool_failures'] = [{'native_item_id': 'failed-tool', 'kind': 'tool_error'}]
                return value
        f.factory = Adapter
        with patch.object(hooks, 'retain_observation', side_effect=OSError('Observation storage unavailable')):
            interrupted = f.execute()
        self.assertEqual(interrupted['management_return']['status'], 'registered')
        self.assertEqual(interrupted['maintenance_observations']['status'], 'needs_attention')
        self.assertEqual(tasks.read_run(f.root, 'run')['outcome'], 'succeeded')
        def no_new_host(*args, **kwargs):
            self.fail('Retained terminal result must not open another host')
        runner = runtime_fixtures.runtime.Runtime(f.root, adapter_factory=no_new_host)
        for _ in range(2):
            result = runner.readresult(run_id='run', config_path='runtime.json', role='executor', evidence_home=str(f.home))
            self.assertEqual(result['management_return']['status'], 'registered')
        queue = maintenance.queue(f.root)
        self.assertEqual(queue['findings'], [])
        self.assertEqual(queue['tool_observations']['items'][0]['occurrences'], 1)

    def test_native_maintenance_report_is_original_text_without_a_closure_protocol(self):
        f = self.f
        f.create_request(id='finding', kind='finding', recipient_role='maintainer', required_scope=[])
        class Adapter(runtime_fixtures.CompletedAdapter):
            def execute(self, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                result['result'] = 'Actual native diagnosis. Original caller still needs to verify.'
                return result
        f.factory = lambda root, config: Adapter(root, config)
        result = f.execute(role='maintainer', request_id='finding')
        self.assertEqual(result['management_return']['status'],'registered')
        returned = tasks.read_task(f.root,'task')['returns'][0]
        self.assertEqual(Path(returned['result_ref']['path']).read_text(), 'Actual native diagnosis. Original caller still needs to verify.')
        self.assertEqual(len(maintenance.queue(f.root)['findings']),1)


if __name__ == '__main__':
    unittest.main()
