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

    def seed(self, name, *, progress=1, phase='active', used=1, status='failed', error=None, stop=False, parent=None):
        name='run-'+name
        request_id='request-'+name
        self.create_request(id=request_id)
        return self._terminal(name,request_id,parent=parent,progress=progress,phase=phase,used=used,status=status,error=error,stop=stop)

    def _terminal(self, name, request_id, *, parent=None, progress=1, phase='active', used=1,
                  status='failed', error=None, stop=False):
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

    def test_cooling_deduplicates_terminal_failure_and_allows_only_one_probe(self):
        with patch('dsh_auto_resume.time.time',side_effect=lambda:self.clock):
            first=self.seed('empty',progress=0)
            for _ in range(2):self.assertEqual(self.consider(first,'no-start')['state'],'ineligible')
            self.assertEqual(len(self.events(recovery.FAULT)),1)
            self.clock+=20
            second=self.seed('second',error={'code':'PI_AI_ERROR','message':recovery.MALFORMED_SSE})
            held=self.consider(second,'second-child')
            self.assertEqual(held['state'],'cooldown')
            self.assertEqual(held['until'],self.clock+600)
            self.assertEqual(len(self.events(recovery.FAULT)),2)
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
            child=self._terminal('one-probe',original['request_id'],parent=original['id'],error={'code':'NOT_A_TRANSPORT_ERROR'})
            self.assertIsNone(self.consider(child,'not-recursive'))
            recovery.finish(self.root,self.actor,original['id'],claim)
            latest=self.events(recovery.FUSE)[0]
            self.assertEqual((latest['state'],latest['until']),('open',self.clock+600))

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

