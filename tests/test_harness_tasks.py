"""Offline complete-task recovery and responsibility tests; no host qualification."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import harness_tasks as tasks
from test_agent_runtime import configuration


class TaskCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID': 'main'})
        self.env.start()
        self.main, self.lead, self.worker, self.other, self.reviewer = [self.actor(n) for n in ('main', 'lead', 'worker', 'other', 'reviewer')]
        self.evidence = self.save('authority', {'offline_test_only': True})
        tasks.initialize(self.root, self.main, self.evidence, 'fixture-v3')
        for role, actor in [('task_lead', self.lead), ('executor', self.worker), ('executor', self.other), ('executor', self.reviewer)]:
            self.call(self.main, 'grant-role', {'id': 'grant-' + actor['id'], 'actor': actor,
                      'org_role': role, 'evidence_ref': self.evidence})
        self.brief = self.save('brief', {'goal': 'Complete two declared members with evidence'})
        self.plan = self.save('plan-1', {'route': 'Original complete task plan'})
        self.call(self.main, 'create', {'id': 'task', 'owner_actor': self.lead,
                  'original_ref': self.brief, 'plan_ref': self.plan, 'summary': 'Bounded complete task', 'required_scope': ['a', 'b']})

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def actor(self, name):
        return {'host': 'codex', 'kind': 'thread', 'id': name, 'instance': name, 'cwd': str(self.root)}

    def save(self, name, document):
        path = self.root / (name + '.json')
        path.write_text(json.dumps(document), encoding='utf-8')
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def call(self, actor, action, body):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': actor['id']}):
            return tasks.mutate(self.root, action, actor, body)

    def request(self, ident='request', scope=None, **extra):
        return self.call(self.lead, 'request', {'id': ident, 'task_id': 'task', 'kind': 'plan',
                         'recipient': self.worker, 'recipient_role': 'executor', 'input_ref': self.brief,
                         'summary': 'Execute original necessary scope', 'plan_revision': 1,
                         'required_scope': scope or ['a', 'b'], **extra})

    def returned(self, ident='return', request_id='request', coverage=None, remaining=None, **extra):
        ref = self.save('output-' + ident, {'contribution': ident})
        artifact = {'id': ident, 'version': 'v1', **ref}
        return self.call(self.worker, 'return', {'id': ident, 'request_id': request_id, 'result_ref': ref,
                         'artifact_refs': [artifact], 'coverage': coverage or ['a', 'b'],
                         'remaining_scope': [] if remaining is None else remaining,
                         'summary': 'Actual result and stated limits', **extra})

    def dispose(self, ident='decision', returned='return', scope=None, remaining=None, **extra):
        snapshot = tasks.read_task(self.root, 'task')
        return self.call(self.lead, 'dispose', {'id': ident, 'return_id': returned, 'disposition': 'adopt',
                         'scope': ['a', 'b'] if scope is None else scope, 'remaining_scope': [] if remaining is None else remaining,
                         'responsible_actor': self.lead, 'evidence_ref': self.evidence,
                         'summary': 'Accepted for the declared task use',
                         'expected_task_revision': snapshot['task']['revision'],
                         'plan_revision': snapshot['task']['plan_revision'], **extra})

    def test_read_does_not_initialize_missing_or_legacy_database(self):
        fresh = self.root / 'absent'
        self.assertEqual(tasks.current_view(fresh)['status'], 'UNCONFIGURED')
        self.assertFalse((fresh / '.control').exists())
        legacy = self.root / 'legacy'
        (legacy / '.control').mkdir(parents=True)
        c = sqlite3.connect(legacy / '.control/harness.sqlite')
        c.execute('CREATE TABLE keep_original(id TEXT)')
        c.commit(); c.close()
        self.assertEqual(tasks.current_view(legacy)['status'], 'UNCONFIGURED')
        c = sqlite3.connect(legacy / '.control/harness.sqlite')
        self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('keep_original',)])
        c.close()

    def test_successor_chat_records_explicit_plan_change_without_new_enrollment(self):
        self.call(self.main, 'grant-role', {'id': 'maintainer-role', 'actor': self.other,
                  'org_role': 'maintainer', 'evidence_ref': self.evidence})
        updated = self.save('current-plan', {'goal': 'Original complete goal; current responsibilities only'})
        payload = {'id': 'plan-edit', 'task_id': 'task', 'expected_task_revision': 1,
                   'plan_ref': updated, 'evidence_ref': updated, 'summary': 'Consolidate the current plan'}
        self.call(self.other, 'revise', payload)
        snapshot = tasks.read_task(self.root, 'task')
        self.assertEqual(snapshot['task']['owner_actor'], self.lead)
        self.assertEqual(snapshot['task']['sponsor_actor'], self.main)
        self.assertEqual(snapshot['task']['state'], 'active')
        self.assertEqual(snapshot['task']['required_scope'], ['a', 'b'])
        self.assertEqual(snapshot['task']['plan_ref'], updated)
        event = next(e for e in snapshot['events'] if e['kind'] == 'revise')
        self.assertEqual(event['actor'], self.other)
        self.assertEqual(event['payload']['evidence_ref'], updated)
        pending=self.call(self.other, 'complete', {**payload, 'id': 'explicit-completion', 'expected_task_revision': 2})
        self.assertEqual(pending['status'],'completion_pending')
        self.assertEqual(tasks.read_task(self.root, 'task')['task']['state'], 'active')

    def test_explicit_init_keeps_original_legacy_records_and_is_idempotent(self):
        c = sqlite3.connect(self.root / '.control/harness.sqlite')
        c.execute('CREATE TABLE handoffs(id,kind,role,owner,input_path,input_hash,summary,state,result_path,result_hash,created_at,updated_at)')
        c.execute("INSERT INTO handoffs VALUES ('old','review','data','coordinator','original','hash','legacy owner','claimed',NULL,NULL,'then','then')")
        c.commit(); c.close()
        tasks.initialize(self.root, self.main, self.evidence, 'fixture-v3')
        c = sqlite3.connect(self.root / '.control/harness.sqlite')
        self.assertEqual(c.execute("SELECT state,input_path FROM handoffs WHERE id='old'").fetchone(), ('claimed', 'original'))
        self.assertEqual(c.execute('SELECT count(*) FROM work_roles WHERE org_role=\'global_main\'').fetchone()[0], 1)
        c.close()

    def test_attribution_cannot_impersonate_another_chat_but_enrollment_is_not_permission(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'other'}):
            with self.assertRaisesRegex(tasks.WorkConflict, 'observed caller'):
                tasks.mutate(self.root, 'pause', self.lead, {'id': 'x'})
        self.call(self.other, 'pause', {'id': 'x', 'task_id': 'task', 'expected_task_revision': 1,
                  'evidence_ref': self.evidence, 'summary': 'Explicitly pause the original task'})
        view = tasks.read_task(self.root, 'task')
        self.assertEqual(view['task']['state'], 'paused')
        event = next(e for e in view['events'] if e['kind'] == 'pause')
        self.assertEqual(event['actor'], self.other)
        self.assertEqual(view['task']['owner_actor'], self.lead)

    def test_return_retry_after_lost_response_reuses_exact_version(self):
        self.request()
        first = self.returned()
        # Recover the original saved output rather than rerunning its work.
        payload = {'id': 'return', 'request_id': 'request', 'result_ref': first['result_ref'],
                   'artifact_refs': first['artifact_refs'], 'coverage': ['a', 'b'], 'remaining_scope': [],
                   'summary': 'Actual result and stated limits'}
        self.assertEqual(first, self.call(self.worker, 'return', payload))
        self.assertEqual(first['id'], self.call(self.worker, 'return', {**payload, 'id': 'alternate-registration-id'})['id'])
        payload['summary'] = 'Later observation of the same content'
        self.assertEqual(first['id'], self.call(self.worker, 'return', payload)['id'])
        payload['result_ref'] = self.save('different-result', {'contribution':'Different actual content'})
        with self.assertRaisesRegex(tasks.WorkConflict, 'different author, run or saved result'):
            self.call(self.worker, 'return', payload)
        self.assertEqual(len(tasks.read_task(self.root, 'task')['returns']), 1)

    def test_normal_cli_returns_large_reviewable_work_and_adopts_every_named_use(self):
        (self.root / 'materials').mkdir()
        brief = self.save('materials/large-package-brief', {'goal': 'Check the complete materials batch'})
        self.request(input_ref=brief)
        artifacts = [self.save(f'quantity-check-{index}', {'quantity': index, 'unit': 'kg'})
                     for index in range(34)]
        changes = [f'Material {index}: ' + ''.join(
            f'Row {row}: quantity and unit checked against the source. ' for row in range(650))
            for index in range(len(artifacts))]
        document = {'contribution': 'All material quantity checks are saved. Use the specified source versions.',
                    'coverage': ['a', 'b'], 'changes': changes,
                    'artifacts': [{'id': f'material-{index}', **ref} for index, ref in enumerate(artifacts)],
                    'maintenance_findings': [{'summary': 'Keep the observed input-import defect on the original task.'}]}
        self.assertGreater(len(json.dumps(document).encode()), 1024 * 1024)

        def command(actor, *args, value):
            result = subprocess.run([sys.executable, str(BIN / 'harness.py'), '--root', str(self.root), *args],
                                    input=json.dumps(value), capture_output=True, text=True,
                                    env={**os.environ, 'CODEX_THREAD_ID': actor['id']})
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        returned = command(self.worker, 'work-return', '--request', 'request', '--result', '-', value=document)
        self.assertEqual(len(tasks.finite.saved_object(self.root, returned['result_ref'])['artifacts']), len(artifacts))
        self.assertNotIn('finding_request_ids', returned)
        saved = tasks.finite.saved_object(self.root, returned['result_ref'])
        self.assertEqual(saved['changes'][-1], changes[-1])
        uses = [{'artifact_id': item['id'], 'use': 'Source-bounded quantity check', 'scope': ['a']}
                for item in returned['artifact_refs']]
        judgment = {'decision': 'adopt', 'scope': ['a', 'b'], 'completion_scope': ['a', 'b'],
                    'reason': 'Each checked quantity retains its source-limited intended use.',
                    'conditions': [], 'artifact_uses': uses}
        adopted = command(self.lead, 'work-dispose', '--return-id', returned['id'], '--use', 'adopt', '--decision', '-', value=judgment)
        view = tasks.read_task(self.root, 'task')
        self.assertEqual(view['dispositions'][-1]['disposition'], 'adopt')
        self.assertEqual(tasks.finite.saved_object(self.root, adopted['evidence_ref'])['artifact_uses'], uses)





    def test_pause_gates_launch_but_preserves_late_return(self):
        self.request()
        self.call(self.lead, 'pause', {'id': 'pause', 'task_id': 'task', 'expected_task_revision': 1,
                  'evidence_ref': self.evidence, 'summary': 'User pause recorded'})
        self.returned()
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'lead'}):
            with self.assertRaisesRegex(tasks.WorkConflict, 'paused'):
                tasks.admission(self.root, 'task', self.lead, request_id='request',
                                expected_plan_revision=1, side_effect=True)
        self.assertEqual(tasks.read_task(self.root, 'task')['returns'][0]['request_id'], 'request')
        self.dispose()  # Recording a decision does not execute paused work.






    def test_unknown_start_is_recovered_never_relaunched(self):
        self.request(recipient=None)
        config = self.save('runtime-config', configuration())
        payload = {'id': 'run', 'request_id': 'request', 'plan_revision': 1,
                   'config_ref': config, 'input_refs': [self.brief], 'target_host': 'codex', 'operation': 'start'}
        self.assertTrue(self.call(self.lead, 'run-intent', payload)['may_launch'])
        self.assertFalse(self.call(self.lead, 'run-intent', payload)['may_launch'])
        self.call(self.lead, 'run-outcome', {'id': 'run', 'outcome': 'unknown', 'summary': 'Lost native start response'})
        with self.assertRaisesRegex(tasks.WorkConflict, 'reconcile run'):
            self.call(self.lead, 'run-intent', {**payload, 'id': 'duplicate'})
        native = self.worker | {'turn_id': 'turn-1'}
        receipt = self.save('native', {'schema': 'data-workspace.agent-runtime-observation.v1',
                            'run_id': 'run', 'request_id': 'request',
                            'host': 'codex', 'source': 'native_rpc', 'operation': 'start',
                            'native_identity': native, 'config_sha256': config['sha256'],
                            'native_status': 'running', 'observed_at': '2026-10-29T00:00:00Z'})
        self.call(self.lead, 'run-bind', {'id': 'run', 'native_identity': native, 'receipt': receipt})
        self.assertEqual(tasks.read_run(self.root, 'run')['latest_receipt'], receipt)
        self.call(self.lead, 'run-stop', {'id': 'run', 'summary': 'User stopped'})
        self.assertEqual(tasks.read_run(self.root, 'run')['outcome'], 'running')
        with self.assertRaisesRegex(tasks.WorkConflict, 'confirmed stop'):
            self.call(self.lead, 'run-outcome', {'id': 'run', 'outcome': 'stopped', 'receipt': receipt})

    def test_old_sql_writer_cannot_modify_new_responsibility(self):
        c = sqlite3.connect(self.root / '.control/harness.sqlite')
        with self.assertRaises(sqlite3.OperationalError):
            c.execute("UPDATE work_tasks SET state='completed' WHERE id='task'")
        c.close()


    def test_unscoped_maintenance_finding_survives_scientific_task_completion(self):
        self.call(self.main, 'grant-role', {'id': 'maintainer', 'actor': self.other, 'org_role': 'maintainer',
                  'evidence_ref': self.evidence})
        self.request()
        self.returned()
        self.dispose()
        import harness_scope
        agreement={'purpose':'Original evidence use','endpoint':'evidence',
            'scope_boundary':{'in_scope':'a and b','out_of_scope':'No production data action'},
            'acceptance':{'text':'Original evidence supports the bounded use','refs':[self.brief]},
            'authority_refs':[],'interfaces':{'text':'','refs':[]}}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):
            harness_scope.apply_scope(self.root,'task',{'items':[{'scope_id':sid,'kind':'deliverable','label':sid,'source_refs':[self.brief]} for sid in ('a','b')],
                'verified_scope_ids':['a','b'],'source_ref':self.brief},self.lead)
            with tasks.transaction(self.root) as c:
                harness_scope.set_contract(c,self.root,'task','task',agreement,self.brief,self.lead)
                harness_scope.set_contract(c,self.root,'request','request',agreement,self.brief,self.lead)
        self.dispose(ident='final-evidence',acceptance='final')
        self.call(self.lead, 'request', {'id': 'tool-finding', 'task_id': 'task', 'kind': 'finding',
                  'recipient': self.other, 'recipient_role': 'maintainer', 'input_ref': self.evidence,
                  'summary': 'Shared improvement can continue without changing this qualified result',
                  'plan_revision': 1, 'required_scope': []})
        view = tasks.read_task(self.root, 'task')
        self.assertEqual(view['pending_request_ids'], ['tool-finding'])
        self.assertEqual(view['task']['state'], 'active')
        self.call(self.lead, 'complete', {'id': 'close', 'task_id': 'task', 'expected_task_revision': 1,
                  'plan_revision': 1, 'evidence_ref': self.evidence, 'summary': 'Complete scientific use; maintenance has its own owner'})
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'other'}):
            maintenance = tasks.current_view(self.root, self.other, include_closed=True)['tasks'][0]
        self.assertEqual(maintenance['task']['state'], 'completed')
        self.assertEqual(maintenance['pending_request_ids'], ['tool-finding'])

    def test_atomic_native_boundary_orders_pause_and_blocks_subsequent_start(self):
        self.request(recipient=None)
        config = self.save('atomic-config', configuration())
        self.call(self.lead, 'run-intent', {'id': 'run', 'request_id': 'request', 'plan_revision': 1,
                  'config_ref': config, 'input_refs': [self.brief], 'target_host': 'codex', 'operation': 'start'})
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'lead'}), ThreadPoolExecutor(max_workers=1) as pool:
            started = threading.Event()
            def pause_after_boundary():
                started.set()
                return tasks.mutate(self.root, 'pause', self.lead, {'id': 'pause', 'task_id': 'task',
                                    'expected_task_revision': 1, 'evidence_ref': self.evidence,
                                    'summary': 'User pause ordered after initiated effect'})
            with tasks.dispatch_admission(self.root, 'task', self.lead, request_id='request',
                                          run_id='run', expected_plan_revision=1):
                future = pool.submit(pause_after_boundary)
                self.assertTrue(started.wait(2))
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=5)['state'], 'paused')
            with self.assertRaisesRegex(tasks.WorkConflict, 'paused'):
                with tasks.dispatch_admission(self.root, 'task', self.lead, request_id='request',
                                              run_id='run', expected_plan_revision=1):
                    self.fail('A paused task must not reach the actual native initiation')

    def test_explicit_scope_judgment_and_pause_preserve_who_recorded_them(self):
        self.request()
        self.returned()
        plan = self.save('scope-correction', {'purpose': 'User now requires a only'})
        self.call(self.main, 'revise', {'id': 'scope-correction', 'task_id': 'task', 'expected_task_revision': 1,
                  'plan_ref': plan, 'affected_scope': ['b'], 'required_scope': ['a'],
                  'evidence_ref': self.evidence, 'summary': 'Explicit user use/scope correction'})
        self.dispose(scope=['a'], remaining=['b'])
        snapshot = tasks.read_task(self.root, 'task')
        self.assertEqual(snapshot['task']['required_scope'], ['a'])
        self.assertEqual(snapshot['task']['state'], 'active')
        consumed = self.call(self.main, 'consume', {'id': 'explicit-use', 'return_id': 'return',
                      'evidence_ref': self.evidence, 'summary': 'Read the original result for the declared use'})
        self.assertEqual(consumed['actor'], self.main)
        self.call(self.main, 'pause', {'id': 'pause', 'task_id': 'task', 'expected_task_revision': 2,
                  'evidence_ref': self.evidence, 'summary': 'Sponsor enacts user pause with absent lead'})
        self.assertEqual(tasks.read_task(self.root, 'task')['task']['state'], 'paused')

    def test_transfer_preserves_unknown_attempt_and_allows_current_owner_to_inspect(self):
        self.call(self.main,'grant-role',{'id':'new-lead','actor':self.other,'org_role':'task_lead','evidence_ref':self.evidence})
        self.request()
        config = self.save('transfer-config',configuration())
        self.call(self.lead,'run-intent',{'id':'run','request_id':'request','plan_revision':1,
            'config_ref':config,'input_refs':[self.brief],'target_host':'codex','operation':'start'})
        self.call(self.lead,'run-outcome',{'id':'run','outcome':'unknown','summary':'Original effects remain unknown.'})
        original = tasks.read_run(self.root,'run')
        self.call(self.lead,'transfer',{'id':'transfer','task_id':'task','expected_task_revision':1,
            'new_owner_actor':self.other,'evidence_ref':self.evidence,'summary':'Explicit handoff to the new lead.'})
        current = tasks.read_task(self.root,'task')
        self.assertEqual(current['task']['state'],'active')
        self.assertEqual(current['task']['owner_actor'],self.other)
        self.assertEqual(tasks.read_run(self.root,'run'),original)
        with patch.dict(os.environ,CODEX_THREAD_ID='other'):
            self.assertEqual(tasks.recovery_admission(self.root,'run',self.other)['mode'],'original_run')
        with patch.dict(os.environ,CODEX_THREAD_ID='reviewer'):
            self.assertFalse(tasks.recovery_admission(self.root,'run',self.reviewer)['may_launch'])
        with self.assertRaises(tasks.WorkConflict):
            self.call(self.other,'run-intent',{'id':'replacement','request_id':'request','plan_revision':1,
                'config_ref':config,'input_refs':[self.brief],'target_host':'codex','operation':'start'})
        self.assertEqual(len(tasks.read_task(self.root,'task')['runs']),1)


    def test_recording_an_assignment_does_not_launch_an_agent(self):
        self.request()
        request = self.call(self.worker, 'request', {'id': 'engineering-note', 'task_id': 'task', 'kind': 'finding',
                  'recipient': self.other, 'recipient_role': 'executor', 'input_ref': self.evidence,
                  'summary': 'Original explicit engineering assignment', 'plan_revision': 1, 'required_scope': []})
        self.assertEqual(request['sender_actor'], self.worker)
        self.assertEqual(tasks.read_task(self.root, 'task')['runs'], [])

    def test_harness_command_routes_read_without_bootstrap(self):
        fresh = self.root / 'read-only'
        result = subprocess.run([sys.executable, str(BIN / 'harness.py'), '--root', str(fresh), 'work-list'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'UNCONFIGURED')
        self.assertFalse(fresh.exists())

    def test_retired_management_protocol_is_unavailable_and_list_is_paginated(self):
        self.request()
        self.returned()
        before = sorted(p.name for p in self.root.glob('*.json'))
        payload = {'id': 'stdin-read', 'return_id': 'return', 'evidence_ref': self.evidence,
                   'summary': 'Existing original evidence used without a new action artifact'}
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'lead'}):
            result = subprocess.run([sys.executable, str(BIN / 'harness.py'), '--root', str(self.root),
                                     'work-consume', '--input', '-'], input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice',result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.glob('*.json')), before)
        self.call(self.main, 'create', {'id': 'other-task', 'owner_actor': self.lead,
                  'original_ref': self.brief, 'plan_ref': self.plan, 'summary': 'Separate actual task scope', 'required_scope': ['other']})
        view = tasks.current_view(self.root, limit=1)
        self.assertEqual(len(view['tasks']), 1)
        self.assertEqual(view['pagination']['total'], 2)
        self.assertEqual(view['pagination']['next_offset'], 1)
        self.assertNotIn('events', view['tasks'][0])
        self.assertTrue(view['tasks'][0]['compact'])


if __name__ == '__main__':
    unittest.main()
