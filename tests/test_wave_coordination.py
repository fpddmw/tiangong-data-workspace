"""Offline tests of the public task entries and an isolated role host."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_harness_tasks as fixture
from task_ledger import decisions
import harness_scope as scope
import harness_tasks as tasks
import harness_wave as wave
from task_ledger import role_intake as intake


class WaveTests(unittest.TestCase):
    setUp = fixture.TaskCoreTests.setUp
    tearDown = fixture.TaskCoreTests.tearDown
    actor = fixture.TaskCoreTests.actor
    save = fixture.TaskCoreTests.save
    call = fixture.TaskCoreTests.call
    request = fixture.TaskCoreTests.request
    returned = fixture.TaskCoreTests.returned

    def cli(self, *args, actor=None):
        return subprocess.run([sys.executable, str(fixture.BIN / 'harness.py'), '--root', str(self.root), *args],
                              capture_output=True, text=True,
                              env={**os.environ, 'CODEX_THREAD_ID': (actor or self.lead)['id']})

    def decision(self, ident, object_id, subject, statement, details=None, action=None, *, actor=None, supersedes=None):
        who = actor or self.lead
        selected = {'task': 'task', 'object': object_id}
        if action:
            selected['action'] = action
        document = {'id': ident, 'scope': selected, 'subject': subject, 'statement': statement,
                    'source_ref': {**self.evidence, 'locator': 'file', 'decided_by': who['id']},
                    **({'details': details} if details else {}), **({'supersedes': supersedes} if supersedes else {})}
        with patch.dict(os.environ, CODEX_THREAD_ID=who['id']):
            return decisions.add(self.root, document)

    def current(self, actor=None, capacity=None):
        with patch.dict(os.environ, CODEX_THREAD_ID=(actor or self.lead)['id']):
            return wave.wait(self.root, ('task',), None, 0, capacity=capacity)['tasks']['task']

    def test_one_explicit_plan_revision_and_historical_binding_after_100_returns(self):
        self.request()
        for index in range(100):
            self.returned('ordinary-' + str(index))
        initial = json.loads(self.cli('work-show', '--id', 'task').stdout)
        self.assertEqual(initial['commitment']['source_ref'], self.plan)
        self.assertEqual(initial['commitment']['version'], 1)
        self.assertEqual(initial['return_count_for_request_page'], 100)
        self.assertNotIn('events', initial['history'])
        note = self.save('next', {'purpose': 'New specific scope'})
        rival = self.save('other-next', {'purpose': 'Conflicting specific scope'})
        reason = self.save('why', {'reason': 'Original scope correction'})
        params = ('work-revise', '--task', 'task', '--base-sha256', self.plan['sha256'], '--reason-file', reason['path'])
        denied = self.cli(*params, '--note', note['path'], actor=self.other)
        self.assertEqual(json.loads(denied.stderr)['code'], 'plan_authority_required')
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda plan: self.cli(*params, '--note', plan['path']), [note, rival]))
        ok, = [r for r in results if r.returncode == 0]
        stale, = [r for r in results if r.returncode != 0]
        self.assertEqual(json.loads(stale.stderr)['code'], 'plan_baseline_conflict')
        old = json.loads(self.cli('work-show', '--id', 'task', '--history').stdout)
        event, = [row for row in old['history']['events'] if row['kind'] == 'revise']
        latest = json.loads(ok.stdout)
        self.assertEqual(latest['plan_revision'], 2)
        self.assertEqual(event['payload']['baseline']['previous_plan_ref'], self.plan)
        self.assertEqual(event['payload']['baseline']['reason_ref'], reason)
        self.assertEqual(json.loads(self.cli(*params, '--note', latest['plan_ref']['path']).stdout), latest)
        inline=(*params[:-2],'--reason',Path(reason['path']).read_text(),'--note',latest['plan_ref']['path'])
        self.assertEqual(json.loads(self.cli(*inline).stdout),latest)
        Path(self.plan['path']).write_text('Editing bytes on disk does not replace the original baseline.')
        self.assertNotEqual(tasks.evidence_ref(self.root, self.plan)['path'], self.plan['path'])
        self.assertEqual(tasks.read_request(self.root, 'request')['plan_revision'], 1)

    def test_wait_reuses_reason_across_transfer_and_wakes_independent_work(self):
        self.request()
        self.returned('b')
        dep=self.root/'supplier-readback.md';dep.write_text('Waiting for supplier X.')
        item=self.current()['actionable'][0]['item_id']
        call=('work-wait','--task','task','--item',item,'--for-file',str(dep))
        reason=json.loads(self.cli(*call,'--reason','Await supplier X.').stdout)
        revision = tasks.read_task(self.root, 'task')['task']['revision']
        self.call(self.main, 'transfer', {'id': 'transfer', 'task_id': 'task', 'expected_task_revision': revision,
            'new_owner_actor': self.other, 'evidence_ref': self.evidence, 'summary': 'Transfer the original lead.'})
        reused=json.loads(self.cli(*call,'--reason','Await supplier X.',actor=self.other).stdout)
        self.assertEqual(reused['id'], reason['id'])
        self.returned('c')
        current = self.current(actor=self.other)
        self.assertEqual([item['object'] for item in current['actionable']], ['c'])
        self.assertEqual(current['waiting'][0]['wait_decision'],reason['id'])
        with patch.dict(os.environ, CODEX_THREAD_ID='other'):
            initial = wave.wait(self.root, ('task',), None, 0)
            again = wave.wait(self.root, ('task',), initial['cursor'], 1)
            self.assertEqual(again['status'], 'actionable')
            self.assertEqual(again['new'], [])
            dep.write_text('The original supplier replied.')
            again = wave.wait(self.root, ('task',), initial['cursor'], 1)
            self.assertEqual([item['object'] for item in again['new']], ['b'])
            crash_restart = wave.wait(self.root, ('task',), None, 0)
            self.assertEqual({item['object'] for item in crash_restart['new']}, {'b', 'c'})

    def test_explicit_wait_public_entry_wakes_for_new_return_and_wave_is_immutable(self):
        self.request()
        initial = json.loads(self.cli('work-show', '--id', 'task').stdout)
        with ThreadPoolExecutor(1) as pool:
            waiter = pool.submit(self.cli, 'work-show', '--id', 'task', '--after', initial['cursor'], '--wait-seconds', '10')
            self.returned('result')
            result = json.loads(waiter.result(timeout=11).stdout)
        self.assertEqual(result['status'], 'actionable')
        self.assertEqual([row['return_id'] for row in result['new']], ['result'])
        action=self.cli('work-dispose','--return-id','result','--use','hold','--decision',self.evidence['path'])
        one=json.loads(action.stdout)['id']
        self.assertEqual(self.cli('work-wave','--task','task','--decision',one).returncode,0)
        closed = json.loads(self.cli('work-show', '--id', 'task', '--wave',one).stdout)
        self.returned('later')
        action=self.cli('work-dispose','--return-id','later','--use','revise','--decision',self.brief['path'])
        two=json.loads(action.stdout)['id']
        self.assertEqual(self.cli('work-wave','--task','task','--decision',two).returncode,0)
        self.assertEqual(json.loads(self.cli('work-wave','--task','task','--decision',one).stdout),closed)
        after = json.loads(self.cli('work-show', '--id', 'task', '--wave',one).stdout)
        self.assertEqual(closed, after)
        self.assertTrue(json.loads(self.cli('work-show', '--id', 'task', '--wave').stdout)['sponsor_check_due'])
        self.assertIn(two,{item['object'] for item in self.current(self.main)['actionable']})
        reviewed=self.cli('work-dispose','--return-id','later','--use','revise','--decision',self.evidence['path'],
                          '--wave',two,actor=self.main)
        self.assertEqual(reviewed.returncode,0,reviewed.stderr)
        self.assertNotIn(two,{item['object'] for item in self.current(self.main)['actionable']})

    def test_display_pages_and_handoff_cannot_acknowledge_pending_returns(self):
        self.request()
        self.returned('older')
        self.returned('newer')
        first = json.loads(self.cli('work-show', '--id', 'task', '--limit', '1').stdout)
        second = json.loads(self.cli('work-show', '--id', 'task', '--limit', '1', '--offset', '1').stdout)
        self.assertEqual(first['pending']['actionable_next_offset'], 1)
        self.assertEqual(second['pending']['actionable_next_offset'], None)
        self.assertEqual([x['return_id'] for x in second['pending']['actionable']], ['newer'])
        waited = json.loads(self.cli('work-show', '--id', 'task', '--after', first['cursor'], '--wait-seconds', '2').stdout)
        self.assertEqual(waited['status'], 'actionable')
        self.assertEqual([x['return_id'] for x in waited['new']], ['newer'])
        self.assertEqual([x['return_id'] for x in waited['tasks']['task']['actionable']], ['older', 'newer'])
        expected={}
        for key,item in zip(('X','Y'),waited['tasks']['task']['actionable']):
            wait=json.loads(self.cli('work-wait','--task','task','--item',item['item_id'],'--for-file',str(self.root/key)).stdout)
            expected[key]=wait['id']
        quiet = json.loads(self.cli('work-show', '--id', 'task', '--after', waited['cursor'], '--wait-seconds', '1').stdout)
        self.assertEqual(quiet['status'], 'timeout')
        self.assertEqual({x['wait_decision'] for x in quiet['tasks']['task']['waiting']},set(expected.values()))
        revision = tasks.read_task(self.root, 'task')['task']['revision']
        self.call(self.main, 'transfer', {'id': 'recovered-owner', 'task_id': 'task', 'expected_task_revision': revision,
                  'new_owner_actor': self.other, 'evidence_ref': self.evidence, 'summary': 'Original lead exited.'})
        (self.root/'X').write_text('Provider X returned.')
        resumed = json.loads(self.cli('work-show', '--id', 'task', '--after', waited['cursor'], '--wait-seconds', '2', actor=self.other).stdout)
        self.assertEqual(resumed['status'], 'actionable')
        self.assertEqual([x['return_id'] for x in resumed['new']], ['older'])
        self.assertEqual([x['wait_decision'] for x in resumed['tasks']['task']['waiting']],[expected['Y']])

    def test_original_ready_request_is_visible_without_an_extra_ready_decision(self):
        setup = {'items': [{'scope_id': 'a', 'kind': 'deliverable', 'label': 'Original unit',
                            'source_refs': [self.brief], 'target_endpoint': 'evidence'}],
                 'verified_scope_ids': ['a'], 'source_ref': self.evidence}
        with patch.dict(os.environ, CODEX_THREAD_ID='lead'):
            scope.apply_scope(self.root, 'task', setup, self.lead)
        self.request(scope=['a'], delivery_contract={'purpose': 'Original use', 'endpoint': 'evidence',
            'scope_boundary': {'in_scope': 'a', 'out_of_scope': 'b'}, 'acceptance': {'text': 'Source-backed report'}})
        seats = lambda host: {'policy_ready': True, 'counts': {'occupied': {'author': 0, 'total': 0}, 'queued': {}},
                              'limits': {'author': 6, 'total': 12}}
        with (patch('harness_hosts.installed_program', return_value=self.brief),
              patch('agent_runtime.load_config', return_value={'programmatic': True, 'host': {'adapter': 'devin-acp'}, 'provider': {}}),
              patch('agent_runtime.capability', return_value={'enabled': True, 'executable_found': True})):
            pending, = self.current(capacity=seats)['actionable']
            self.assertEqual(pending['command'], 'work-dispatch --request request --execute')
            self.assertEqual(pending['available_author_seats'], 6)
            self.assertEqual(json.loads(self.cli('work-dispatch', '--request', 'request').stdout)['run'], None)
            self.returned(coverage=['a'])
            pending, = self.current(capacity=seats)['actionable']
            self.assertEqual(pending['action'], 'adopt')

    def test_independent_requests_on_an_adopted_scope_keep_separate_waits(self):
        agreement = {'purpose':'Two uses on one branch', 'endpoint':'evidence',
                     'scope_boundary':{'in_scope':'a', 'out_of_scope':'b'},
                     'acceptance':{'text':'Reproducible evidence.'}}
        with patch.dict(os.environ, CODEX_THREAD_ID='lead'):
            setup = {'items':[{'scope_id':'a', 'kind':'deliverable', 'label':'A', 'source_refs':[self.brief],
                               'target_endpoint':'evidence'}], 'verified_scope_ids':['a'], 'source_ref':self.brief}
            scope.apply_scope(self.root, 'task', setup, self.lead)
            with tasks.transaction(self.root) as c:
                scope.set_contract(c, self.root, 'task', 'task', agreement, self.brief, self.lead)
        self.request(scope=['a'], delivery_contract=agreement)
        self.returned(coverage=['a'])
        adopted = self.cli('work-dispose', '--return-id', 'return', '--use', 'adopt', '--decision',
                           self.evidence['path'], '--scope', 'a', '--acceptance', 'final')
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        quiet = json.loads(self.cli('work-show','--mine').stdout)['tasks']['task']
        settled = next(r for r in quiet['scope']['scope_items'] if r['scope_id']=='a')
        self.assertEqual((settled['state'], settled['remaining'], quiet['actionable_count']), ('accepted',False,0))
        detail = json.loads(self.cli('work-show','--id','task','--scope','a').stdout)
        self.assertEqual(detail['accepted_use']['source_ref'], self.evidence)
        self.assertEqual(detail['original_brief_ref'], self.brief)
        for qid in ('new-use','other-use'):
            self.request(qid, scope=['a'], delivery_contract=agreement)
        pool = {'policy_ready':True,'counts':{'occupied':{'author':0,'total':0},'queued':{}},
                'limits':{'author':6,'total':12}}
        with (patch.dict(os.environ,CODEX_THREAD_ID='lead'),
              patch('harness_hosts.installed_program',return_value=self.brief),
              patch('agent_runtime.load_config',return_value={'programmatic':True,'host':{'adapter':'devin-acp'},'provider':{}}),
              patch('agent_runtime.capability',return_value={'enabled':True,'executable_found':True}),
              patch('dsh_capacity.snapshot',return_value=pool)):
            first = wave.wait(self.root,(),None,0,mine=True)
            items = {q['request_id']:q for q in first['new']}
            self.assertEqual(set(items), {'new-use','other-use'})
            self.assertNotEqual(items['new-use']['item_id'], items['other-use']['item_id'])
            dep = self.root/'upstream-result'
            saved = wave.defer(self.root,'task',items['new-use']['item_id'],path=str(dep),external=True)
            # The original persisted wait was scope-keyed and already identified its request.
            with tasks.transaction(self.root) as c:
                legacy = {**saved, 'id':'existing-decision', 'item_id':wave._digest(['task','a','dispatch'])}
                tasks._event(c,tasks._row(c,'work_tasks','task'),wave.DEFERRED,self.lead,legacy)
            pending = self.current()
            self.assertEqual([q['request_id'] for q in pending['actionable']], ['other-use'])
            self.assertEqual([q['request_id'] for q in pending['waiting']], ['new-use'])
            self.assertEqual(pending['waiting'][0]['wait_decision'], 'existing-decision')
            dep.write_text('Release')
            resumed = wave.wait(self.root,(),first['cursor'],0,mine=True)
            self.assertEqual([q['request_id'] for q in resumed['new']], ['new-use'])
            self.assertEqual(tasks.read_task(self.root,'task')['runs'], [])

    def test_unknown_writer_offers_exact_readback_and_another_branch_can_start(self):
        agreement = {'purpose':'A separable use', 'endpoint':'evidence',
                     'scope_boundary':{'in_scope':'contracted branch','out_of_scope':'other uses'},
                     'acceptance':{'text':'Reproducible evidence.'}}
        with patch.dict(os.environ,CODEX_THREAD_ID='lead'):
            scope.apply_scope(self.root,'task',{'items':[{'scope_id':sid,'kind':'deliverable','label':sid,
                'source_refs':[self.brief]} for sid in ('a','b')], 'verified_scope_ids':['a','b'],
                'source_ref':self.brief}, self.lead)
        self.request('original',scope=['a'],recipient=None,delivery_contract=agreement)
        self.call(self.lead,'run-intent',{'id':'original-run','request_id':'original','plan_revision':1,
                  'config_ref':self.save('runtime',fixture.configuration()),'input_refs':[self.brief],
                  'target_host':'codex','operation':'start'})
        self.call(self.lead,'run-outcome',{'id':'original-run','outcome':'unknown','summary':'Unconfirmed write'})
        self.request('neighbor',scope=['a'],delivery_contract=agreement)
        self.request('independent',scope=['b'],delivery_contract=agreement)
        with (patch('harness_hosts.installed_program',return_value=self.brief),
              patch('agent_runtime.load_config',return_value={'programmatic':True,'host':{'adapter':'devin-acp'},'provider':{}}),
              patch('agent_runtime.capability',return_value={'enabled':True,'executable_found':True})):
            items = {q['request_id']:q for q in self.current(capacity=lambda host:{
                'policy_ready':True,'counts':{'occupied':{'author':1,'total':1},'queued':{}},
                'limits':{'author':6,'total':12}})['actionable']}
        self.assertEqual(set(items), {'neighbor','independent'})
        self.assertEqual(items['neighbor']['command'], 'work-dispatch --request neighbor')
        self.assertEqual(items['neighbor']['check_original_runs'][0]['command'], 'work-show --id original-run --detail')
        self.assertEqual(items['independent']['command'], 'work-dispatch --request independent --execute')
        with patch.dict(os.environ,CODEX_THREAD_ID='lead'):
            tasks.end_delegation(self.root,self.lead,'original',self.evidence['path'])
        details = json.loads(self.cli('work-show','--id','task','--scope','a').stdout)
        prior, = details['ended_authors']
        self.assertEqual(prior['original_attempt']['run_id'],'original-run')
        self.assertEqual(tasks.read_run(self.root,'original-run')['outcome'],'unknown')
        revision = tasks.read_task(self.root,'task')['task']['revision']
        self.call(self.lead,'pause',{'id':'pause','task_id':'task','expected_task_revision':revision,
                  'evidence_ref':self.evidence,'summary':'User paused the package'})
        self.assertEqual(self.current()['actionable'], [])

    def test_material_scope_separates_shared_receipts_and_preserves_original_wait(self):
        self.request('shared', scope=['a','b'])
        self.request('unit-b', scope=['b'])
        self.call(self.worker,'return',{'id':'unindexed','request_id':'shared','result_ref':self.evidence,
                  'artifact_refs':[], 'coverage':[], 'remaining_scope':['a','b'], 'summary':'Shared evidence only.'})
        self.returned('a-result','shared',['a'])
        self.returned('b-result','unit-b',['b'])
        dep = self.root / 'supplier'
        dep.write_text('version 1')
        original = self.current()
        receipt = next(i for i in original['actionable'] if i['return_id']=='b-result')
        self.assertEqual(self.cli('work-wait','--task','task','--item',receipt['item_id'],
                                  '--external','--for-file',str(dep)).returncode, 0)
        shown = json.loads(self.cli('work-show','--mine','--limit','1').stdout)['tasks']['task']
        self.assertEqual(shown['scope']['unassigned_by_request'][0]['request_id'], 'shared')
        rows = {r['scope_id']:r for r in shown['scope']['scope_items']}
        self.assertEqual(rows['a']['pending_actions'], {'adopt':1})
        self.assertEqual(rows['b']['first_wait']['dependency'], {'path':str(dep)})
        detail = json.loads(self.cli('work-show','--id','task','--scope','b').stdout)
        self.assertEqual({q['request_id'] for q in detail['authors']}, {'shared','unit-b'})
        self.assertEqual(detail['related_unindexed'][0]['request_id'], 'shared')
        self.assertEqual([i['return_id'] for i in detail['coverage_returns']['items']], ['b-result'])
        self.assertEqual(detail['waiting']['items'][0]['return_id'], 'b-result')
        shared = json.loads(self.cli('work-show','--id','shared','--limit','1','--offset','1').stdout)
        self.assertEqual([r['id'] for r in shared['returns']], ['unindexed'])
        self.assertEqual(shared['pending']['waiting_count'], 0)
        self.assertNotEqual(self.cli('work-show','--id','task','--scope','missing').returncode, 0)

    def test_wait_pages_every_new_receipt_and_reading_all_never_disposes(self):
        self.request()
        for i in range(5):
            self.returned('original-'+str(i))
        cmd = ['work-show','--mine','--limit','2']
        result = json.loads(self.cli(*cmd).stdout)
        seen = []
        for _ in range(3):
            seen.extend(i['return_id'] for i in result['new'])
            view = result['tasks']['task']
            self.assertEqual(view['actionable_count'], 5)
            self.assertLessEqual(len(view['actionable']), 2)
            result = json.loads(self.cli(*cmd,'--after',result['cursor']).stdout)
        self.assertEqual(seen, ['original-'+str(i) for i in reversed(range(5))])
        self.assertEqual((result['status'],result['new']), ('actionable',[]))

    def test_old_receipts_do_not_bury_an_independent_ready_assignment(self):
        ids = ['a','b']
        with patch.dict(os.environ, CODEX_THREAD_ID='lead'):
            scope.apply_scope(self.root, 'task', {
                'items':[{'scope_id':sid, 'kind':'deliverable', 'label':sid, 'source_refs':[self.brief],
                          'target_endpoint':'evidence'} for sid in ids],
                'verified_scope_ids':ids, 'source_ref':self.brief}, self.lead)
        self.request('older-material',scope=['a'])
        for i in range(12):
            self.returned(str(i),'older-material',['a'])
        self.request('independent-material',scope=['b'],delivery_contract={
            'purpose':'Independent product', 'endpoint':'evidence',
            'scope_boundary':{'in_scope':'b','out_of_scope':'a'}, 'acceptance':{'text':'Verified evidence.'}})
        with (patch.dict(os.environ,CODEX_THREAD_ID='lead'),
              patch('harness_hosts.installed_program',return_value=self.brief),
              patch('agent_runtime.load_config',return_value={'programmatic':True,'host':{'adapter':'devin-acp'},'provider':{}}),
              patch('agent_runtime.capability',return_value={'enabled':True,'executable_found':True})):
            capacity = lambda host:{'policy_ready':True,'counts':{'occupied':{'author':0,'total':0},'queued':{}},
                                    'limits':{'author':6,'total':12}}
            result = wave.wait(self.root,(),None,0,mine=True,capacity=capacity,limit=1)
        pending = result['tasks']['task']
        ready, = pending['actionable']
        self.assertEqual((pending['actionable_count'],ready['action'],ready['request_id']),
                         (13,'dispatch','independent-material'))
        self.assertEqual(result['new'][0]['request_id'],'independent-material')
        self.assertEqual({r['scope_id']:r['pending_actions'] for r in pending['scope']['scope_items']},
                         {'a':{'adopt':12},'b':{'dispatch':1}})

    def appointment(self):
        doc={'id':'elders','subject':wave.INTAKE,'scope':{'task':'task','object':'R','action':'intake'},
             'statement':'Decide the already funded artifact.',
             'source_ref':{**self.evidence,'locator':'file','decided_by':'main'},
             'details':{'successor_host':'codex','expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                        'decision_scope':{'task':'task','object':'R'},'decision_subject':'next-investment'}}
        with patch.dict(os.environ,CODEX_THREAD_ID='main'):
            return intake.authorize_for_host(self.root,doc,LocalRoleHost(self))

    def test_original_preferred_one_successor_original_receipts_stay(self):
        self.appointment()
        host = LocalRoleHost(self)
        self.assertEqual(intake.offer(self.root, 'task', 'R', host)['status'], 'original_inbox')
        self.assertEqual([x['action'] for x in self.current(self.main)['actionable']], ['intake'])
        host.state = 'offline'
        self.assertEqual(intake.offer(self.root, 'task', 'R', host)['status'], 'original_liveness_unresolved')
        host.state = 'unknown'
        self.assertEqual(intake.offer(self.root, 'task', 'R', host)['status'], 'original_liveness_unresolved')
        self.assertEqual(len(host.calls), 0)
        host.state = 'exited'
        with patch.object(host,'qualified_for',return_value=False):
            self.assertEqual(intake.offer(self.root,'task','R',host)['status'],'host_unqualified')
            self.assertEqual(host.calls,[])
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda n: intake.offer(self.root, 'task', 'R', host), range(2)))
        self.assertEqual(len(host.calls), 1)
        self.assertIn('successor_bound', {r['status'] for r in results})
        details = {'intake_id': 'elders', 'matter': 'R'}
        with self.assertRaisesRegex(tasks.WorkConflict, 'original confirmed holder'):
            self.decision('late', 'R', 'next-investment', 'Late original tries to decide.', details, actor=self.main)
        with self.assertRaisesRegex(tasks.WorkConflict, 'outside the authorized'):
            self.decision('extra', 'S', 'next-investment', 'New investment.', details, actor=self.other)
        first = self.decision('yes', 'R', 'next-investment', 'Use the existing approved allocation.', details, actor=self.other)
        self.assertEqual(self.decision('yes', 'R', 'next-investment', 'Use the existing approved allocation.',
                                       details, actor=self.other), first)
        self.assertEqual(intake.offer(self.root, 'task', 'R', host)['status'], 'handled')

    def test_stop_committed_before_start_has_zero_native_effect(self):
        self.appointment()
        host = LocalRoleHost(self)
        host.state = 'exited'
        tx = tasks.transaction
        entered = 0

        def between(*args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                with patch.object(tasks, 'transaction', tx):
                    self.call(self.lead, 'pause', {'id': 'pause', 'task_id': 'task', 'expected_task_revision': 1,
                              'evidence_ref': self.evidence, 'summary': 'Original stop.'})
            return tx(*args, **kwargs)

        with patch.object(tasks, 'transaction', between):
            result = intake.offer(self.root, 'task', 'R', host)
        self.assertEqual(result['status'], 'stopped_before_start')
        self.assertEqual(host.calls, [])

    def test_cold_start_lost_reply_reconciles_original_attempt_once(self):
        self.appointment()
        host=LocalRoleHost(self)
        host.state='exited'
        start=host.start
        def lost(intent,decision,original):
            start(intent,decision,original)
            return None
        with patch.object(host,'start',lost):
            original=intake.offer(self.root,'task','R',host)
            self.assertEqual(original['status'],'reconcile_original_intent')
            self.assertEqual(intake.offer(self.root,'task','R',host)['status'],'reconcile_original_intent')
            host.read_attempt=lambda attempt: {'attempt_id':attempt,'successor':self.other}
            reconciled=intake.offer(self.root,'task','R',host)
        self.assertTrue(reconciled['recovered'])
        self.assertEqual(host.calls,[original['attempt_id']])


class LocalRoleHost:
    """Isolated receiver with independent observations of exit and start."""
    def __init__(self, test):
        self.state = 'available'
        self.calls = []
        self.test = test

    def qualified_for(self, host):
        return host == 'codex'

    def observe(self, actor):
        return {'state': self.state, 'actor': actor, 'source_ref': self.test.evidence}

    def still_exited(self, actor, receipt):
        return self.state == 'exited'

    def start(self, intent, decision, original):
        self.calls.append(intent['attempt_id'])
        return self.test.other
