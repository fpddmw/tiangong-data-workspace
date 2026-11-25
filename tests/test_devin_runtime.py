"""Shared runtime integration; fixtures are not scientific or model evidence."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

import test_agent_runtime as fixtures
import agent_runtime as runtime
import execution_lease as leases
import harness_hosts
import dsh_capacity
from devin_acp import ACPError


class DevinRuntimeTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    create_request = fixtures.RuntimeTests.create_request
    ref = fixtures.RuntimeTests.ref
    execute = fixtures.RuntimeTests.execute

    def select_devin(self):
        document = json.loads(self.config_path.read_text())
        document['programmatic'] = True
        document['hosts']['devin'] = {'adapter': 'devin-acp', 'binary': 'offline-devin', 'enabled': True}
        document['providers']['devin'] = {'selection': 'existing-host-configuration'}
        document['roles']['executor'].update(host='devin', provider='devin', model='swe-2-high',
            profile='native-workspace', native_policy={'sandbox': 'workspace-write', 'network_access': False,
                'writable_roots': [str(self.home)]})
        self.config_path.write_text(json.dumps(document))
        self.factory = lambda root, config: fixtures.CompletedAdapter(root, config,
            author=runtime.identity('devin_cli', 'session', 'fixture-devin', root))

    def test_shared_review_assembly_uses_host_capability_and_no_dsh_slot(self):
        self.select_devin()
        result = self.execute(program_context={})
        run = runtime.Runtime(self.root).core.read_run(self.root, result['run_id'])
        events = [e['payload'] for e in run['events'] if e['kind'] == 'capacity']
        self.assertEqual([e['phase'] for e in events[:2]], ['queued', 'admitted'])
        self.assertTrue(all(Path(e['queue_source']).name == 'devin-capacity.json' for e in events))
        self.assertEqual(result['outcome'], 'succeeded')
        returned = runtime.read_receipt(self.root, result['receipt'])['result']
        self.assertEqual(returned['requested_review_ids'], [])
        self.assertEqual(harness_hosts.HOSTS['devin_cli'].source, 'native_rpc')
        self.assertTrue(harness_hosts.supports('devin_cli', 'author_review'))
        self.assertFalse(harness_hosts.supports('devin_cli', 'native_goal'))
        counts = dsh_capacity.snapshot(self.root)['counts']['occupied']
        self.assertEqual(counts['total'], 0)

    def test_observer_uses_original_host_without_global_rebinding(self):
        self.select_devin()
        result = self.execute(program_context={})
        run = runtime.Runtime(self.root).core.read_run(self.root, result['run_id'])
        selected = runtime.Runtime(self.root).run_config(run, 'executor')
        self.assertEqual(selected['host']['adapter'], 'devin-acp')
        recovered = runtime.Runtime(self.root).readresult(run_id=run['id'], config_path=None,
            role='executor', evidence_home=str(self.home))
        self.assertEqual(recovered['receipt'], result['receipt'])
        self.assertEqual(recovered['native_identity']['id'], 'fixture-devin')

    def test_closed_setup_without_prompt_is_recovered_without_loading_session(self):
        self.select_devin()

        class ExitedAfterSetup:
            def __init__(self, root, config):
                self.root,self.config = root,config

            def execute(self,text,observe,guard,*,boundary,**kwargs):
                with boundary():
                    child = subprocess.Popen(['/usr/bin/true'],start_new_session=True)
                    self.config['_execution_lease'].spawned(child)
                child.wait(timeout=3)
                native = runtime.identity('devin_cli','session','fixture-no-prompt',self.root)
                observe(native,'session_ready',{})
                rows = []
                for index,(method,params,result) in enumerate([
                        ('initialize',{}, {'protocolVersion':1}),
                        ('session/new',{}, {'sessionId':native['id']}),
                        ('session/set_mode',{'sessionId':native['id']}, {})],1):
                    rows.append({'direction':'client','id':index,'method':method,'params':params})
                    rows.append({'direction':'agent','jsonrpc':'2.0','id':index,'request_method':method,'result':result})
                path = Path(self.config['diagnostic_directory']).parent/'acp.jsonl'
                path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                raise ACPError('session/set_mode','Synthetic transport exited')

            def close(self):
                pass

        self.factory = ExitedAfterSetup
        with self.assertRaises(ACPError):
            self.execute(program_context={})
        worker = runtime.Runtime(self.root,adapter_factory=lambda *_:self.fail('Recovery must not start a transport'))
        self.assertEqual(worker._reconcile_devin_setup_exit(worker.core.read_run(self.root,'run'))['outcome'],'unknown')
        with patch('execution_lease.observe_run',return_value={'state':'exited'}):
            observed = worker.readresult(run_id='run',config_path=None,role='executor',evidence_home=str(self.home))
        self.assertEqual((observed['outcome'],observed['execution_state'],observed['target_session']),
                         ('failed','not_started','fixture-no-prompt'))

    def legacy_load_refusal(self,*,prompt=False):
        self.select_devin();first=self.execute(program_context={})
        error={'code':-32015,'message':'Session held by another native owner','data':{'cognition.ai/errorKind':'session_locked','cognition.ai/lockHolderPid':54306}}
        case=self
        class Legacy:
            def __init__(self,root,config):self.config=config;self.process=None
            def execute(self,text,observe,guard,**kwargs):
                with kwargs['boundary']():guard()
                self.process=subprocess.Popen(['/usr/bin/true'],start_new_session=True)
                self.config['_execution_lease'].spawned(self.process);self.process.wait(timeout=3)
                rows=[{'direction':'client','id':1,'method':'initialize'},
                    {'direction':'agent','jsonrpc':'2.0','id':1,'result':{'protocolVersion':1}},
                    {'direction':'client','id':2,'method':'session/load','params':{'sessionId':'fixture-devin'}},
                    {'direction':'agent','method':'session/update','params':{'update':{'sessionUpdate':'tool_call'}}},
                    {'direction':'agent','jsonrpc':'2.0','id':2,'error':error}]
                if prompt:rows.append({'direction':'client','id':3,'method':'session/prompt','params':{'sessionId':'fixture-devin'}})
                directory=Path(self.config['diagnostic_directory']).parent
                (directory/'acp.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
                raise ACPError('response',error)
            def close(self):pass
        self.factory=Legacy
        with self.assertRaises(ACPError):self.execute(run_id='rejected',operation='resume',previous_run_id=first['run_id'],program_context={})
        return first

    def test_historical_load_rejection_is_readonly_reconciled_and_same_session_resumable(self):
        first=self.legacy_load_refusal();worker=runtime.Runtime(self.root,adapter_factory=lambda *_:self.fail('Read must not launch/load a native Session'))
        before=worker.core.read_run(self.root,'rejected');self.assertEqual(before['outcome'],'unknown')
        original_receipt=before['latest_receipt']
        value=worker.readresult(run_id='rejected',config_path=None,role='executor',evidence_home=str(self.home))
        self.assertEqual((value['outcome'],value['execution_state'],value['target_session']),('failed','not_started','fixture-devin'))
        after=worker.core.read_run(self.root,'rejected')
        self.assertTrue(any(e.get('receipt')==original_receipt for e in after['events']))
        self.assertEqual(len(worker.readresult(run_id='rejected',config_path=None,role='executor',evidence_home=str(self.home))['original_receipt']),len(value['original_receipt']))
        self.assertEqual(worker.core.read_run(self.root,'rejected')['events'],after['events'])
        self.factory=lambda root,config:fixtures.CompletedAdapter(root,config,author=runtime.identity('devin_cli','session','fixture-devin',root))
        resumed=self.execute(run_id='recovered',operation='resume',previous_run_id='rejected',program_context={})
        self.assertEqual(resumed['native_identity']['id'],first['native_identity']['id'])
        self.assertEqual(worker.core.read_run(self.root,'rejected')['outcome'],'failed')

    def test_prompt_attempt_or_live_original_transport_remains_unknown(self):
        self.legacy_load_refusal(prompt=True);worker=runtime.Runtime(self.root)
        value=worker.readresult(run_id='rejected',config_path=None,role='executor',evidence_home=str(self.home))
        self.assertEqual(value['execution_state'],'launch_unresolved')
        self.assertEqual(worker.core.read_run(self.root,'rejected')['outcome'],'unknown')

    def test_ended_missing_session_admits_only_one_authorized_successor(self):
        import harness_package
        import run_worker
        from task_ledger import sessions
        import harness_tasks as tasks

        self.select_devin()
        author=runtime.identity('devin_cli','session','missing-original',self.root)

        class RejectedBeforePrompt:
            def __init__(self,root,config):
                self.config=config

            def execute(self,text,observe,guard,*,requested_session_id,boundary,**kwargs):
                with boundary():
                    guard()
                    self.process=subprocess.Popen(['/usr/bin/true'],start_new_session=True)
                    self.config['_execution_lease'].spawned(self.process)
                self.process.wait(timeout=3)
                transcript=[{'direction':'client','id':1,'method':'initialize'},
                            {'direction':'agent','jsonrpc':'2.0','id':1,'result':{'protocolVersion':1}}]
                if requested_session_id:
                    transcript.append({'direction':'client','id':2,'method':'session/load',
                                       'params':{'sessionId':requested_session_id}})
                    transcript.append({'direction':'agent','jsonrpc':'2.0','id':2,'error':{
                        'code':-32002,'data':{'cognition.ai/errorKind':'session_not_found'}}})
                    failure=harness_hosts.NativeStartRejected('acp_request_rejected_before_prompt')
                else:
                    transcript.append({'direction':'client','id':2,'method':'session/new'})
                    transcript.append({'direction':'agent','jsonrpc':'2.0','id':2,'result':{'sessionId':author['id']}})
                    observe(author,'inProgress',{})
                    failure=harness_hosts.NativeStartRejected('native_configuration_unavailable_before_prompt',
                                                               ready_thread=author,phase='before_prompt')
                path=Path(self.config['diagnostic_directory']).parent/'acp.jsonl'
                path.write_text(''.join(json.dumps(row)+'\n' for row in transcript))
                raise failure

            def close(self):
                pass

        self.factory=RejectedBeforePrompt
        with self.assertRaises(runtime.CapabilityUnavailable):
            self.execute(program_context={})
        with self.assertRaises(runtime.CapabilityUnavailable):
            self.execute(run_id='missing',operation='resume',previous_run_id='run',program_context={})
        note=self.home/'end-original.md'
        note.write_text('The old Session is missing before its first prompt. End the original delegation.')
        tasks.end_delegation(self.root,self.actor,'request',note)
        brief=self.home/'successor.md'
        brief.write_text('Continue the same original scope with one author.')
        successor=sessions.request(self.root,self.actor,'task',brief,scope_ids=['science'],continues='request')
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            proof=harness_package.missing_devin_writer(c,self.root,successor,'request')
            self.assertEqual(proof['target_session'],author['id'])
            with patch.object(leases,'observe_run',return_value={'state':'active'}):
                self.assertIsNone(harness_package.missing_devin_writer(c,self.root,successor,'request'))
            log=self.home/'runs'/'run'/'acp.jsonl'
            original=log.read_bytes()
            log.write_bytes(original+b'{"direction":"client","id":3,"method":"session/prompt"}\n')
            self.assertIsNone(harness_package.missing_devin_writer(c,self.root,successor,'request'))
            log.write_bytes(original)
        with tasks.transaction(self.root) as c:
            c.execute('SAVEPOINT before_late_return')
            tasks.apply(c,self.root,'return',self.actor,{'id':'late','request_id':'request',
                'result_ref':self.evidence,'summary':'A late result from the original delegation.'})
            self.assertIsNone(harness_package.missing_devin_writer(c,self.root,successor,'request'))
            c.execute('ROLLBACK TO before_late_return')
            c.execute('RELEASE before_late_return')
        launches=[]
        def factory(root,config):
            adapter=fixtures.CompletedAdapter(root,config,author=runtime.identity('devin_cli','session','new-author',root))
            launches.append(adapter)
            return adapter

        worker=runtime.Runtime(self.root,adapter_factory=factory)
        config=runtime.load_config(self.root,self.config_path,'executor')
        answer=run_worker._drive_program(self.root,worker,successor,config)
        self.assertEqual(answer['outcome'],'succeeded')
        self.assertEqual(answer['native_identity']['id'],'new-author')
        retained=tasks.read_run(self.root,answer['run_id'])
        self.assertEqual(next(event['payload']['writer_replacement'] for event in retained['events'] if event['kind']=='intent'),proof)
        self.assertEqual(run_worker._drive_program(self.root,worker,successor,config)['run_id'],answer['run_id'])
        self.assertEqual(len(launches),1)
        self.assertEqual(tasks.read_run(self.root,'missing')['outcome'],'failed')

    def test_devin_hook_uses_shared_snapshot_and_restores_after_invalidation(self):
        import devin_context_hook
        self.select_devin()
        self.execute(program_context={})
        event = {'session_id': 'fixture-devin', 'hook_event_name': 'UserPromptSubmit'}
        value = devin_context_hook.emit(self.root, 'request', 'run', event)
        self.assertIn('additionalContext', value['hookSpecificOutput'])
        self.assertEqual(devin_context_hook.emit(self.root, 'request', 'run', event), {})
        compact = {**event, 'hook_event_name': 'PostCompaction'}
        self.assertEqual(devin_context_hook.emit(self.root, 'request', 'run', compact), {})
        after = devin_context_hook.emit(self.root, 'request', 'run', {**event, 'hook_event_name': 'PostToolUse'})
        self.assertEqual(value['hookSpecificOutput']['additionalContext'], after['hookSpecificOutput']['additionalContext'])
        self.assertEqual(devin_context_hook.emit(self.root, 'request', 'run', {**event, 'session_id': 'other-session'}), {})

    def test_manager_exit_retains_live_client_tool_and_spawn_gap(self):
        from harness_tasks import read_request
        request = read_request(self.root, 'request')
        native = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
        tool = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
        def close(process):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        self.addCleanup(close, native)
        self.addCleanup(close, tool)
        with leases.ExecutionLease(self.root, request, self.home, 'tracked-tools') as lease:
            lease.intent()
            lease.spawned(native)
            lease.tool_intent('command')
            lease.tool_spawned('command', tool)
            close(native)
            lease.settled()
            observation = leases.execution_observation(lease.path)
            self.assertEqual(observation['state'], 'active')
            self.assertIn('tool', observation['reason'])
            close(tool)
            lease.settled()
            self.assertEqual(leases.execution_observation(lease.path)['state'], 'exited')
        with leases.ExecutionLease(self.root, request, self.home, 'spawn-gap') as lease:
            lease.intent()
            lease.tool_intent('unrecorded-pid')
            lease.settled()
            observation = leases.execution_observation(lease.path)
            self.assertEqual(observation['state'], 'unknown')
            self.assertIn('intent', observation['reason'])


if __name__ == '__main__':
    unittest.main()
