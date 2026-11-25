"""Exact original ledger, provider cooling, and recovery admission boundaries."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from agent_base import identity
import dsh_auto_resume as recovery
import dsh_capacity as capacity
import execution_lease as execution
import harness_tasks as tasks
import test_agent_runtime as fixture


class AutoResumeTests(unittest.TestCase):
    setUp_original=fixture.RuntimeTests.setUp
    create_request=fixture.RuntimeTests.create_request
    ref=fixture.RuntimeTests.ref

    def setUp(self):
        self.setUp_original()
        queue=capacity._queue(self.root)
        queue.update(policy=capacity.PILOT_POLICY,pilot_provider='isolated')
        execution.atomic_json(capacity._paths(self.root)[0],queue)
        self.config={'provider':{'dsh_provider':'isolated'}}
        self.observed=patch.object(execution,'observe_run',return_value={'state':'exited'})
        self.observed.start();self.addCleanup(self.observed.stop)
        self.clock=10000

    def seed(self, name, *, progress=1, phase='active', used=1, status='failed', error=None, stop=False, parent=None,
             zero_tools=False):
        name='run-'+name
        request_id='request-'+name
        self.create_request(id=request_id)
        return self._terminal(name,request_id,parent=parent,progress=progress,phase=phase,used=used,status=status,error=error,stop=stop,
                              zero_tools=zero_tools)

    def _terminal(self, name, request_id, *, parent=None, progress=1, phase='active', used=1,
                  status='failed', error=None, stop=False, zero_tools=False):
        native=identity('deepseek_harness','session','session-'+(parent or name),self.root,'2' if parent else '1')
        launch=self.home/(name+'-launch.json')
        launch.write_text(json.dumps({'run_id':name,'request_id':request_id,'native_cwd':str(self.root),'work_home':str(self.home)}))
        self.create_intent(name,request_id,launch,parent=parent)
        observed={'source':'dsh_persisted_session_log','session_id':native['id'],'latest_turn':native['turn_id'],
                  'requests':[{'provider':'isolated'}], 'terminal':'error' if status=='failed' else 'completed',
                  'native_error':error if error is not None else {'code':'TIMEOUT'},'unreturned_tool_calls':[],
                  'native_goal':{'id':'goal-'+(parent or name),'phase':phase,'roundsStarted':used,'maxGoalRounds':4}}
        result={'native_status':'failed' if status=='failed' else 'completed','native_session_observation':observed,
                'turn_activity':{'unreturned_tools':0,'ending_turn_progress_results':1,
                                 **({'ending_turn_tool_calls':0} if zero_tools else {}),
                                 'ending_turn_verified_progress':[{'kind':'artifact'}] if progress else []},
                'metering':{'finished_at':datetime.fromtimestamp(self.clock,timezone.utc).isoformat()}}
        receipt=self.home/(name+'-receipt.json');receipt.write_text(json.dumps({'result':result}))
        with tasks.transaction(self.root) as c:
            c.execute('UPDATE work_runs SET native_identity_json=?,outcome=?,stop_requested=? WHERE id=?',
                      (tasks._json(native),status,int(stop),name))
            tasks._run_event(c,tasks._row(c,'work_runs',name),'outcome',self.actor,self.ref(receipt),{})
        return tasks.read_run(self.root,name)

    def create_intent(self, name, request_id, launch, *, parent=None):
        return tasks.mutate(self.root,'run-intent',self.actor,{'id':name,'request_id':request_id,
            'config_ref':self.ref(self.config_path),'input_refs':[self.ref(launch)],'target_host':'deepseek_harness',
            'operation':'resume' if parent else 'start',
            **({'previous_run_id':parent,'dsh_auto_continue_from':parent} if parent else {})})

    def consider(self, run, next_run=None):
        return recovery.consider(self.root,self.actor,tasks.read_request(self.root,run['request_id']),run,self.config,next_run)

    def events(self, kind):
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            return recovery._provider_events(c,kind,'isolated')

    def test_initial_goal_after_completed_reads_requires_matching_live_observation(self):
        run = self.seed('first-goal', progress=0, used=0)
        request = tasks.read_request(self.root, run['request_id'])
        result = recovery.read_receipt(self.root, run['latest_receipt'])['result']
        log = recovery._observed(run, result, 'isolated')
        result['turn_activity']['ending_turn_tool_calls'] = 52
        live = {'sessionId':run['native_identity']['id'],
                'goal':{**log['native_goal'], 'activation':'disarmed'}}
        result['native_goal_observation'] = live
        with contextlib.closing(tasks._connect(self.root, True)) as c:
            def ready():
                return recovery._ready(c, self.root, request, run, self.config, result, log)
            self.assertTrue(ready())
            with patch.dict(live, sessionId='another-session'):
                self.assertFalse(ready())
            with patch.dict(live['goal'], id='another-goal'):
                self.assertFalse(ready())
            with patch.dict(log, unreturned_tool_calls=['pending-tool']):
                self.assertFalse(ready())
            with patch.dict(run, stop_requested=True):
                self.assertFalse(ready())

    def test_session_budget_uses_distinct_failures_and_resets_on_progress(self):
        def observed(name,stamp,*,session='same',request=None,progress=False,kind='transport_timeout'):
            return {'run_id':name,'native_session':session,'native_turn':session+':'+name,
                    'native_request':request,'observed_at':stamp,'verified_progress':progress,'kind':kind}
        first=observed('first',10,request='wire-a')
        duplicate=observed('duplicate',12,request='wire-a')
        second=observed('second',20,request='wire-b')
        responses=[first,duplicate,observed('elsewhere',15,session='other'),
                   observed('decode',17,kind='sse_json_decode')]
        self.assertEqual(recovery.transport_budget([first],duplicate),1)
        self.assertEqual(recovery.transport_budget(responses,second),2)
        self.assertEqual(recovery.transport_budget(responses,{**second,'verified_progress':True}),1)

    def test_candidate_checkpoint_grants_one_retry_without_resetting_the_session_budget(self):
        original=self.seed('candidate-checkpoint',progress=0)
        saved={'prepared_candidate_refs':[self.ref(self.input_path)]}
        with patch('dsh_auto_resume.time.time',side_effect=lambda:self.clock):
            request=tasks.read_request(self.root,original['request_id'])
            claim=recovery.consider(self.root,self.actor,request,original,self.config,'continue-candidate',
                delivery_effects={**saved,'run_id':original['id']})
            self.assertEqual(claim['state'],'claimed')
            self.assertFalse(self.events(recovery.FAULT)[0]['verified_progress'])
            self.clock+=1
            second=self._terminal('continue-candidate',request['id'],parent=original['id'],progress=0)
            held=recovery.consider(self.root,self.actor,request,second,self.config,'do-not-loop',
                delivery_effects={**saved,'run_id':second['id']})
            self.assertEqual(held['state'],'session_retry_limit')

    def test_cooling_deduplicates_terminal_failure_and_allows_only_one_probe(self):
        with patch('dsh_auto_resume.time.time',side_effect=lambda:self.clock):
            first=self.seed('empty',progress=0)
            for _ in range(2):self.assertEqual(self.consider(first,'no-start')['state'],'ineligible')
            self.assertEqual(len(self.events(recovery.FAULT)),1)
            self.clock+=20
            other=self.seed('bad-frame',error={'code':'PI_AI_ERROR','message':recovery.MALFORMED_SSE},progress=0)
            self.assertEqual(self.consider(other,'bad-frame-child')['state'],'ineligible')
            self.assertEqual(self.events(recovery.FUSE),[])
            second=self.seed('second')
            held=self.consider(second,'second-child')
            self.assertEqual(held['state'],'cooldown')
            self.assertEqual(held['until'],self.clock+600)
            self.assertEqual(len(self.events(recovery.FAULT)),3)
            self.clock+=601
            third=self.seed('third')
            fourth=self.seed('fourth')
            barrier=threading.Barrier(2)
            def try_once(run):
                barrier.wait(timeout=5)
                return self.consider(run,run['id']+'-child')
            with ThreadPoolExecutor(max_workers=2) as pool:
                a,b=pool.map(try_once,(third,fourth))
            self.assertEqual({x['state'] for x in (a,b)},{'claimed','probe_in_progress'})
            sealed,=self.events(recovery.CLAIM)
            self.assertTrue(sealed['probe'])
            original=tasks.read_run(self.root,sealed['run_id'])
            child=self._terminal(sealed['next_run_id'],original['request_id'],parent=original['id'],status='succeeded')
            recovery.finish(self.root,self.actor,original['id'],sealed)
            # A new manager reads the same durable state. The concurrent fresh
            # failure after the probe claim restarts the full cooldown.
            self.assertEqual(self.events(recovery.FUSE)[0]['state'],'open')
            self.clock+=601
            next_probe=self.seed('reset')
            granted=self.consider(next_probe,'reset-child')
            self.assertTrue(granted['probe'])
            self._terminal('reset-child',next_probe['request_id'],parent=next_probe['id'],status='succeeded')
            recovery.finish(self.root,self.actor,next_probe['id'],granted)
            self.assertEqual(self.events(recovery.FUSE)[0]['state'],'reset')
            self.assertEqual(child['outcome'],'succeeded')

    def test_only_observed_progress_and_current_authority_allow_reentry(self):
        with patch('dsh_auto_resume.time.time',side_effect=lambda:self.clock):
            cases=(('no_data',{'progress':0}),('paused',{'phase':'paused'}),('exhausted',{'used':4}),
                   ('blocked',{'phase':'blocked'}),('stopped',{'stop':True}))
            for name,options in cases:
                run=self.seed(name,**options)
                self.assertEqual(self.consider(run,name+'-child')['state'],'ineligible')
                self.clock+=601
            ordinary=self.seed('text-only',progress=0,status='succeeded')
            self.assertIsNone(self.consider(ordinary,'never'))
            other=self.seed('non-transport',error={'code':'PI_AI_ERROR','message':'some other application error'})
            self.assertIsNone(self.consider(other,'never'))
            unsafe=self.seed('unresolved-write')
            interrupted=self.home/'runs'/unsafe['id']/'delivery/operation-unknown'
            interrupted.mkdir(parents=True)
            self.assertEqual(self.consider(unsafe,'withheld')['state'],'original_effect_or_attempt_unresolved')
            self.clock+=601
            good=self.seed('safe')
            claimed=self.consider(good,'new-native')
            self.assertEqual(claimed['state'],'claimed')
            tasks.mutate(self.root,'run-stop',self.actor,{'id':good['id'],'summary':'Explicit original stop before admission'})
            launch=Path(good['input_refs'][0]['path'])
            with self.assertRaisesRegex(tasks.WorkConflict,'no longer authorized'):
                self.create_intent('new-native',good['request_id'],launch,parent=good['id'])

    def test_failed_probe_reopens_even_without_a_second_transport_fault(self):
        with patch('dsh_auto_resume.time.time',side_effect=lambda:self.clock):
            first=self.seed('prior-1',progress=0);self.consider(first,'unused')
            self.clock+=1
            second=self.seed('prior-2',progress=0);self.consider(second,'unused')
            self.clock+=601
            original=self.seed('eligible')
            claim=self.consider(original,'one-probe')
            self.assertTrue(claim['probe'])
            self._terminal('one-probe',original['request_id'],parent=original['id'],error={'code':'TIMEOUT'})
            recovery.finish(self.root,self.actor,original['id'],claim)
            latest=self.events(recovery.FUSE)[0]
            self.assertEqual((latest['state'],latest['until']),('open',self.clock+600))

    def test_original_local_write_lock_is_not_a_provider_fault_or_an_extension(self):
        with patch('dsh_auto_resume.time.time',side_effect=lambda:self.clock):
            for n in range(3):
                run=self.seed(f'local-{n}',progress=0)
                original=self.home/f'local-{n}.json'
                execution.atomic_json(original,{'source':'adapter_exception','run_id':run['id'],
                    'request_id':run['request_id'],'config_sha256':run['config_ref']['sha256'],
                    'error_type':'OperationalError','detail':'database is locked'})
                result=recovery.read_receipt(self.root,run['latest_receipt'])['result']
                result.update(native_status='unknown',transport_status='interrupted; native turn unresolved')
                result['native_session_observation']['terminal']=None
                later=self.home/f'native-{n}.json'
                execution.atomic_json(later,{'result':result})
                with tasks.transaction(self.root) as c:
                    c.execute("UPDATE work_runs SET outcome='unknown' WHERE id=?",(run['id'],))
                    row=tasks._row(c,'work_runs',run['id'])
                    tasks._run_event(c,row,'unknown',self.actor,self.ref(original),{})
                    tasks._run_event(c,row,'run-outcome',self.actor,self.ref(later),{})
                    # Preserve a legacy classification and its old cooldown for projection.
                    tasks._run_event(c,row,recovery.FAULT,self.actor,None,{'provider':'isolated',
                        'kind':'interrupted_transport','native_turn':str(n),'observed_at':self.clock})
                    tasks._run_event(c,row,recovery.FUSE,self.actor,None,{'provider':'isolated',
                        'state':'open','until':self.clock+600})
                current=tasks.read_run(self.root,run['id'])
                self.assertEqual(self.consider(current,'never')['fault'],'local_ledger_contention')
                with contextlib.closing(tasks._connect(self.root,True)) as c:
                    self.assertEqual(recovery.effective_fuse(c,'isolated'),{})
                self.clock+=10
            self.assertFalse(self.events(recovery.FAULT))
            self.assertEqual(len(self.events(recovery.FUSE)),3)
            self.assertEqual(self.consider(self.seed('genuine-1'),'first')['state'],'claimed')
            self.assertEqual(self.consider(self.seed('genuine-2'),'second')['state'],'cooldown')

    def test_recent_timeout_is_bound_to_the_observed_original_turn_and_provider(self):
        run=self.seed('timed',progress=0)
        result=recovery.read_receipt(self.root,run['latest_receipt'])['result']
        with patch('dsh_auto_resume.time.time',side_effect=lambda:self.clock):
            self.assertEqual(recovery.recent_timeout_deadline(run,result,'isolated'),(self.clock+600)*1000)
            self.assertEqual(recovery.recent_timeout_deadline(run,result,'other'),0)
            wrong={**run,'native_identity':{**run['native_identity'],'turn_id':'2'}}
            self.assertEqual(recovery.recent_timeout_deadline(wrong,result,'isolated'),0)
            self.clock+=601
            self.assertEqual(recovery.recent_timeout_deadline(run,result,'isolated'),0)

    def test_transport_recovery_reconciles_only_the_original_native_write(self):
        import delivery_evidence as evidence

        run=self.seed('write-receipt')
        request=tasks.read_request(self.root,run['request_id'])
        archive=self.home/'runs'/run['id']/'delivery'
        target={'type':'flow','id':'exact-flow','version':'00.00.001'}
        operation='dataset flow save'
        intent={key:target[key] for key in ('id','version')}
        intent.update(requestId='original',expectedActor='author',expectedEnvironment='https://isolated.invalid')

        def retain(name, reply, *, captures_request=intent, dispatch='started'):
            place=archive/('operation-'+name)
            incoming=place/'input.json'
            output=place/'stdout.bin'
            execution.atomic_json(incoming,captures_request)
            execution.atomic_json(output,reply)
            execution.atomic_json(place/'record.json',{'schema':evidence.CLI_SCHEMA,'source':'trusted_public_cli',
                'operation':operation,'argv':['dataset','flow','save','--input','-','--json'],
                'request_id':request['id'],'run_id':run['id'],'dispatch':dispatch,
                'input_unchanged':True,'capture_complete':True,'input_ref':self.ref(incoming),'stdout_ref':self.ref(output)})

        context={'requestId':'original','actor':{'id':'author'},'environment':'https://isolated.invalid'}
        envelope={'schemaVersion':1,'operation':operation,'context':context,'data':{'subject':target}}
        retain('unknown',{**envelope,'outcome':'write_outcome_unknown','status':'unknown'})
        retain('not-started',{},captures_request={**intent,'requestId':'local-stop'},dispatch='not_started')
        retain('unrelated',{**envelope,'context':{**context,'requestId':'unrelated'},'outcome':'dataset_written',
              'status':'completed'},captures_request={**intent,'requestId':'unrelated'})
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            self.assertEqual(recovery._unresolved_operations(c,self.root,request)['operation_id'],'original')
            retain('late-reply',{**envelope,'outcome':'service_rejected','status':'failed',
                  'data':{'subject':target,'receipt':{'ok':False,'code':'CONTENT_CONFLICT'}}})
            self.assertIsNone(recovery._unresolved_operations(c,self.root,request))

    def test_six_seats_defer_a_zero_tool_failure_without_holding_capacity(self):
        from dsh_recovery_host import _deadline, reattach, record_result

        queue = capacity._queue(self.root)
        queue.update(policy=capacity.POLICY, pilot_provider=None)
        execution.atomic_json(capacity._paths(self.root)[0], queue)
        execution.atomic_json(self.root/'.control/dsh-recovery.json', {'provider':'isolated'})
        watch = {'provider':'isolated', 'manager':execution.process_identity(__import__('os').getpid())}
        with patch('dsh_auto_resume.time.time', side_effect=lambda: self.clock):
            first = self.seed('no-action', progress=0)
            reattach(self.root, self.actor, first['id'], watch)
            first = tasks.read_run(self.root, first['id'])
            self.assertEqual(self.consider(first, 'unused')['state'], 'ineligible')
            self.clock += 10
            second = self.seed('zero-tools', progress=0, zero_tools=True)
            reattach(self.root, self.actor, second['id'], watch)
            # An interrupted trusted read never requires verifying a write.
            pending = self.home/'runs'/second['id']/'delivery/operation-read'
            pending.mkdir(parents=True)
            execution.atomic_json(pending/'dispatch.json', {'schema':'htam.cli-dispatch.v1',
                'run_id':second['id'], 'request_id':second['request_id'], 'read_only':True})
            second = tasks.read_run(self.root, second['id'])
            self.assertEqual(self.consider(second, 'child')['state'], 'cooldown')
            second = tasks.read_run(self.root, second['id'])
            self.assertEqual(_deadline(self.root, second, 'isolated'), self.clock+600)
            self.assertFalse(capacity._queue(self.root)['entries'])
            self.assertEqual(capacity._queue(self.root)['policy'], capacity.POLICY)
            self.clock += 601
            self.assertTrue(self.consider(second, 'child')['probe'])
            child = self._terminal('child', second['request_id'], parent=second['id'], status='succeeded')
            reattach(self.root, self.actor, child['id'], watch)
            # The service may observe the child in a separate process after
            # the original probe claimant has disappeared.
            record_result(self.root, self.actor, tasks.read_run(self.root, child['id']), None)
            self.assertEqual(self.events(recovery.FUSE)[0]['state'], 'reset')

    def test_public_stop_cancels_pending_recovery_without_changing_native_failure(self):
        from agent_runtime import Runtime
        from dsh_recovery_host import reattach
        import os

        run = self.seed('pending')
        reattach(self.root, self.actor, run['id'],
                 {'provider':'isolated','manager':execution.process_identity(os.getpid())})
        result = Runtime(self.root).stop(run_id=run['id'], role='executor',
            config_path=str(self.config_path), evidence_home=str(self.home), reason='Stop this technical continuation')
        self.assertEqual(result['outcome'], 'failed')
        self.assertTrue(result['stop_requested'])
        self.assertFalse(result['stop_confirmed'])
        stopped = tasks.read_run(self.root, run['id'])
        self.assertEqual(stopped['latest_receipt'], run['latest_receipt'])
        self.assertIsNone(recovery.pending_view(stopped))

