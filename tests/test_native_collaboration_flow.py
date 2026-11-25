"""Original Session owns the work. Only explicit tool calls start a reviewer.

Native transports are fakes; this exercises the real public dispatch, request,
receipt and review services without invoking models or touching production.
"""
import contextlib
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import agent_runtime as runtime
from task_ledger import sessions
import harness_tasks as tasks
import run_worker
import test_agent_runtime as fixtures
import collaboration_transport_fixture as transport
import harness_scope
import execution_lease
import production_review
from scientific_review.author_tools import ReviewService


class NativeCollaborationTests(unittest.TestCase):
    ref = fixtures.RuntimeTests.ref

    def create_request(self, **extra):
        # Transport cases use an explicit evidence endpoint; scientific candidate
        # qualification and mandatory review remain covered by delivery tests.
        return fixtures.RuntimeTests.create_request(self, finite=True, programmatic=True, requires_review=False, **extra)

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        config = fixtures.configuration()
        config['programmatic'] = True
        self.dsh_home=self.home/'isolated-dsh';self.dsh_home.mkdir()
        (self.dsh_home/'ISOLATED-COLLABORATION-FIXTURE').touch()
        self.review_state=self.home/'review-fixture.json'
        self.review_ready=self.home/'review-started.json'
        self.review_release=self.home/'release-review'
        self.review_argv=self.home/'review-launches.jsonl'
        fixture_cli=str(Path(transport.__file__).resolve())
        config['hosts']['dsh'] = {'adapter': 'dsh-headless', 'entry': 'headless', 'enabled': True,
            'binary': fixture_cli, 'node': fixture_cli, 'dsh_home': str(self.dsh_home)}
        config['providers']['dsh'] = {'selection': 'existing-host-configuration', 'dsh_provider': 'offline'}
        for role in ('executor','scientific_reviewer'):
            config['roles'][role] = {'host':'dsh','provider':'dsh','model':'same-family', 'effort':'high',
                                    'profile':'read-only-evidence','native_policy':{'network_access':False}}
        self.config_path.write_text(json.dumps(config))
        (self.root/'.control/runtime.json').write_text(json.dumps({'programmatic_worker':self.ref(self.config_path)}))
        self.calls=[]
        self.author_text='Candidate and evidence are available. My limits remain in the original report.'
        self.review_text='The denominator is incorrect. Recalculate using the dry material mass.'
        self.ask_review=False
        self.author_status='completed'
        self.fail_after_observation=False
        self.reject_before_start=False
        self.collected=None
        self.review_hold=False
        self.collect_review_id=None
        self.review_status_at_resume=None
        self.requested_review_ids=[]
        self.author_release=None
        self.author_started=threading.Event()
        self.finding_report=None
        self.configure_review()
        environment=patch.dict(os.environ,{'HTAM_COLLAB_FIXTURE_STATE':str(self.review_state)})
        environment.start();self.addCleanup(environment.stop)
        self.addCleanup(self.release_reviews)

    def configure_review(self):
        transport.atomic(self.review_state,{'root':str(self.root),'home':str(self.dsh_home),
            'ready':str(self.review_ready),'release':str(self.review_release),
            'argv_log':str(self.review_argv),'text':self.review_text,'hold':self.review_hold})

    def wait_until(self, predicate, message, seconds=5):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=predicate()
            if value:return value
            time.sleep(0.02)
        self.fail(message)

    def review_run(self):
        task=tasks.read_task(self.root,'task')
        reviews={r['id'] for r in task['requests'] if r['independent']}
        runs=[r for r in task['runs'] if r['request_id'] in reviews]
        return runs[-1] if runs else None

    def wait_review_running(self):
        self.wait_until(self.review_ready.exists,'The detached native review did not start')
        return self.wait_until(lambda:(r if (r:=self.review_run()) and r['outcome']=='running' else None),
            'The independent native identity was not registered')

    def wait_review_return(self):
        self.review_release.touch()
        return self.wait_until(lambda:(r if (r:=self.review_run()) and r['outcome']=='succeeded' and r['return_ids'] else None),
            'The detached reviewer did not retain its original result',seconds=10)

    def release_reviews(self):
        self.review_release.touch()
        if self.review_ready.exists():
            returned = self.wait_review_return()
            parent = tasks.read_request(self.root, 'request')
            launch = production_review.program_home(parent, root=self.root) / 'reviews' / returned['request_id'] / 'launch.json'
            owner = json.loads(launch.read_bytes())['child']
            # Return persistence intentionally precedes transport/capacity
            # cleanup. The isolated directory belongs to that real job until
            # its OS process exits; author close remains nonblocking.
            self.wait_until(lambda: execution_lease.matches(owner) is False,
                            'The detached test review still owns its files', seconds=10)

    def final_adopt_evidence(self, outcome, note):
        body=harness_scope.read_contract(self.root,'request','request')['contract']['body']
        with tasks.transaction(self.root) as c:
            harness_scope.set_contract(c,self.root,'task','task',body,self.evidence,self.actor)
        returned=run_worker.return_result(self.root,self.actor,run_id=outcome['run_id'],scope_ids=['science'])
        return tasks.dispose_result(self.root,self.actor,returned['id'],str(note),disposition='adopt',
                                    acceptance='final',scope_ids=['science'])

    def adapter(self, root, config):
        case=self
        class Adapter:
            def execute(self, text, observe, guard, **kw):
                capacity=config.get('_dsh_capacity')
                with capacity.admit(guard) if capacity else contextlib.nullcontext():
                    return self.execute_admitted(text,observe,guard,**kw)

            def execute_admitted(self,text,observe,guard,**kw):
                case.calls.append((config['role'], text, kw))
                if case.reject_before_start:
                    raise runtime.NativeStartRejected('original_session_directory_differs')
                with kw.get('boundary', contextlib.nullcontext)(): guard()
                native=runtime.identity('deepseek_harness','session',
                    (kw.get('previous') or {}).get('id', 'author' if config['role']=='executor' else 'reviewer'),
                    root,str(int((kw.get('previous') or {}).get('turn_id','0'))+1))
                route={'provider':config['provider']['dsh_provider'],'model':config['model'],'reasoningEffort':config['effort']}
                transport.record_session(case.dsh_home,native,None,None,route)
                observe(native,'running',{})
                case.author_started.set()
                if case.author_release and not case.author_release.wait(5):raise TimeoutError('Test did not release its author')
                if config['role']=='executor' and case.finding_report:
                    first=self.review_service.report_finding({'report':str(case.finding_report)})
                    case.assertEqual(first,self.review_service.report_finding({'report':str(case.finding_report)}))
                    case.reported_finding=first
                if case.fail_after_observation:
                    # Provide real exit evidence for this failed fake transport.
                    # Keeping the Python test manager alive is not proof that an
                    # absent mocked native process exited, nor permission to settle it.
                    self.process=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.buffer.read(1)'],
                        stdin=subprocess.PIPE,start_new_session=True)
                    config['_execution_lease'].spawned(self.process)
                    self.process.communicate(b'x',timeout=3)
                    case.assertEqual(self.process.returncode,0)
                    raise TimeoutError('Original native result unknown')
                if config['role']=='executor' and case.ask_review:
                    candidate=root/'work/original/candidate.txt'; candidate.write_text('Current candidate: 8 kg')
                    requested=self.review_service.request_review({'artifacts':[str(candidate)],'question':'Check dry mass and denominator.'})
                    repeated=self.review_service.request_review({'artifacts':[str(candidate)],'question':'Check dry mass and denominator.'})
                    case.assertEqual(repeated['review_id'],requested['review_id'])
                    case.requested_review_ids.append(requested['review_id'])
                    if not case.review_hold:
                        case.collected=self.review_service.result({'review_id':requested['review_id'],'wait_seconds':10})
                    candidate.write_text('Author revised candidate: 5 kg dry mass')
                if config['role']=='executor' and case.collect_review_id:
                    case.review_status_at_resume=tasks.read_run(case.root,case.review_run()['id'])['outcome']
                    case.review_release.touch()
                    case.collected=self.review_service.result({'review_id':case.collect_review_id,'wait_seconds':10})
                terminal=case.author_status if config['role']=='executor' else 'completed'
                text=case.author_text if config['role']=='executor' else case.review_text
                transport.record_session(case.dsh_home,native,
                    {'failed':'error','interrupted':'aborted'}.get(terminal,terminal),text,route)
                return {'native_identity':native,'native_status':case.author_status if config['role']=='executor' else 'completed',
                    'stop_confirmed':case.author_status=='interrupted',
                    'result':case.author_text if config['role']=='executor' else case.review_text}
            def readresult(self,native,**kw):
                observed=transport.read_session(case.dsh_home,native)
                terminal=observed.get('terminal')
                if str(observed.get('latest_turn')) != native.get('turn_id'):
                    # This fixture stores only its latest turn. A final from
                    # turn 1 cannot be claimed as a final for the initial turn
                    # 0 observed before turn_start reached the manager.
                    return {'native_identity':native,'native_status':'unknown',
                            'result':None,'native_session_observation':observed,'stop_confirmed':False}
                return {'native_identity':native,'native_status':{'error':'failed','aborted':'interrupted'}.get(terminal,terminal or 'unknown'),
                    'result':observed.get('final_text'),'native_session_observation':observed,'stop_confirmed':terminal=='aborted'}
            def close(self): pass
        return Adapter()

    def dispatch(self, **kw):
        cls=runtime.Runtime
        with patch.object(runtime,'Runtime',lambda root:cls(root,adapter_factory=self.adapter)):
            return sessions.dispatch(self.root,self.actor,'request',execute=True,**kw)

    def task_decision(self, action, note):
        script = Path(__file__).resolve().parents[1] / 'bin/harness.py'
        result = subprocess.run([sys.executable, str(script), '--root', str(self.root),
            'work-' + action, '--task', 'task', '--note', str(note)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_successor_chat_resumes_original_session_with_current_config_and_new_instruction(self):
        import os
        first = self.dispatch()
        original = tasks.read_run(self.root, first['run_id'])
        request = tasks.read_request(self.root, 'request')
        Path(request['input_ref']['path']).unlink()
        current = json.loads(self.config_path.read_bytes())
        current['roles']['executor']['model'] = 'current-configured-model'
        current_path = self.home / 'current-runtime.json'
        current_path.write_text(json.dumps(current))
        self.config_path.unlink()
        (self.root / '.control/runtime.json').write_text(json.dumps({'programmatic_worker': {'path': str(current_path)}}))
        note = self.home / 'continue-original.md'
        note.write_text('Continue the same Session using the retained result and the current source originals.')
        previous_actor = self.actor
        self.actor = {**self.actor, 'id': 'successor-chat', 'instance': 'successor-chat'}
        snapshot = tasks.read_task(self.root, request['task_id'])['task']
        from task_ledger.sessions import reference
        tasks.mutate(self.root,'transfer',previous_actor,{'id':'lead-transfer','task_id':request['task_id'],
            'expected_task_revision':snapshot['revision'],'evidence_ref':reference(self.root,note),
            'summary':'Original lead transfers the same task and writer.','new_owner_actor':self.actor})
        try:
            with patch.dict(os.environ, CODEX_THREAD_ID='successor-chat'):
                resumed = self.dispatch(instructions=note, run_id=first['run_id'])
        finally:
            self.actor = previous_actor
        after = tasks.read_run(self.root, resumed['run_id'])
        self.assertEqual(resumed['outcome'], 'succeeded')
        self.assertEqual(after['native_identity']['id'], original['native_identity']['id'])
        self.assertEqual(tasks.read_run(self.root, original['id']), original)
        self.assertNotEqual(after['config_ref']['sha256'], original['config_ref']['sha256'])
        self.assertEqual(after['dispatcher_actor']['id'], 'successor-chat')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][2]['previous']['id'], original['native_identity']['id'])

    def test_plain_final_is_saved_byte_for_byte_and_repeat_does_not_call_model(self):
        self.author_text='## Actual report\nNo management JSON.\n```json\n{"review_ready":true}\n```\n'
        first=self.dispatch()
        self.assertEqual(first['outcome'],'succeeded')
        returned=tasks.read_return(self.root,first['management_return']['return_id'])
        self.assertEqual(Path(returned['result_ref']['path']).read_text(),self.author_text)
        self.assertEqual(returned['coverage'],[])
        from harness_package import snapshot
        package = snapshot(self.root, 'task')['packages'][0]
        plain = package['returns'][0]
        self.assertEqual(plain['contribution'], self.author_text)
        self.assertFalse(plain['structuredSummaryAvailable'])
        self.assertEqual(package['scope']['remaining_scope'],['science'])
        self.assertEqual(snapshot(self.root,'task')['task_scopes'][0]['remaining_count'],1)
        self.assertEqual(package['evidenceIssues'], [])
        self.assertEqual(self.dispatch()['management_return'],first['management_return'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(tasks.read_task(self.root,'task')['runs']),1)

    def test_native_author_reports_plain_file_with_real_identity_and_no_relaunch(self):
        from task_ledger import maintenance as harness_maintenance
        self.finding_report=self.home/'observed-tool-error.md'
        self.finding_report.write_text('The public tool failed on this original input; independent work continues.')
        outcome=self.dispatch()
        self.assertEqual(outcome['outcome'],'succeeded')
        finding=tasks.read_request(self.root,self.reported_finding['finding_id'])
        self.assertEqual(finding['sender_actor']['host'],'deepseek_harness')
        self.assertEqual(finding['sender_actor']['id'],'author')
        self.assertEqual(finding['task_id'],'task')
        self.assertEqual(finding['input_ref'],self.ref(self.finding_report))
        self.assertEqual(len(self.calls),1)
        with self.assertRaisesRegex(ValueError,'live author'):
            harness_maintenance.report(self.root,self.actor,'task',self.finding_report,native_run_id=outcome['run_id'])

    def test_concurrent_observers_and_late_finisher_keep_one_original_result(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.dispatch()
        source = json.loads(Path(first['receipt']['path']).read_text())['result']
        config = runtime.load_config(self.root, str(self.config_path), 'executor')
        worker = runtime.Runtime(self.root, adapter_factory=self.adapter)
        with ThreadPoolExecutor(max_workers=2) as pool:
            observed = list(pool.map(lambda _: worker._record_result(first['run_id'], config, source, self.home), range(2)))
        self.assertEqual({item['management_return']['return_id'] for item in observed},
                         {first['management_return']['return_id']})
        self.assertTrue(all(item['management_return']['status'] == 'registered' for item in observed))
        self.assertNotEqual(observed[0]['receipt'], observed[1]['receipt'])
        self.assertEqual(len(self.calls), 1)
        changed = worker._record_result(first['run_id'], config, {**source, 'result': 'Different final bytes'}, self.home)
        self.assertEqual(changed['management_return']['status'], 'index_pending')
        self.assertIn('different author, run or saved result', changed['management_return']['diagnostic'])
        original = tasks.read_return(self.root, first['management_return']['return_id'])
        self.assertEqual(Path(original['result_ref']['path']).read_text(), self.author_text)

    def test_author_requests_same_family_independent_review_inside_original_turn(self):
        self.ask_review=True
        outcome=self.dispatch()
        self.assertEqual(outcome['outcome'],'succeeded')
        self.assertEqual([call[0] for call in self.calls],['executor'])
        self.assertEqual(self.collected['state'],'received',self.collected)
        self.assertEqual(self.collected['opinion']['text'],self.review_text)
        self.assertEqual((self.home/'candidate.txt').read_text(),'Author revised candidate: 5 kg dry mass')
        reviewed=self.collected['opinion']['submission']['artifact_refs'][0]
        self.assertEqual(Path(reviewed['path']).read_text(),'Current candidate: 8 kg')
        review_run=self.wait_review_return()
        self.assertNotEqual(review_run['native_identity']['id'],'author')
        launches=[json.loads(line) for line in self.review_argv.read_text().splitlines()]
        self.assertEqual(len(launches),1,'Repeated exact review requests must not start two processes')
        self.assertEqual(launches[0]['route']['model'],'same-family')
        self.assertNotIn('write',launches[0]['tools'])
        self.assertTrue(self.calls[0][1].startswith('本次职责：'))
        self.assertIn('原Session和原生Goal',self.calls[0][1].splitlines()[0])
        self.assertIn('独立只读',launches[0]['input'].splitlines()[0])
        self.assertIn(str(self.root/'roles/reviewer.md'),launches[0]['input'])
        packet_path=Path(tasks.read_request(self.root,review_run['request_id'])['input_ref']['path'])
        self.assertEqual(packet_path.parent.name,review_run['request_id'])
        self.assertEqual(packet_path.parent.parent.name,'reviews')
        self.assertTrue(Path(reviewed['path']).is_relative_to(packet_path.parent/'fixed'))
        snapshots=[ref for ref in tasks.read_run(self.root,outcome['run_id'])['input_refs']
                   if Path(ref['path']).name.startswith('runtime-config-')]
        self.assertEqual(len(snapshots),1)
        self.assertEqual(Path(snapshots[0]['path']).parent.name,outcome['run_id'])
        self.assertEqual(self.dispatch()['management_return'],outcome['management_return'])
        self.assertEqual(len(self.calls),1)

    def test_review_archive_reuses_original_packet_identity_and_freezes_new_questions(self):
        import production_review
        author=self.dispatch();run=tasks.read_run(self.root,author['run_id'])
        candidate=self.home/'archive-candidate.json';candidate.write_text('{"amount":8}')
        raw={'id':str(candidate.relative_to(self.root)),**self.ref(candidate)}
        frozen=production_review._fix_artifact(self.root,self.home,raw)
        original_request=tasks.read_request(self.root,'request')
        packet={'author_run_id':run['id'],'authors':[run['native_identity']],
            'review_scope':'The original exact scientific question','scope_keys':['science'],
            'artifact_refs':[frozen],'evidence_refs':[original_request['input_ref'],
                {'path':frozen['path'],'sha256':frozen['sha256']}]}
        legacy=self.home/'legacy-review-packet.json';legacy.write_text(json.dumps(packet))
        tasks.mutate(self.root,'request',self.actor,{'id':'historical-review','task_id':'task',
            'kind':'review','recipient_role':'executor','input_ref':self.ref(legacy),
            'summary':packet['review_scope'],'plan_revision':1,'required_scope':['science'],
            'independent':True,'parent_request_id':'request'})
        current={**packet,'artifact_refs':[raw],
            'evidence_refs':[original_request['input_ref'],{'path':raw['path'],'sha256':raw['sha256']}]}
        worker=runtime.Runtime(self.root)
        reused=run_worker._program_review_request(self.root,worker,original_request,current)
        self.assertEqual(reused['id'],'historical-review')
        self.assertEqual(reused['input_ref'],self.ref(legacy))
        new=run_worker._program_review_request(self.root,worker,original_request,
            {**current,'review_scope':'A different exact scientific question'})
        self.assertNotEqual(new['id'],reused['id'])
        self.assertEqual(Path(new['input_ref']['path']).parts[-3:],('reviews',new['id'],'packet.json'))
        submitted=json.loads(Path(new['input_ref']['path']).read_bytes())
        fixed=submitted['artifact_refs'][0]
        self.assertTrue(Path(fixed['path']).is_relative_to(Path(new['input_ref']['path']).parent/'fixed'))
        candidate.write_text('{"amount":5}')
        self.assertEqual(Path(fixed['path']).read_text(),'{"amount":8}')
        self.assertEqual(Path(frozen['path']).read_text(),'{"amount":8}')
        self.assertEqual(self.ref(legacy),reused['input_ref'])

    def test_author_terminal_is_visible_while_its_uncollected_review_still_runs(self):
        self.ask_review=True;self.review_hold=True;self.configure_review()
        outcome=self.dispatch()
        self.assertEqual(outcome['outcome'],'succeeded')
        reviewer=self.wait_review_running()
        author=tasks.read_run(self.root,outcome['run_id'])
        self.assertEqual(author['outcome'],'succeeded');self.assertTrue(author['return_ids'])
        self.assertFalse(reviewer['stop_requested'])
        ready=json.loads(self.review_ready.read_bytes())
        os.kill(ready['pid'],0)  # The actual isolated native transport survives author return.
        receipt=runtime.read_receipt(self.root,outcome['receipt'])
        self.assertEqual(receipt['result']['collected_review_return_ids'],[])
        self.assertEqual(receipt['result']['requested_review_ids'],self.requested_review_ids)
        final=self.wait_review_return()
        self.assertEqual(final['id'],reviewer['id'])
        self.assertEqual(len(self.calls),1)
        from harness_snapshot import read as read_snapshot,bundle
        ready_view=read_snapshot(self.root,request_id='request')['work']['reviews']['request']
        self.assertEqual(ready_view[0]['state'],'ready');self.assertEqual(ready_view[0]['deliveries'],[])
        card=bundle(self.root,author['native_identity'],request_id='request')
        self.assertEqual(card['tasks'][0]['important']['review_ready'],1)
        self.assertEqual(card['tasks'][0]['important']['review_delivered'],0)

    def test_terminal_author_resumes_original_session_and_collects_detached_review(self):
        self.ask_review=True;self.review_hold=True;self.configure_review()
        first=self.dispatch();reviewer=self.wait_review_running()
        author=tasks.read_run(self.root,first['run_id'])
        self.ask_review=False;self.collect_review_id=self.requested_review_ids[0]
        note=self.home/'continue-complete-package.md'
        note.write_text('Continue the original full package and collect the outstanding review.')
        resumed=self.dispatch(instructions=note,run_id=author['id'])
        self.assertEqual(resumed['outcome'],'succeeded')
        self.assertEqual(self.review_status_at_resume,'running')
        current=tasks.read_run(self.root,resumed['run_id'])
        self.assertEqual(current['native_identity']['id'],author['native_identity']['id'])
        self.assertNotEqual(current['native_identity']['turn_id'],author['native_identity']['turn_id'])
        self.assertEqual(self.collected['review_id'],self.collect_review_id)
        self.assertEqual(self.collected['opinion']['text'],self.review_text)
        self.assertEqual(self.wait_review_return()['id'],reviewer['id'])
        self.assertEqual(len(self.review_argv.read_text().splitlines()),1)
        self.assertEqual([call[0] for call in self.calls],['executor','executor'])
        from harness_snapshot import read as read_snapshot
        reviews=read_snapshot(self.root,request_id='request')['work']['reviews']['request']
        self.assertEqual(len(reviews[0]['deliveries']),1)
        delivered=reviews[0]['deliveries'][0]
        self.assertEqual(delivered['receiver']['id'],author['native_identity']['id'])
        self.assertEqual(delivered['return_id'],self.collected['return_id'])
        self.assertEqual(delivered['opinion_ref'],self.collected['ref'])
        # Repeated full collection in the same Session is idempotent even in a
        # later invocation. Rendering a Hook above did not create this fact.
        self.adapters=[]
        service=ReviewService(runtime.Runtime(self.root),tasks.read_request(self.root,'request'),{},current['id'],self.home,lambda:None)
        service.record_delivery(self.collect_review_id,self.collected)
        self.assertEqual(len(read_snapshot(self.root,request_id='request')['work']['reviews']['request'][0]['deliveries']),1)

    def test_new_host_session_can_receive_exact_original_opinion_without_borrowing_identity(self):
        self.ask_review=True;first=self.dispatch();self.wait_review_return()
        original_review=self.collected
        self.create_request(id='new-host-responsibility',input_ref=self.evidence)
        self.reject_before_start=False
        # Register a genuinely distinct observed host Session via the native
        # transport fixture; the old Session remains an independent fact.
        cross=json.loads(self.config_path.read_bytes())
        cross['roles']['executor']={'host':'codex','provider':'native','model':'offline-new-host','profile':'read-only-evidence'}
        cross_path=self.home/'cross-host-runtime.json';cross_path.write_text(json.dumps(cross))
        new=runtime.Runtime(self.root,adapter_factory=lambda root,config:fixtures.CompletedAdapter(root,config,
            author=runtime.identity('codex','thread','new-cross-host-session',root,'1')))
        outcome=new.execute(config_path=str(cross_path),role='executor',task_id='task',request_id='new-host-responsibility',
            run_id='cross-host-receiver',plan_revision=1,input_path=self.evidence['path'],input_sha256=self.evidence['sha256'],
            evidence_home=str(self.home/'cross-host'),program_context={})
        self.assertEqual(outcome['outcome'],'succeeded')
        service=ReviewService(new,tasks.read_request(self.root,'new-host-responsibility'),{},
            outcome['run_id'],self.home,lambda:None)
        service.record_delivery(original_review['review_id'],original_review)
        rows=service.availability();self.assertEqual(rows[0]['review_id'],original_review['review_id'])
        self.assertEqual({d['receiver']['id'] for d in rows[0]['deliveries']},{'author','new-cross-host-session'})

    def test_index_failure_keeps_report_and_does_not_restart_scientific_work(self):
        original=tasks.mutate
        def broken(root,action,actor,payload):
            if action=='return': raise ValueError('Synthetic index unavailable')
            return original(root,action,actor,payload)
        with patch.object(tasks,'mutate',side_effect=broken):
            result=self.dispatch()
        self.assertEqual(result['outcome'],'succeeded')
        self.assertEqual(result['management_return']['status'],'index_pending')
        self.assertEqual(Path(result['management_return']['result_ref']['path']).read_text(),self.author_text)
        repaired=self.dispatch()
        self.assertEqual(repaired['management_return']['status'],'registered')
        self.assertEqual(len(self.calls),1)

    def test_concurrent_dispatches_keep_one_author_through_atomic_run_intent(self):
        for resume in (False,True):
            with self.subTest(resume=resume):
                before=run_worker._selected_run(self.root,'request')
                note=self.home/'concurrent-continuation.md';note.write_text('Continue this original Session once.')
                barrier=threading.Barrier(2)
                calls_before=len(self.calls)
                runs_before=len(tasks.read_task(self.root,'task')['runs'])
                outcomes=[];errors=[]
                self.author_started.clear();self.author_release=threading.Event()
                def dispatch():
                    try:
                        barrier.wait(3)  # Both callers enter before the production request lease.
                        outcomes.append(run_worker.programmatic(self.root,'request',
                            instructions_path=note if resume else None,
                            exact_run_id=before['id'] if resume else None,
                            runtime_factory=lambda root:runtime.Runtime(root,adapter_factory=self.adapter)))
                    except Exception as exc:errors.append(exc)
                threads=[threading.Thread(target=dispatch) for _ in range(2)]
                try:
                    for thread in threads:thread.start()
                    self.assertTrue(self.author_started.wait(3))
                    self.wait_until(lambda:bool(outcomes or errors),'The second caller did not observe/reject the active writer')
                finally:
                    self.author_release.set()
                    for thread in threads:thread.join(5)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(len(self.calls)-calls_before,1)
                self.assertEqual(len(tasks.read_task(self.root,'task')['runs'])-runs_before,1)
                self.assertEqual(len(outcomes)+len(errors),2)
                self.assertTrue(any(item.get('outcome')=='succeeded' for item in outcomes))
                for item in outcomes:self.assertIn(item.get('outcome'),('running','succeeded'))
                for error in errors:self.assertRegex(str(error),'owns this request/Session|already exists|in-flight|immutable|execution active')
                self.author_release=None

    def test_sqlite_index_failure_preserves_completed_report_and_retries_without_model(self):
        with tasks.transaction(self.root) as c:
            c.execute("CREATE TRIGGER fail_report_index BEFORE INSERT ON work_returns "
                      "BEGIN SELECT RAISE(FAIL, 'Synthetic index failure'); END")
        try:
            result = self.dispatch()
        finally:
            with tasks.transaction(self.root) as c:
                c.execute('DROP TRIGGER fail_report_index')
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['management_return']['status'], 'index_pending')
        self.assertEqual(Path(result['management_return']['result_ref']['path']).read_text(), self.author_text)
        self.assertEqual(tasks.read_run(self.root, result['run_id'])['outcome'], 'succeeded')
        self.assertEqual(self.dispatch()['management_return']['status'], 'registered')
        self.assertEqual(len(self.calls), 1)

    def test_explicit_resume_of_completed_author_retains_old_stop_and_session(self):
        first = self.dispatch()
        tasks.mutate(self.root, 'run-stop', self.actor, {'id': first['run_id'],
            'summary': 'Retained stop request from the original invocation.'})
        original = tasks.read_run(self.root, first['run_id'])
        note = self.home / 'continue-after-completion.md'
        note.write_text('Resume this original author Session after its confirmed completed turn.')
        with self.assertRaisesRegex(ValueError, 'stop is still in force'):
            self.dispatch(instructions=note, run_id=first['run_id'])
        self.assertEqual(len(self.calls), 1)
        result = self.dispatch(instructions=note, run_id=first['run_id'], resume_stopped=True)
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(self.calls[-1][2]['previous']['id'], 'author')
        self.assertEqual(tasks.read_run(self.root, first['run_id']), original)
        self.assertEqual(len(self.calls), 2)

    def test_unknown_attempt_is_read_without_launching_another_author(self):
        self.fail_after_observation=True
        with self.assertRaises(TimeoutError): self.dispatch()
        original=tasks.read_task(self.root,'task')['runs'][0]
        retained=Path(original['latest_receipt']['path']).read_bytes()
        result=self.dispatch()
        self.assertEqual(result['outcome'],'unknown')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(tasks.read_task(self.root,'task')['runs']),1)
        self.assertEqual(Path(original['latest_receipt']['path']).read_bytes(),retained)

    def test_owner_continues_same_session_without_adoption_or_recovery_form(self):
        first=self.dispatch()
        note=self.home/'continue.md'; note.write_text('Continue the remaining source comparison in the same package.')
        second=self.dispatch(instructions=note)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1][2]['previous']['id'],'author')
        self.assertIn('本次接续说明（负责人原文）：'+str(note),self.calls[1][1])
        self.assertIn('原请求文件：'+str(self.input_path),self.calls[1][1])
        self.assertIn('上一运行回执（事实记录）：',self.calls[1][1])
        self.assertNotIn(note.read_text(),self.calls[1][1])
        self.assertEqual(self.dispatch(instructions=note,run_id=first['run_id'])['management_return'],second['management_return'])
        self.assertEqual(len(self.calls),2)
        self.assertFalse(tasks.read_task(self.root,'task')['dispositions'])

    def test_unstarted_resume_reuses_the_actual_instruction_not_the_old_brief(self):
        self.dispatch()
        note=self.home/'new-delivery.md'
        note.write_text('Complete the newly assigned source retrieval; the old evidence survey is finished.')
        self.reject_before_start=True
        failed=self.dispatch(instructions=note)
        self.assertEqual(failed['outcome'],'failed')
        original=tasks.read_run(self.root,failed['run_id'])
        self.reject_before_start=False
        # A plain observation cannot silently replace the unsent instruction.
        self.dispatch()
        self.assertEqual(len(self.calls),2)
        result=self.dispatch(instructions=note)
        self.assertEqual(result['outcome'],'succeeded')
        self.assertEqual(self.calls[-1][2]['previous']['id'],'author')
        self.assertIn('本次接续说明（负责人原文）：'+str(note),self.calls[-1][1])
        self.assertEqual(tasks.read_run(self.root,failed['run_id']),original)
        self.dispatch(instructions=note,run_id=failed['run_id'])
        self.assertEqual(len(self.calls),3)

    def test_lead_reopens_task_and_explicitly_continues_ended_original_author(self):
        first = self.dispatch()
        original = tasks.read_run(self.root, first['run_id'])
        note = self.home / 'finish-original-delivery.md'
        note.write_text('The phase was closed too early. Continue the remaining original delivery.')
        ended = tasks.end_delegation(self.root, self.actor, 'request', str(note))
        pending=self.task_decision('complete',note)
        self.assertEqual(pending['status'],'completion_pending')
        self.final_adopt_evidence(first,note)
        self.assertEqual(self.task_decision('complete',note)['state'],'completed')
        self.task_decision('reopen',note)
        reopened=next(event['payload']['id'] for event in reversed(tasks.read_task(self.root,'task')['events']) if event['kind']=='reopen')
        self.assertEqual(self.dispatch()['outcome'], 'succeeded')
        self.assertEqual(self.dispatch(instructions=note)['state'],'original_delivered')
        with self.assertRaisesRegex(tasks.WorkConflict, 'ended'):
            self.dispatch(instructions=note,basis=reopened)
        self.assertEqual(len(self.calls), 1)
        resumed = self.dispatch(instructions=note, resume_stopped=True,basis=reopened)
        self.assertEqual(resumed['outcome'], 'succeeded')
        self.assertEqual(self.calls[-1][2]['previous']['id'], 'author')
        self.assertEqual(tasks.read_run(self.root, first['run_id']), original)
        self.assertEqual(len(self.calls), 2)
        self.dispatch(instructions=note, resume_stopped=True,run_id=first['run_id'],basis=reopened)
        self.assertEqual(len(self.calls), 2)
        with tasks._connect(self.root, True) as c:
            events = [json.loads(row[0]) for row in c.execute(
                "SELECT payload_json FROM work_task_events WHERE kind='delegation-decision' ORDER BY id")]
        self.assertEqual([item['action'] for item in events], ['end', 'resume'])
        self.assertEqual(events[0], ended)

    def test_explicit_delegation_resume_keeps_closed_task_boundary(self):
        first=self.dispatch()
        note = self.home / 'closed.md'; note.write_text('End the assignment and task.')
        tasks.end_delegation(self.root, self.actor, 'request', str(note))
        self.assertEqual(self.task_decision('complete',note)['status'],'completion_pending')
        self.final_adopt_evidence(first,note)
        self.assertEqual(self.task_decision('complete',note)['state'],'completed')
        with self.assertRaises(tasks.WorkConflict):
            self.dispatch(instructions=note, resume_stopped=True)
        with tasks._connect(self.root, True) as c:
            self.assertEqual(tasks._delegation_decisions(c, 'task')['request']['action'], 'end')
        self.assertEqual(len(self.calls), 1)

    def test_stopped_author_keeps_intent_until_explicit_exact_resume(self):
        self.author_status='interrupted'
        first=self.dispatch()
        self.assertEqual(first['outcome'],'stopped')
        note=self.home/'resume.md'; note.write_text('I explicitly resume the stopped original work.')
        with self.assertRaisesRegex(ValueError,'stop is still in force'):
            self.dispatch(instructions=note)
        self.assertEqual(len(self.calls),1)
        self.author_status='completed'
        result=self.dispatch(instructions=note,resume_stopped=True,run_id=first['run_id'])
        self.assertEqual(result['management_return']['status'],'registered')
        self.assertEqual(self.calls[-1][2]['previous']['id'],'author')
        self.assertEqual(tasks.read_run(self.root,first['run_id'])['outcome'],'stopped')

    def test_explicit_owner_instruction_continues_observed_session_after_unknown(self):
        self.fail_after_observation=True
        with self.assertRaises(TimeoutError): self.dispatch()
        original = tasks.read_task(self.root, 'task')['runs'][0]
        self.fail_after_observation=False
        note=self.home/'continue.md'; note.write_text('Continue the original package.')
        self.assertEqual(self.dispatch(instructions=note)['outcome'], 'succeeded')
        self.assertEqual(self.calls[-1][2]['previous']['id'], 'author')
        self.assertEqual(tasks.read_run(self.root, original['id'])['outcome'], 'unknown')
        self.assertEqual(len(self.calls),2)

    def test_owner_adoption_without_scope_matrix_never_ends_author(self):
        result=self.dispatch()
        decision=self.home/'adopt.json'
        decision.write_text(json.dumps({'decision':'adopt','reason':'Use this source-limited evidence; continue the remaining analysis.'}))
        adopted=tasks.dispose_result(self.root,self.actor,result['management_return']['return_id'],str(decision),disposition='adopt')
        self.assertEqual(adopted['disposition'],'adopt')
        note=self.home/'continue.md'; note.write_text('Proceed with the remaining original work.')
        self.dispatch(instructions=note)
        self.assertEqual(len(self.calls),2)
        with tasks._connect(self.root,True) as c:
            self.assertEqual(tasks._delegation_decisions(c,'task'),{})

    def test_report_readback_survives_missing_historical_brief(self):
        first=self.dispatch()
        self.input_path.unlink()
        result=self.dispatch()
        self.assertEqual(result['management_return'],first['management_return'])
        self.assertEqual(len(self.calls),1)

    def test_plain_brief_registers_once_without_management_fields(self):
        brief=self.home/'full-package.md'
        raw='# Full source comparison\nComplete the useful local candidate and scientific review.\n'
        brief.write_text(raw)
        first=sessions.request(self.root,self.actor,'task',str(brief))
        second=sessions.request(self.root,self.actor,'task',str(brief))
        self.assertEqual(first,second)
        self.assertEqual(first['input_ref'],self.ref(brief))
        self.assertEqual(first['required_scope'],[])
        self.assertEqual(brief.read_text(),raw)
        self.assertEqual(len(self.calls),0)

    def test_public_plain_review_request_works_after_author_has_finished(self):
        author=self.dispatch()
        before=tasks.read_run(self.root,author['run_id'])
        brief=self.home/'independent-review.md'
        brief.write_text('Review the saved candidate and source calculations. Explain scientific corrections directly to the author.')
        contract=self.home/'review-contract.json'
        contract.write_text(json.dumps(harness_scope.read_contract(self.root,'request','request')['contract']['body']))
        script=Path(__file__).resolve().parents[1]/'bin/harness.py'
        created=subprocess.run([sys.executable,str(script),'--root',str(self.root),'work-request',
            '--task','task','--brief',str(brief),'--review-of','request','--contract',str(contract)],capture_output=True,text=True)
        self.assertEqual(created.returncode,0,created.stderr)
        request=json.loads(created.stdout)
        self.assertEqual(Path(request['input_ref']['path']).read_text(),brief.read_text())
        result=run_worker.programmatic(self.root,request['id'],runtime_factory=lambda root:runtime.Runtime(root,adapter_factory=self.adapter))
        self.assertEqual(result['outcome'],'succeeded')
        self.assertEqual([call[0] for call in self.calls],['executor','scientific_reviewer'])
        self.assertEqual(tasks.read_run(self.root,author['run_id']),before)
        reviewer=tasks.read_run(self.root,result['run_id'])
        self.assertNotEqual(reviewer['native_identity']['id'],before['native_identity']['id'])
        opinion=run_worker._program_opinion(self.root,None,None,reviewer)
        self.assertEqual(opinion['opinion']['text'],self.review_text)
        self.assertEqual(opinion['submission_ref'],request['input_ref'])

    def test_author_failure_does_not_cancel_independent_review(self):
        self.ask_review=True;self.author_status='failed';self.review_hold=True;self.configure_review()
        outcome=self.dispatch()
        self.assertEqual(outcome['outcome'],'failed')
        reviewer=self.wait_review_running()
        self.assertFalse(reviewer['stop_requested'])
        final=self.wait_review_return()
        self.assertEqual(final['id'],reviewer['id'])
        report=tasks.read_return(self.root,final['return_ids'][0])
        self.assertEqual(Path(report['result_ref']['path']).read_text(),self.review_text)
        self.assertEqual(tasks.read_run(self.root,outcome['run_id'])['outcome'],'failed')

    def test_original_brief_reuses_recorded_id_without_restarting_author(self):
        original = tasks.read_request(self.root, 'request')
        result = sessions.request(self.root, self.actor, 'task', original['input_ref']['path'])
        self.assertEqual(result['id'], 'request')
        self.assertEqual(tasks.read_task(self.root, 'task')['runs'], [])
        self.assertEqual(self.calls, [])

    def test_public_cli_returns_plain_reports_then_completes_after_exact_final_adoption(self):
        script=Path(__file__).resolve().parents[1]/'bin/harness.py'
        def cli(*args):
            result=subprocess.run([sys.executable,str(script),'--root',str(self.root),*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        brief=self.home/'new-task.md';brief.write_text('Produce a reusable source-limited candidate and review it.')
        task=cli('work-create','--brief',str(brief))
        self.assertEqual(task['required_scope'],[])
        assignment=cli('work-request','--task',task['id'],'--brief',str(brief))
        self.assertEqual(assignment['input_ref'],self.ref(brief))
        report=self.home/'ordinary-report.md';report.write_text('The source supports this limited use. See the original candidate.')
        result=cli('work-return','--request',assignment['id'],'--result',str(report))
        self.assertEqual(Path(result['result_ref']['path']).read_text(),report.read_text())
        adopted=cli('work-dispose','--return-id',result['id'],'--use','adopt','--decision',str(report))
        self.assertEqual(adopted['disposition'],'adopt')
        note=self.home/'updated-plan.md';note.write_text('Clarify the intended comparison; continue independent research.')
        revision=('work-revise','--task',task['id'],'--base-sha256',task['plan_ref']['sha256'],
                  '--note',str(note),'--reason-file',str(report))
        changed=cli(*revision)
        self.assertEqual(changed['state'],'active')
        self.assertEqual(cli(*revision),changed)
        finished=cli('work-complete','--task',task['id'],'--note',str(report))
        self.assertEqual(finished['status'],'completion_pending')
        self.assertEqual(tasks.read_task(self.root,task['id'])['task']['state'],'active')
        scope=self.home/'evidence-scope.json'
        scope.write_text(json.dumps({'items':[{'scope_id':'E1','kind':'deliverable',
            'label':'The original source comparison evidence','source_refs':[self.ref(brief)],
            'target_endpoint':'evidence'}],'source_ref':self.ref(brief),
            'links':[{'request_id':assignment['id'],'scope_ids':['E1']}],'verified_scope_ids':['E1']}))
        cli('work-scope','--task',task['id'],'--apply',str(scope))
        contract=self.home/'evidence-contract.json'
        body=harness_scope.read_contract(self.root,'request','request')['contract']['body']
        contract.write_text(json.dumps(body))
        cli('work-contract','--task',task['id'],'--set',str(contract),'--source',str(brief))
        cli('work-contract','--request',assignment['id'],'--set',str(contract),'--source',str(brief))
        covered=cli('work-return','--request',assignment['id'],'--result',str(report),'--scope','E1')
        accepted=cli('work-dispose','--return-id',covered['id'],'--use','adopt','--decision',str(report),
            '--scope','E1','--acceptance','final')
        self.assertEqual(accepted['disposition'],'adopt')
        self.assertEqual(cli('work-complete','--task',task['id'],'--note',str(report))['state'],'completed')
        self.assertEqual(len(self.calls),0)

    def test_plan_note_does_not_stop_the_author_or_require_scope_requalification(self):
        first=self.dispatch()
        task=tasks._read_one(self.root,'work_tasks','task')
        note=self.home/'new-plan.md';note.write_text('Clarify the report audience; continue the original scope.')
        tasks.mutate(self.root,'revise',self.actor,{'id':'change','task_id':'task',
            'expected_task_revision':task['revision'],'plan_ref':self.ref(note),
            'evidence_ref':self.ref(note),'summary':note.read_text()})
        follow=self.home/'follow.md';follow.write_text('Finish the remaining comparison.')
        resumed=self.dispatch(instructions=follow)
        self.assertEqual(resumed['outcome'],'succeeded')
        self.assertFalse(tasks.read_run(self.root,first['run_id'])['stop_requested'])
        self.assertEqual(len(self.calls),2)
