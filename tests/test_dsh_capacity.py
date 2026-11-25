"""Real isolated processes and public queries; no production sessions/models."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading
import unittest
from unittest.mock import patch

import dsh_capacity as capacity
import execution_lease
import harness_tasks as tasks
import test_agent_runtime as fixture
import test_dsh_headless as dsh_fixture
from agent_base import StopRequested

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / 'tests/dsh_capacity_fixture.py'


class CapacityTests(unittest.TestCase):
    ref = fixture.RuntimeTests.ref
    create_request = fixture.RuntimeTests.create_request

    def setUp(self):
        fixture.RuntimeTests.setUp(self)
        self.managers, self.native_pids, self.native_handles = [], [], []
        self.number = 0
        self.addCleanup(self.stop_test_processes)

    def stop_test_processes(self):
        for marker in self.root.glob('*.native-ready'):
            self.native_pids.append(int(marker.read_text()))
            (self.root / (marker.name.removesuffix('.native-ready') + '.release')).touch()
        for marker in self.root.glob('*.manager-ready'):
            (self.root / (marker.name.removesuffix('.manager-ready') + '.release')).touch()
        for process in self.managers:
            if process.poll() is None:
                process.terminate()
        for process in self.managers:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
        for pid in set(self.native_pids):
            try:
                if execution_lease._process_markers(pid).get('HTAM_DSH_CAPACITY_ROOT') == str(self.root):
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        for process in self.native_handles:
            process.wait(timeout=5)

    def seed(self, kind='author', *, task_id='task', host=capacity.DSH):
        self.number += 1
        run_id, request_id = 'capacity-run-' + str(self.number), 'capacity-request-' + str(self.number)
        if task_id != 'task':
            tasks.mutate(self.root, 'create', self.actor, {'id': task_id, 'owner_actor': self.actor,
                'original_ref': self.evidence, 'plan_ref': self.evidence, 'summary': 'Other isolated package', 'required_scope': []})
        review = {}
        if kind == 'review':
            # Queue load uses separate scientific units. The three-round
            # autonomy budget is tested against one unit in test_wave_delivery.
            parent = 'capacity-subject-' + str(self.number)
            self.create_request(id=parent,task_id=task_id,required_scope=[])
            review = {'kind':'review','independent':True,'parent_request_id':parent}
        self.create_request(id=request_id, task_id=task_id, required_scope=[], **review)
        home = self.root / 'work' / run_id
        home.mkdir(parents=True)
        launch = home / 'launch.json'
        launch.write_text(json.dumps({'run_id': run_id, 'request_id': request_id,
                                     'native_cwd': str(self.root), 'work_home': str(home)}))
        tasks.mutate(self.root, 'run-intent', self.actor, {'id': run_id, 'request_id': request_id,
            'config_ref': self.ref(self.config_path), 'input_refs': [self.input_ref, self.ref(launch)],
            'target_host': host, 'operation': 'start'})
        return run_id, request_id, home

    def test_busy_capacity_flush_preserves_original_transition_and_can_be_replayed(self):
        run_id,_,_=self.seed()
        queue_path=capacity._paths(self.root)[0]
        event={'id':run_id,'kind':'author','phase':'admitted','sequence':5,'queue_source':str(queue_path)}
        with tasks.transaction(self.root):
            started=time.monotonic()
            capacity.record_capacity(self.root,self.actor,event)
            self.assertLess(time.monotonic()-started,1)
            pending,=capacity._accounting_home(self.root,capacity.DSH).glob('*.json')
            fact=json.loads(pending.read_text())
            self.assertFalse(any(e['kind']=='capacity' for e in tasks.read_run(self.root,run_id)['events']))
        self.assertTrue(capacity.flush_accounting(self.root))
        self.assertFalse(pending.exists())
        execution_lease.atomic_json(pending,fact)
        self.assertTrue(capacity.flush_accounting(self.root))
        logged,=[e for e in tasks.read_run(self.root,run_id)['events'] if e['kind']=='capacity']
        self.assertEqual(logged['created_at'],fact['at'])
        self.assertEqual(logged['payload']['phase'],'admitted')

    def start(self, kind='author', *, ready=True, mode='manager', task_id='task', host=capacity.DSH):
        run_id, request_id, home = self.seed(kind, task_id=task_id, host=host)
        error = (self.root / (run_id + '.stderr')).open('w')
        process = subprocess.Popen([sys.executable, str(DRIVER), mode, str(self.root), run_id, kind, request_id],
            env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=error)
        error.close()
        self.managers.append(process)
        if ready:
            self.until(lambda: (self.root / (run_id + '.native-ready')).exists(), run_id)
        return run_id, process, home

    def until(self, condition, label, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            exited = [(p.pid, p.returncode) for p in self.managers if p.poll() not in (None, 0)]
            if exited:
                errors = '\n'.join(p.read_text() for p in self.root.glob('*.stderr'))
                self.fail(str(exited) + '\n' + errors)
            time.sleep(.06)
        errors='\n'.join(p.read_text() for p in self.root.rglob('transport-handoff.stderr'))
        self.fail('Timed out waiting for ' + label + ('\n'+errors if errors else ''))

    def view(self):
        return capacity.snapshot(self.root)

    def release(self, run_id, process):
        (self.root / (run_id + '.release')).touch()
        process.wait(timeout=15)

    def queued(self, run_id):
        return any(e['run_id'] == run_id for e in self.view()['waiting'])

    def test_pilot_selects_earliest_eligible_across_roles_and_keeps_four_total(self):
        from dsh_capacity_handoff import provider_pilot
        authority=self.evidence['path']
        tasks.mutate(self.root,'grant-role',self.actor,{'id':'maintainer','actor':self.actor,
            'org_role':'maintainer','evidence_ref':self.evidence})
        self.assertTrue(provider_pilot(self.root,authority,'configured-dsh',dry_run=True)['eligible'])
        provider_pilot(self.root,authority,'configured-dsh')
        authors=[self.start() for _ in range(3)]
        first=self.start(ready=False)
        self.until(lambda:self.queued(first[0]),'fourth author queued')
        initial_review=self.start('review')  # earlier author is blocked by its role cap
        short=self.start('review',ready=False)
        self.until(lambda:self.queued(short[0]),'fifth invocation queued')
        self.assertEqual(self.view()['provider_pilot']['occupied'],{'author':3,'review':1,'total':4})
        self.assertFalse(provider_pilot(self.root,authority,dry_run=True)['eligible'])
        self.release(*authors[0][:2])
        self.until(lambda:(self.root/(first[0]+'.native-ready')).exists(),'oldest now eligible author')
        self.assertTrue(self.queued(short[0]))
        # A slow ordinary review never grants the short revision priority.
        self.release(*initial_review[:2])
        self.until(lambda:(self.root/(short[0]+'.native-ready')).exists(),'next eligible review')
        for entry in [*authors[1:],first,short]:self.release(*entry[:2])
        self.assertTrue(provider_pilot(self.root,authority,dry_run=True)['eligible'])
        provider_pilot(self.root,authority)
        self.assertNotIn('provider_pilot',self.view())

    def test_eight_seat_provider_limit_and_eligible_fifo(self):
        policy=capacity.EIGHT_SEAT_POLICY
        provider='tiangong-cluster'
        def ticket(kind,sequence):
            return {'kind':kind,'sequence':sequence,'phase':'queued','provider':provider,
                    'admission_policy':policy,'run_id':str(sequence)}
        def reason(authors,reviews,kind,earlier=()):
            active=[{'kind':role,'provider':provider}
                    for role,count in [('author',authors),('review',reviews)] for _ in range(count)]
            entry=ticket(kind,9)
            queue={'policy':policy,'pilot_provider':provider,'entries':[*earlier,entry]}
            return capacity._waiting_reason(entry,capacity._pilot_counts(active,provider),queue,{},active)
        for authors,reviews in ((5,3),(4,4),(3,5)):
            self.assertEqual(reason(authors,reviews,'review'),'provider_total_capacity')
        self.assertEqual(reason(5,0,'author'),'author_capacity')
        self.assertEqual(reason(0,5,'review'),'review_capacity')
        self.assertIsNone(reason(5,2,'review',[ticket('author',1)]))
        self.assertEqual(reason(4,2,'review',[ticket('author',1)]),'earlier_eligible_provider_request')

    def test_six_worker_and_six_review_seats_across_packages_and_public_query(self):
        held=[self.start(kind,task_id='other-package' if i==3 else 'task')
              for i,kind in enumerate(['author']*6+['review']*6)]
        extra,pending,_=self.start(ready=False)
        self.until(lambda:self.queued(extra),'seventh worker')
        extra_review,_,_=self.start('review',ready=False)
        self.until(lambda:self.queued(extra_review),'seventh reviewer')
        queried=subprocess.run([sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),
            'work-capacity'],capture_output=True,text=True,timeout=15)
        self.assertEqual(queried.returncode,0,queried.stderr)
        data=json.loads(queried.stdout)
        self.assertEqual(data['limits'],{'author':6,'review':6,'total':12})
        self.assertEqual(data['counts']['occupied'],{'author':6,'review':6,'total':12})
        self.assertEqual(data['counts']['queued'],{'author':1,'review':1})
        self.assertTrue(data['policy_ready'])
        tasks.mutate(self.root,'run-stop',self.actor,{'id':extra,'summary':'Cancel original isolated waiter'})
        self.until(lambda:(self.root/(extra+'.cancelled')).exists(),'original queued stop')
        pending.wait(timeout=5)
        self.release(*held[0][:2])
        self.assertFalse((self.root/(extra_review+'.native-ready')).exists(),'Idle worker seat cannot enlarge the review pool')
        self.release(*held[6][:2])
        self.until(lambda:(self.root/(extra_review+'.native-ready')).exists(),'next reviewer FIFO head')
        self.assertFalse((self.root/(extra+'.native-ready')).exists())
        self.assertTrue(tasks.read_run(self.root,extra)['stop_requested'])

    def test_full_worker_pool_does_not_block_later_reviewer(self):
        held=[self.start() for _ in range(6)]
        worker,_,_=self.start(ready=False)
        self.until(lambda:self.queued(worker),'seventh worker')
        review,_,_=self.start('review')
        self.assertEqual(self.view()['counts']['occupied'],{'author':6,'review':1,'total':7})
        self.assertFalse((self.root/(worker+'.native-ready')).exists())

    def test_six_authors_yield_for_own_reviews_and_reacquire_worker_pool(self):
        authors=[self.start(mode='controlled-wait') for _ in range(6)]
        self.until(lambda:all(tasks.read_run(self.root,a[0])['native_identity'] for a in authors),'original Session bindings')
        # Earlier turns of the same Session must yield with its current owner,
        # rather than manufacturing replacement occupied seats.
        for author,_,_ in authors:
            history,_,_=self.seed()
            with tasks.transaction(self.root) as c:
                c.execute("UPDATE work_runs SET native_identity_json=?,outcome='unknown' WHERE id=?",
                    (tasks._json(tasks.read_run(self.root,author)['native_identity']),history))
        for run,_,_ in authors:(self.root/(run+'.begin-wait')).touch()
        self.until(lambda:len(self.view()['waiting_review'])==6,'six protected controlled waits')
        self.assertEqual(self.view()['counts']['occupied']['total'],0)
        for run,_,_ in authors:
            self.assertEqual(execution_lease.observe_run(self.root,tasks.read_run(self.root,run))['state'],'active')
        reviews=[self.start('review') for _ in range(6)]
        self.assertEqual(self.view()['counts']['occupied'],{'author':0,'review':6,'total':6})
        for run,_,_ in authors:(self.root/(run+'.review-done')).touch()
        self.until(lambda:all((self.root/(a[0]+'.reacquired')).exists() for a in authors),'all own reviews returned')
        self.assertEqual(self.view()['counts']['occupied'],{'author':6,'review':6,'total':12})
        for review,process,_ in reviews:self.release(review,process)
        for author,process,_ in authors:self.release(author,process)

    def test_wait_marker_without_lease_proof_or_live_manager_cannot_free_native_owner(self):
        run,manager,home=self.start(mode='controlled-wait')
        (self.root/(run+'.begin-wait')).touch()
        self.until(lambda:len(self.view()['waiting_review'])==1,'verified controlled wait')
        path=execution_lease.metadata_path(home,run);meta=json.loads(path.read_bytes())
        self.assertEqual(self.view()['counts']['occupied']['total'],0)
        changed={k:v for k,v in meta.items() if k!='capacity_wait'}
        execution_lease.atomic_json(path,changed)
        self.assertEqual(self.view()['counts']['occupied']['total'],1)
        execution_lease.atomic_json(path,meta)
        manager.kill();manager.wait(timeout=5);self.managers.remove(manager)
        self.assertEqual(self.view()['counts']['occupied']['total'],1)
        self.assertEqual(execution_lease.observe_run(self.root,tasks.read_run(self.root,run))['state'],'active')

    def test_retire_review_wait_requires_exit_of_manager_writer_and_tools(self):
        import dsh_capacity_handoff as handoff
        dsh_home = self.home / 'native-writers'
        config = json.loads(self.config_path.read_bytes())
        config['hosts']['fixture-dsh'] = {'adapter': 'dsh-headless', 'dsh_home': str(dsh_home)}
        self.config_path.write_text(json.dumps(config))
        tasks.mutate(self.root, 'grant-role', self.actor, {'id': 'maintainer', 'actor': self.actor,
                     'org_role': 'maintainer', 'evidence_ref': self.evidence})
        run_id, manager, home = self.start(mode='controlled-wait')
        (self.root / (run_id + '.begin-wait')).touch()
        self.until(lambda: self.view()['waiting_review'], 'original controlled wait')
        path = execution_lease.metadata_path(home, run_id)
        meta = json.loads(path.read_bytes())
        source = self.evidence['path']
        with capacity._lock(self.root):
            queue = capacity._queue(self.root)
            queue.update(policy=capacity.PREVIOUS_POLICY, admission_policy=capacity.POLICY)
            queue['entries'][0].update(policy=capacity.PREVIOUS_POLICY, phase='queued')
            queue['entries'][0].pop('admission_policy')
            execution_lease.atomic_json(capacity._paths(self.root)[0], queue)
        self.assertEqual(handoff.retire_exited_waits(self.root, source, dry_run=True)['exited_waits'], [])
        tasks.mutate(self.root, 'run-stop', self.actor, {'id': run_id, 'summary': 'Original test stop'})
        manager.kill(); manager.wait(timeout=5); self.managers.remove(manager)
        self.assertEqual(handoff.retire_exited_waits(self.root, source, dry_run=True)['exited_waits'], [])
        (self.root / (run_id + '.release')).touch()
        self.until(lambda: execution_lease.execution_observation(path)['state'] == 'exited', 'native group exit')
        lock = dsh_home / 'sessions' / 'cwd-fixture' / meta['native_identity']['id'] / 'session.lock'
        lock.parent.mkdir(parents=True)
        with lock.open('w') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(handoff.retire_exited_waits(self.root, source)['retired'], [])
        tool = subprocess.Popen([sys.executable, str(DRIVER), 'child', str(self.root), run_id + '.tool'],
                                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.native_handles.append(tool)
        path_data = {**meta, 'tools': {'known': {'phase': 'spawned', 'process': execution_lease.process_identity(tool.pid)}}}
        execution_lease.atomic_json(path, path_data)
        self.assertEqual(handoff.retire_exited_waits(self.root, source)['retired'], [])
        self.release(run_id + '.tool', tool)
        execution_lease.atomic_json(path, {**meta, 'tools': {'unobserved': {'phase': 'launch_intent'}}})
        self.assertEqual(handoff.retire_exited_waits(self.root, source)['retired'], [])
        execution_lease.atomic_json(path, meta)
        with tasks.transaction(self.root) as c:
            c.execute("UPDATE work_runs SET outcome='unknown' WHERE id=?", (run_id,))
        before = tasks.read_run(self.root, run_id)
        dry = handoff.retire_exited_waits(self.root, source, dry_run=True)
        self.assertEqual([e['run_id'] for e in dry['exited_waits']], [run_id])
        self.assertEqual(dry['retired'], [])
        self.assertEqual(len(capacity._queue(self.root)['entries']), 1)
        after = handoff.retire_exited_waits(self.root, source)
        self.assertEqual(after['retired'], [run_id])
        self.assertEqual(after['protocol_policy'], capacity.POLICY)
        self.assertEqual(handoff.retire_exited_waits(self.root, source)['retired'], [])
        retained = tasks.read_run(self.root, run_id)
        for key in ('outcome', 'stop_requested', 'native_identity', 'latest_receipt'):
            self.assertEqual(retained[key], before[key])
        self.assertEqual(retained['events'][-1]['payload']['reason'], 'review_wait_execution_exited')
        self.assertEqual(json.loads(path.read_bytes()), meta)

    def test_fifo_and_cancelled_head_does_not_block_later_original_request(self):
        held = [self.start() for _ in range(6)]
        first, first_process, _ = self.start(ready=False)
        self.until(lambda: self.queued(first), 'first queue ticket')
        second, second_process, _ = self.start(ready=False)
        self.until(lambda: self.queued(second), 'second queue ticket')
        self.release(held[0][0], held[0][1])
        self.until(lambda: (self.root / (first + '.native-ready')).exists(), 'FIFO head')
        self.assertFalse((self.root / (second + '.native-ready')).exists())
        self.release(first, first_process)
        self.until(lambda: (self.root / (second + '.native-ready')).exists(), 'same second ticket')
        phases = [e['payload']['phase'] for e in tasks.read_run(self.root, second)['events'] if e['kind'] == 'capacity']
        self.assertEqual(phases, ['queued', 'admitted'])

    def test_queued_original_run_is_readable_before_native_identity_and_on_resume(self):
        from agent_runtime import Runtime
        from scientific_review.author_tools import ReviewService
        import run_worker
        held=[self.start('review') for _ in range(6)]
        pending,_,home=self.start('review',ready=False)
        self.until(lambda:self.queued(pending),'third review waiting')
        run=tasks.read_run(self.root,pending)
        request=tasks.read_request(self.root,run['request_id'])
        worker=Runtime(self.root,adapter_factory=lambda *args: self.fail('Queued run must not read or execute a native turn'))
        result=run_worker._program_read(self.root,worker,request,run)
        self.assertEqual(result['execution_state'],'queued')
        self.assertIsNone(result['native_identity'])
        self.assertEqual(result['capacity']['waiting_reason'],'review_capacity')
        service=ReviewService(worker,tasks.read_request(self.root,'request'),{},'unused',home,lambda:None)
        self.assertEqual(service.result({'review_id':request['id']})['state'],'queued')
        queried=subprocess.run([sys.executable,str(REPO/'bin/agent_runtime.py'),'--root',str(self.root),
            'readresult','--run-id',pending],capture_output=True,text=True,timeout=15)
        self.assertEqual(queried.returncode,0,queried.stderr)
        self.assertEqual(json.loads(queried.stdout)['execution_state'],'queued')
        # A resume waiting for a seat must not read its previous turn as this result.
        native={'host':'deepseek_harness','kind':'session','id':'session-original',
                'instance':'session-original','cwd':str(self.root),'turn_id':'1'}
        with tasks.transaction(self.root) as c:
            c.execute('UPDATE work_runs SET native_identity_json=? WHERE id=?',(tasks._json(native),pending))
        result=run_worker._program_read(self.root,worker,request,tasks.read_run(self.root,pending))
        self.assertEqual(result['execution_state'],'queued')
        self.assertEqual(result['native_identity'],native)
        self.assertEqual(tasks.read_run(self.root,pending)['outcome'],'intent')
        self.assertFalse((self.root/(pending+'.native-ready')).exists())

    def test_unobserved_launch_without_ticket_is_not_reported_as_queued(self):
        from agent_runtime import Runtime
        run,_,home=self.seed('review')
        result=Runtime(self.root).readresult(run_id=run,role='scientific_reviewer',config_path=None,evidence_home=str(home))
        self.assertEqual(result['execution_state'],'launch_unresolved')
        self.assertFalse(result['replay_allowed'])
        self.assertNotIn('capacity',result)
        self.assertEqual(tasks.read_run(self.root,run)['outcome'],'intent')

    def test_other_hosts_do_not_depend_on_dsh_queue_readability(self):
        capacity._paths(self.root)[0].write_text('broken DSH-only queue fixture')
        for host in ('codex','claude_code','devin'):
            self.assertIsNone(capacity.waiting_run(self.root,{'id':'other-run','target_host':host}))

    def test_parent_exit_keeps_native_child_and_start_crash_window_reserved(self):
        held = [self.start(mode='crash-window' if i == 0 else 'manager') for i in range(6)]
        original, manager, _ = held[0]
        manager.kill(); manager.wait(timeout=5)
        self.managers.remove(manager)
        later, _, _ = self.start(ready=False)
        self.until(lambda: self.queued(later), 'wait behind surviving native process')
        self.assertFalse((self.root / (later + '.native-ready')).exists())
        self.assertEqual(self.view()['counts']['occupied']['author'], 6)
        (self.root / (original + '.release')).touch()
        self.until(lambda: (self.root / (later + '.native-ready')).exists(), 'released orphan reservation')

    def test_first_session_binding_does_not_count_execution_and_reservation_twice(self):
        run_id,_,_=self.start()
        native={'host':'deepseek_harness','kind':'session','id':'session-first-bound',
                'instance':'session-first-bound','cwd':str(self.root),'turn_id':'1'}
        with tasks.transaction(self.root) as c:
            c.execute('UPDATE work_runs SET native_identity_json=? WHERE id=?',(tasks._json(native),run_id))
        capacity._paths(self.root)[2].unlink(missing_ok=True)  # Discard only the isolated derived cache.
        view=self.view()
        self.assertEqual(view['counts']['occupied'],{'author':1,'review':0,'total':1})
        self.assertEqual([row['run_id'] for row in view['active']],[run_id])

    def test_native_exit_and_detached_tool_keep_slot_after_manager_exit(self):
        held = [self.start() for _ in range(5)]
        original, manager, _ = self.start(mode='detached-tool')
        manager.wait(timeout=15)
        self.until(lambda: (self.root / (original + '.tool.native-ready')).exists(), 'detached test tool')
        later, _, _ = self.start(ready=False)
        self.until(lambda: self.queued(later), 'tool still owns sixth slot')
        self.assertEqual(self.view()['counts']['occupied']['author'], 6)
        (self.root / (original + '.tool.release')).touch()
        self.until(lambda: (self.root / (later + '.native-ready')).exists(), 'tool natural exit')

    def test_dead_waiting_manager_is_abandoned_without_auto_start(self):
        held = [self.start() for _ in range(6)]
        dead, process, _ = self.start(ready=False)
        self.until(lambda: self.queued(dead), 'stale test waiter')
        process.kill(); process.wait(timeout=5);self.managers.remove(process)
        next_id, _, _ = self.start(ready=False)
        self.until(lambda: self.queued(next_id), 'next waiter')
        self.release(held[0][0], held[0][1])
        self.until(lambda: (self.root / (next_id + '.native-ready')).exists(), 'next after abandoned')
        self.assertFalse((self.root / (dead + '.native-ready')).exists())
        self.assertTrue(any(e['kind'] == 'capacity' and e['payload']['phase'] == 'abandoned'
                            for e in tasks.read_run(self.root, dead)['events']))

    def test_ended_unknown_run_is_not_a_slot_and_live_old_intent_is(self):
        run_id, request_id, home = self.seed()
        with tasks.transaction(self.root) as c:
            c.execute("UPDATE work_runs SET outcome='unknown' WHERE id=?", (run_id,))
        self.assertEqual(self.view()['counts']['occupied']['total'], 0)
        request = tasks.read_request(self.root, request_id)
        with execution_lease.ExecutionLease(self.root, request, home, run_id) as lease:
            lease.intent()
            self.assertEqual(self.view()['counts']['occupied']['author'], 1)
            lease.settled()
        self.assertEqual(self.view()['counts']['occupied']['total'], 0)

    def test_capacity_rejects_reused_groups_and_rechecks_a_child_missing_from_the_os_sample(self):
        run_id, request_id, home = self.seed('review')
        manager = {'pid':401,'pgid':401,'ppid':1,'start':'Wed Nov 18 01:00:00 2026','boot':'same-boot'}
        child = {'pid':402,'pgid':402,'ppid':401,'start':'Wed Nov 18 01:01:00 2026','boot':'same-boot'}
        path = execution_lease.metadata_path(home, run_id)
        path.parent.mkdir(parents=True)
        metadata = {'run_id':run_id, 'request_id':request_id, 'child':child, 'manager':manager, 'phase':'settled'}
        execution_lease.atomic_json(path, metadata)
        replacement = {**child, 'ppid':1, 'start':'Wed Nov 18 01:02:00 2026'}
        with patch.object(execution_lease, 'boot_identity', return_value='same-boot'), \
             patch.object(execution_lease, '_process_markers', return_value={}), \
             patch.object(capacity, '_processes', return_value={402:replacement}):
            self.assertEqual(self.view()['counts']['occupied']['review'], 0)
        execution_lease.atomic_json(path, {**metadata, 'phase':'spawned'})
        with patch.object(execution_lease, 'boot_identity', return_value='same-boot'), \
             patch.object(execution_lease, '_process_markers', return_value={}), \
             patch.object(capacity, '_processes', return_value={401:manager}), \
             patch.object(execution_lease, 'original_group', return_value={'state':'active'}) as observe:
            self.assertEqual(self.view()['counts']['occupied']['review'], 1)
            observe.assert_called_once_with(child)
    def test_original_native_writer_counts_without_manager_metadata_or_terminal(self):
        config=json.loads(self.config_path.read_text())
        config['hosts']['dsh']={'adapter':'dsh-headless','dsh_home':str(self.home)}
        self.config_path.write_text(json.dumps(config))
        run_id,_,_=self.seed()
        native={'host':'deepseek_harness','kind':'session','id':'session-legacy-writer',
                'instance':'session-legacy-writer','cwd':str(self.root),'turn_id':'1'}
        with tasks.transaction(self.root) as c:
            c.execute("UPDATE work_runs SET outcome='unknown',native_identity_json=? WHERE id=?",(tasks._json(native),run_id))
        lock=self.home/'sessions/project/session-legacy-writer/session.lock'
        lock.parent.mkdir(parents=True);lock.touch()
        with lock.open('r') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.view()['counts']['occupied']['total'],1)
        self.assertEqual(self.view()['counts']['occupied']['total'],0)

    def test_multiprocess_shared_readers_do_not_become_writers_or_duplicate_sessions(self):
        config=json.loads(self.config_path.read_text())
        config['hosts']['dsh']={'adapter':'dsh-headless','dsh_home':str(self.home)}
        self.config_path.write_text(json.dumps(config))
        native={'host':'deepseek_harness','kind':'session','id':'session-shared-readers',
            'instance':'session-shared-readers','cwd':str(self.root),'turn_id':'1'}
        for _ in range(4):
            run,_,_=self.seed()
            with tasks.transaction(self.root) as c:
                c.execute("UPDATE work_runs SET outcome='unknown',native_identity_json=? WHERE id=?",(tasks._json(native),run))
        lock=self.home/'sessions/project/session-shared-readers/session.lock'
        lock.parent.mkdir(parents=True);lock.touch()
        script='import fcntl,sys;f=open(sys.argv[1]);fcntl.flock(f,fcntl.LOCK_SH);print("ready",flush=True);sys.stdin.read(1)'
        readers=[subprocess.Popen([sys.executable,'-c',script,str(lock)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True) for _ in range(4)]
        try:
            for reader in readers:self.assertEqual(reader.stdout.readline().strip(),'ready')
            for _ in range(3):self.assertEqual(self.view()['counts']['occupied']['total'],0)
        finally:
            for reader in readers:reader.communicate('x',timeout=5)
        with lock.open() as writer:
            fcntl.flock(writer,fcntl.LOCK_EX|fcntl.LOCK_NB)
            view=self.view();self.assertEqual(view['counts']['occupied']['total'],1)
            self.assertEqual(view['occupancy_counts']['unknown_reservation']['author'],1)

    def test_unverified_writer_ownership_is_a_distinct_reservation(self):
        observed={'processes':[],'boot':'test-boot','at':1,'attribution_issues':[],
            'occupied':[{'run_id':'unknown','native_session':'same','kind':'author','reason':'native_writer_ownership_unverified'}]}
        active,counts,_=capacity._effective({'entries':[]},observed)
        self.assertEqual(counts['total'],1)
        self.assertEqual(active[0]['occupancy'],'unknown_reservation')

    def test_legacy_review_occupancy_obeys_its_pool_without_consuming_worker_seats(self):
        # Old managers already executing before admission existed: no queue tickets.
        legacy = []
        for _ in range(6):
            run_id, _, home = self.seed('review')
            process = subprocess.Popen([sys.executable, str(DRIVER), 'child', str(self.root), run_id],
                env={**os.environ, 'HTAM_DSH_CAPACITY_ROOT':str(self.root), 'HTAM_DSH_CAPACITY_RUN_ID':run_id},
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.native_pids.append(process.pid)
            self.native_handles.append(process)
            execution_lease.atomic_json(execution_lease.metadata_path(home, run_id),
                {'phase': 'spawned', 'run_id': run_id, 'child': execution_lease.process_identity(process.pid)})
            legacy.append((run_id, process))
        author, _, _ = self.start()
        queued_review, _, _ = self.start('review', ready=False)
        self.until(lambda: self.queued(queued_review), 'review over its old limit')
        self.assertEqual(self.view()['counts']['occupied'], {'author': 1, 'review': 6, 'total': 7})
        self.release(*legacy[0])
        self.until(lambda: (self.root / (queued_review + '.native-ready')).exists(), 'natural review降载')

    def test_review_fifo(self):
        held = [self.start('review') for _ in range(6)]
        first, first_process, _ = self.start('review', ready=False)
        self.until(lambda: self.queued(first), 'first independent review')
        second, _, _ = self.start('review', ready=False)
        self.until(lambda: self.queued(second), 'second independent review')
        self.release(held[0][0], held[0][1])
        self.until(lambda: (self.root / (first + '.native-ready')).exists(), 'first review FIFO')
        self.assertFalse((self.root / (second + '.native-ready')).exists())
        self.release(first, first_process)
        self.until(lambda: (self.root / (second + '.native-ready')).exists(), 'second review FIFO')

    def test_legacy_worker_excess_drains_naturally_and_does_not_block_free_review_pool(self):
        legacy = []
        for kind in ['author'] * 7 + ['review'] * 2:
            run_id, _, home = self.seed(kind)
            process = subprocess.Popen([sys.executable, str(DRIVER), 'child', str(self.root), run_id],
                env={**os.environ, 'HTAM_DSH_CAPACITY_ROOT':str(self.root), 'HTAM_DSH_CAPACITY_RUN_ID':run_id},
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.native_handles.append(process);self.native_pids.append(process.pid)
            execution_lease.atomic_json(execution_lease.metadata_path(home, run_id),
                {'phase':'spawned', 'run_id':run_id, 'child':execution_lease.process_identity(process.pid)})
            legacy.append((run_id, process))
        pending, _, _ = self.start(ready=False)
        self.until(lambda: self.queued(pending), 'old nine executions')
        self.assertEqual(self.view()['counts']['occupied']['total'], 9)
        self.start('review')
        self.assertEqual(self.view()['counts']['occupied']['review'],3)
        self.start('review');self.start('review')
        review,_,_=self.start('review',ready=False)
        self.until(lambda:self.queued(review),'review waits at total twelve despite its free role seat')
        self.assertEqual(self.view()['counts']['occupied']['total'],12)
        self.assertEqual(next(e for e in self.view()['waiting'] if e['run_id']==review)['waiting_reason'],
                         'workspace_total_capacity')
        self.release(*legacy[0])
        self.until(lambda:(self.root/(review+'.native-ready')).exists(),'review uses naturally freed total seat')
        self.assertFalse((self.root / (pending + '.native-ready')).exists(), 'Six existing workers still fill their own pool')
        self.release(*legacy[1])
        self.until(lambda: (self.root / (pending + '.native-ready')).exists(), 'natural total capacity return')

    def test_real_observation_unknown_waits_and_can_cancel_without_failure(self):
        run_id, request_id, home = self.seed()
        failures = []
        def guard():
            if tasks.read_run(self.root, run_id)['stop_requested']:
                raise StopRequested('Explicit isolated cancellation', run_id)
        with execution_lease.ExecutionLease(self.root, tasks.read_request(self.root, request_id), home, run_id) as lease:
            lease.intent()
            def wait():
                try:
                    with capacity.Capacity(self.root, lease, 'author', self.actor).admit(guard):
                        failures.append('Capacity must not be guessed from an unavailable OS observation')
                except StopRequested:
                    pass
                except Exception as exc:
                    failures.append(repr(exc))
            with patch.object(capacity, '_observe', side_effect=OSError('OS observation unavailable')):
                thread = threading.Thread(target=wait)
                thread.start()
                self.until(lambda: len(capacity._queue(self.root)['entries']) == 1, 'unknown capacity waiter')
                view = self.view()
                self.assertIsNone(view['counts']['occupied'])
                self.assertEqual(view['waiting'][0]['waiting_reason'], 'execution_ownership_unavailable')
                self.assertEqual(tasks.read_run(self.root, run_id)['outcome'], 'intent')
                tasks.mutate(self.root, 'run-stop', self.actor, {'id': run_id, 'summary': 'Stop waiting'})
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])

    def test_duplicate_capacity_call_does_not_remove_original_ticket(self):
        run_id, request_id, home = self.seed()
        with execution_lease.ExecutionLease(self.root, tasks.read_request(self.root, request_id), home, run_id) as lease:
            lease.intent()
            with capacity.Capacity(self.root, lease, 'author', self.actor).admit(lambda: None):
                with self.assertRaisesRegex(ValueError, 'already owns'):
                    with capacity.Capacity(self.root, lease, 'author', self.actor).admit(lambda: None):
                        self.fail('Duplicate original ticket admitted')
                self.assertEqual([e['run_id'] for e in capacity._queue(self.root)['entries']], [run_id])


class EntrypointTests(unittest.TestCase):
    ref = fixture.RuntimeTests.ref
    seed = CapacityTests.seed
    start = CapacityTests.start
    until = CapacityTests.until
    view = CapacityTests.view
    queued = CapacityTests.queued
    release = CapacityTests.release
    stop_test_processes = CapacityTests.stop_test_processes
    _stream = dsh_fixture.DeepSeekProgramTests._stream
    _dispatch = dsh_fixture.DeepSeekProgramTests._dispatch

    def create_request(self, **changes):
        if changes:
            return fixture.RuntimeTests.create_request(self, **changes)
        return dsh_fixture.DeepSeekProgramTests.create_request(self)

    def setUp(self):
        dsh_fixture.DeepSeekProgramTests.setUp(self)
        self.managers, self.native_pids, self.native_handles, self.number = [], [], [], 0
        self.addCleanup(self.stop_test_processes)

    def test_same_family_dsh_reviewer_has_a_fresh_readonly_session(self):
        held = [self.start(task_id='held-package-' + str(i)) for i in range(5)]
        # The sixth worker yields and reacquires its own pool; its reviewer
        # advances in the separate reviewer pool with all original leases alive.
        dsh_fixture.DeepSeekProgramTests.test_same_family_dsh_reviewer_has_a_fresh_readonly_session(self)
        self.until(lambda: self.view()['counts']['occupied'] == {'author': 5, 'review': 0, 'total': 5},
                   'only the five held authors remain')

    def test_public_dispatch_start_and_resume_each_queue_once_on_the_same_session(self):
        holders = [self.start() for _ in range(6)]
        self._stream(final='First original Session report.')
        command = [sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),
                   'work-dispatch','--request','request','--config',str(self.config_path),'--execute']
        first = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
        self.managers.append(first)
        self.until(lambda: any(e['request_id']=='request' for e in self.view()['waiting']), 'public first dispatch')
        self.assertFalse(self.argv_log.exists(), 'Queued dispatch has not called the native CLI')
        self.release(holders[0][0],holders[0][1])
        output,error=first.communicate(timeout=20)
        self.assertEqual(first.returncode,0,error)
        original=json.loads(output)
        self.assertEqual(original['outcome'],'succeeded',original)
        holders.append(self.start())
        self._stream(turn=5,final='Continued the same original Session.')
        instructions=self.home/'original-continuation.md'
        instructions.write_text('Continue the exact original bounded task; retain all original history.')
        resumed=subprocess.Popen(command+['--instructions',str(instructions),'--run-id',original['run_id']],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
        self.managers.append(resumed)
        self.until(lambda:any(e['request_id']=='request' for e in self.view()['waiting']), 'public resume queued')
        self.assertEqual(len(self.argv_log.read_text().splitlines()),1)
        self.release(holders[1][0],holders[1][1])
        output,error=resumed.communicate(timeout=20)
        self.assertEqual(resumed.returncode,0,error)
        continued=json.loads(output)
        self.assertEqual(continued['outcome'],'succeeded',continued)
        self.assertEqual(continued['native_identity']['id'],original['native_identity']['id'])
        launches=[json.loads(line) for line in self.argv_log.read_text().splitlines()]
        self.assertEqual(len(launches),2)
        self.assertIn('--session-id',launches[1]['argv'])

    def test_owner_continues_proven_abandoned_resume_at_original_fifo_position(self):
        self._stream(final='Original Session terminal report.')
        command = [sys.executable, str(REPO/'bin/harness.py'), '--root', str(self.root),
                   'work-dispatch', '--request', 'request', '--config', str(self.config_path), '--execute']
        first = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        initial = json.loads(first.stdout)
        self.assertEqual(initial['outcome'], 'succeeded', initial)
        holders = [self.start() for _ in range(6)]
        self._stream(turn=5, final='Same Session continued after a never-started queue transport exited.')
        instructions = self.home/'original-wire-continuation.md'
        instructions.write_text('Continue this exact original task in the original Session.')
        lost = subprocess.Popen(command + ['--instructions', str(instructions), '--run-id', initial['run_id']],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.managers.append(lost)
        self.until(lambda:any(e['request_id']=='request' for e in self.view()['waiting']), 'queued original resume')
        ticket = next(e for e in self.view()['waiting'] if e['request_id']=='request')
        run_id = ticket['run_id']
        lost.kill(); lost.communicate(timeout=5); self.managers.remove(lost)
        later = self.start(ready=False)
        self.until(lambda:any(e['payload'].get('phase')=='abandoned' for e in tasks.read_run(self.root,run_id)['events']),
                   'normal cleanup recorded the original unadmitted queue exit')
        before = tasks.read_run(self.root,run_id)
        read = [sys.executable, str(REPO/'bin/run_worker.py'), '--root', str(self.root),
                '--request', 'request', '--run-id', run_id, '--read-result']
        observed = subprocess.run(read, capture_output=True, text=True, timeout=15)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        observation = json.loads(observed.stdout)
        self.assertEqual(observation['execution_state'], 'abandoned_before_start', observation)
        self.assertEqual(observation['sequence'], ticket['sequence'])
        self.assertEqual(observation['requested_session_id'], initial['native_identity']['id'])
        self.assertFalse(observation['replay_allowed'])
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)
        command += ['--run-id', run_id, '--continue']
        recovered = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.managers.append(recovered)
        self.until(lambda:any(e['run_id']==run_id and e['phase']=='queued' for e in capacity._queue(self.root)['entries']),
                   'original owner recovered the earlier queue slot')
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1)
        self.release(*holders[0][:2])
        output, error = recovered.communicate(timeout=20)
        self.assertEqual(recovered.returncode, 0, error)
        result = json.loads(output)
        self.assertEqual(result['outcome'], 'succeeded', result)
        self.assertEqual(result['native_identity']['id'], initial['native_identity']['id'])
        retained = tasks.read_run(self.root, run_id)
        self.assertEqual(retained['input_refs'], before['input_refs'])
        self.assertEqual(retained['config_ref'], before['config_ref'])
        self.assertEqual([e['payload']['sequence'] for e in retained['events']
                          if e['kind']=='capacity' and e['payload']['phase']=='admitted'], [ticket['sequence']])
        self.assertEqual(len([e for e in retained['events'] if e['kind']=='intent']), 1)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 2)

    def test_public_queued_dispatch_stop_never_starts_a_native_session(self):
        holders=[self.start() for _ in range(6)]
        self._stream(final='Must not run after original stop.')
        command=[sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),
                 'work-dispatch','--request','request','--config',str(self.config_path),'--execute']
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
        self.managers.append(process)
        self.until(lambda:any(e['request_id']=='request' for e in self.view()['waiting']), 'public stop candidate queued')
        entry=next(e for e in self.view()['waiting'] if e['request_id']=='request')
        tasks.mutate(self.root,'run-stop',self.actor,{'id':entry['run_id'],'summary':'Explicit original stop while queued'})
        process.communicate(timeout=8)
        self.assertFalse(self.argv_log.exists())
        run=tasks.read_run(self.root,entry['run_id'])
        self.assertTrue(run['stop_requested'])
        self.assertIsNone(run['native_identity'])
        receipt=json.loads(Path(run['latest_receipt']['path']).read_bytes())
        self.assertEqual(receipt['observation'],{'capacity_phase':'cancelled_before_start','native_started':False})
        self.assertTrue(any(e['kind']=='capacity' and e['payload']['phase']=='cancelled_before_start' for e in run['events']))

    def test_original_contract_is_rechecked_after_capacity_wait(self):
        import harness_scope
        holders=[self.start() for _ in range(6)]
        self._stream(final='Must not launch against changed original authority/acceptance.')
        process=subprocess.Popen([sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),
            'work-dispatch','--request','request','--config',str(self.config_path),'--execute'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
        self.managers.append(process)
        self.until(lambda:any(e['request_id']=='request' for e in self.view()['waiting']), 'dispatch before contract change')
        entry=next(e for e in self.view()['waiting'] if e['request_id']=='request')
        contract=harness_scope.read_contract(self.root,'request','request')['contract']['body']
        contract['acceptance']['text']+=' New exact acceptance basis while queued.'
        path=self.home/'revised-contract.json';path.write_text(json.dumps(contract))
        harness_scope.save_contract(self.root,self.actor,'request','request',path,self.evidence['path'])
        self.release(holders[0][0],holders[0][1])
        output,error=process.communicate(timeout=15)
        self.assertEqual(json.loads(output)['outcome'],'failed',output+error)
        self.assertIn('original_dispatch_guard_rejected',json.loads(output)['diagnostic'])
        self.assertFalse(self.argv_log.exists())
        self.assertIsNone(tasks.read_run(self.root,entry['run_id'])['native_identity'])

    def test_original_public_queued_transport_reattaches_without_new_intent(self):
        self._assert_original_queued_transport_reattaches()

    def test_original_public_queued_resume_reattaches_without_new_intent_or_session(self):
        self._assert_original_queued_transport_reattaches(resume=True)

    def _assert_original_queued_transport_reattaches(self, *, resume=False):
        import dsh_capacity_handoff as handoff
        command=[sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),
                 'work-dispatch','--request','request','--config',str(self.config_path),'--execute']
        if resume:
            self._stream(final='First original Session report.')
            first=subprocess.run(command,capture_output=True,text=True,env=os.environ.copy(),timeout=20)
            self.assertEqual(first.returncode,0,first.stderr)
            first_run=json.loads(first.stdout)
            instructions=self.home/'original-queued-continuation.md'
            instructions.write_text('Continue the exact original Session and bounded task.')
            command+=['--instructions',str(instructions),'--run-id',first_run['run_id']]
        holders=[self.start() for _ in range(6)]
        self._stream(turn=5 if resume else 4,final='Original queued run completed after explicit policy adoption.')
        process=subprocess.Popen(command,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
        self.managers.append(process)
        self.until(lambda:any(e['request_id']=='request' for e in self.view()['waiting']),'original public waiter')
        with capacity._lock(self.root):
            queue=capacity._queue(self.root);queue.pop('policy')
            entry=next(e for e in queue['entries'] if e['request_id']=='request');entry.pop('policy');entry.pop('admission_policy',None)
            before=dict(entry);original=tasks.read_run(self.root,entry['run_id'])
            execution_lease.atomic_json(capacity._paths(self.root)[0],queue)
        tasks.mutate(self.root,'grant-role',self.actor,{'id':'maintainer','actor':self.actor,
            'org_role':'maintainer','evidence_ref':self.evidence})
        actual=handoff.adopt(self.root,self.evidence['path'])
        process.communicate(timeout=5);self.managers.remove(process)
        self.assertEqual(process.returncode,-signal.SIGTERM)
        self.until(lambda:any(e['run_id']==before['run_id'] and e['phase']=='queued' and e['manager']!=before['manager']
            for e in capacity._queue(self.root)['entries']),'same run claimed by fresh transport')
        self.release(*holders[0][:2])
        self.until(lambda:tasks.read_run(self.root,before['run_id'])['outcome']!='intent','same original native start')
        self.until(lambda:tasks.read_run(self.root,before['run_id'])['outcome']=='succeeded','same original completion')
        after=tasks.read_run(self.root,before['run_id'])
        self.assertEqual(after['input_refs'],original['input_refs'])
        self.assertEqual(after['operation'],'resume' if resume else 'start')
        if resume:self.assertEqual(after['native_identity']['id'],first_run['native_identity']['id'])
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM work_runs WHERE request_id=?',('request',)).fetchone()[0],2 if resume else 1)
        self.assertEqual(len(self.argv_log.read_text().splitlines()),2 if resume else 1)
        self.assertEqual(actual['transports'][0]['sequence'],before['sequence'])
        self.until(lambda:execution_lease.matches(actual['reattached'][0]['manager']) is False,'handoff transport natural exit')


class HandoffTests(unittest.TestCase):
    ref=CapacityTests.ref
    create_request=CapacityTests.create_request
    setUp=CapacityTests.setUp
    seed=CapacityTests.seed
    start=CapacityTests.start
    until=CapacityTests.until
    view=CapacityTests.view
    queued=CapacityTests.queued
    release=CapacityTests.release
    stop_test_processes=CapacityTests.stop_test_processes

    def legacy_waiter(self):
        self.holders=[self.start('review') for _ in range(6)]
        original=self.start('review',ready=False)
        self.until(lambda:self.queued(original[0]),'original legacy waiter')
        with capacity._lock(self.root):
            queue=capacity._queue(self.root);queue.pop('policy')
            entry=next(e for e in queue['entries'] if e['run_id']==original[0]);entry.pop('policy');entry.pop('admission_policy',None)
            execution_lease.atomic_json(capacity._paths(self.root)[0],queue)
        tasks.mutate(self.root,'grant-role',self.actor,{'id':'maintainer','actor':self.actor,
            'org_role':'maintainer','evidence_ref':self.evidence})
        return original,dict(entry)

    def test_fenced_original_run_keeps_sequence_inputs_and_exactly_one_claim(self):
        import dsh_capacity_handoff as handoff
        original,before=self.legacy_waiter();run_id,process,home=original
        row=tasks.read_run(self.root,run_id)
        dry=handoff.adopt(self.root,self.evidence['path'],dry_run=True)
        self.assertEqual([t['run_id'] for t in dry['transports']],[run_id])
        self.assertIsNone(process.poll())
        with patch.object(handoff,'_spawn',return_value={'pid':99999999,'exists':False}):
            actual=handoff.adopt(self.root,self.evidence['path'])
        process.wait(timeout=5);self.managers.remove(process)
        self.assertEqual(process.returncode,-signal.SIGTERM)
        entry=next(e for e in capacity._queue(self.root)['entries'] if e['run_id']==run_id)
        self.assertEqual((entry['phase'],entry['sequence']),('handoff',before['sequence']))
        self.assertEqual(tasks.read_run(self.root,run_id)['input_refs'],row['input_refs'])
        self.assertIsNone(tasks.read_run(self.root,run_id)['native_identity'])
        request=tasks.read_request(self.root,row['request_id'])
        with execution_lease.ExecutionLease(self.root,request,home,run_id) as lease:
            handoff.claim(self.root,row,lease,entry['handoff_token']);lease.intent()
            with self.assertRaisesRegex(ValueError,'already claimed'):
                handoff.claim(self.root,row,lease,entry['handoff_token'])
            self.release(*self.holders[0][:2])
            with capacity.Capacity(self.root,lease,'review',self.actor,handoff=entry['handoff_token']).admit(lambda:None):
                self.assertEqual(self.view()['counts']['occupied']['total'],6)
                current=next(e for e in capacity._queue(self.root)['entries'] if e['run_id']==run_id)
                self.assertEqual(current['sequence'],before['sequence'])
            lease.settled()
        self.assertEqual(tasks.read_run(self.root,run_id)['outcome'],'intent')

    def test_stop_or_child_or_unverified_pid_refuses_before_signal(self):
        import dsh_capacity_handoff as handoff
        original,before=self.legacy_waiter();run_id,process,home=original
        path=execution_lease.metadata_path(home,run_id);meta=json.loads(path.read_bytes())
        for change in ({'child':execution_lease.process_identity(os.getpid())},
                       {'manager':{**meta['manager'],'start':'reused PID'}}):
            execution_lease.atomic_json(path,{**meta,**change})
            with self.assertRaises(ValueError),patch.object(handoff.os,'kill') as signal_call:
                handoff.adopt(self.root,self.evidence['path'])
            signal_call.assert_not_called()
        execution_lease.atomic_json(path,meta)
        tasks.mutate(self.root,'run-stop',self.actor,{'id':run_id,'summary':'Original explicit stop'})
        with self.assertRaises(ValueError),patch.object(handoff.os,'kill') as signal_call:
            handoff.adopt(self.root,self.evidence['path'])
        signal_call.assert_not_called()
        self.assertTrue(tasks.read_run(self.root,run_id)['stop_requested'])

    def test_transport_crash_after_claim_recovers_same_original_fifo(self):
        import dsh_capacity_handoff as handoff
        original,before=self.legacy_waiter();run_id,process,home=original
        with patch.object(handoff,'_spawn',return_value={'pid':99999999,'exists':False}):
            handoff.adopt(self.root,self.evidence['path'])
        process.wait(timeout=5);self.managers.remove(process)
        entry=next(e for e in capacity._queue(self.root)['entries'] if e['run_id']==run_id)
        helper=subprocess.Popen([sys.executable,str(DRIVER),'claim-wait',str(self.root),run_id,
            'review',entry['request_id'],entry['handoff_token']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.managers.append(helper)
        self.until(lambda:(self.root/(run_id+'.claim-ready')).exists(),'claimed but never admitted transport')
        helper.kill();helper.wait(timeout=5);self.managers.remove(helper)
        self.assertTrue(self.queued(run_id),'A fenced original ticket is retained after transport crash')
        with patch.object(handoff,'_spawn',return_value={'pid':99999999,'exists':False}):
            recovered=handoff.adopt(self.root,self.evidence['path'])
        self.assertEqual(recovered['reattached'][0]['run_id'],run_id)
        self.assertEqual(recovered['reattached'][0]['sequence'],before['sequence'])
        self.assertIsNone(tasks.read_run(self.root,run_id)['native_identity'])
        self.assertEqual(tasks.read_run(self.root,run_id)['outcome'],'intent')

    def test_fenced_spawn_failure_is_recoverable_and_original_stop_cancels_barrier(self):
        import dsh_capacity_handoff as handoff
        original,before=self.legacy_waiter();run_id,process,home=original
        with patch.object(handoff,'_spawn',side_effect=OSError('Isolated helper spawn failed')):
            with self.assertRaises(OSError):handoff.adopt(self.root,self.evidence['path'])
        process.wait(timeout=5);self.managers.remove(process)
        self.assertTrue(self.queued(run_id))
        with patch.object(handoff,'_spawn',return_value={'pid':99999999,'exists':False}):
            recovered=handoff.adopt(self.root,self.evidence['path'])
        self.assertEqual(recovered['reattached'][0]['sequence'],before['sequence'])
        tasks.mutate(self.root,'run-stop',self.actor,{'id':run_id,'summary':'Stop original pending handoff'})
        with patch.object(handoff,'_spawn') as spawn:
            handoff.adopt(self.root,self.evidence['path'])
        spawn.assert_not_called()
        self.assertFalse(self.queued(run_id))
        self.assertTrue(tasks.read_run(self.root,run_id)['stop_requested'])
        self.assertIsNone(tasks.read_run(self.root,run_id)['native_identity'])


if __name__ == '__main__':
    unittest.main()
