"""Durable review observation and bounded waits, independent of author lifetime."""
from types import SimpleNamespace
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from scientific_review.author_tools import ReviewService
from agent_runtime import StopRequested

class ReviewWaitingTests(unittest.TestCase):
    def setUp(self):
        self.service = ReviewService(SimpleNamespace(root='/fixture'), {'id':'author','task_id':'task'}, {},
                                     'author-run', '/fixture', lambda:None)
        self.service.cancelled = Mock(wraps=threading.Event())
        self.service.availability=Mock(return_value=[])
        self.service.record_delivery=Mock()
        request = patch('harness_tasks.read_request', return_value={
            'id':'review','task_id':'task','parent_request_id':'author','independent':True})
        request.start(); self.addCleanup(request.stop)
        # Unit fixtures have no ledger; queue withdrawal itself is covered
        # against an actual ledger by test_review_revision.
        for name, value in [('harness_tasks._connect', Mock()), ('scientific_review.queue._links', None),
                            ('scientific_review.queue.withheld', None), ('run_worker._direct_return_id', None)]:
            lookup = patch(name, return_value=value)
            lookup.start(); self.addCleanup(lookup.stop)
        reader = patch('run_worker._program_read')
        self.reader = reader.start(); self.addCleanup(reader.stop)
        read_run = patch('harness_tasks.read_run')
        self.read_run = read_run.start(); self.addCleanup(read_run.stop)
    def test_default_is_immediate_and_reconciles_original_native(self):
        run={'id':'run','outcome':'running','updated_at':'original-receipt-time'}
        self.read_run.return_value=run
        activity={'at':'newer-tool-start','source':'original_tool_spawn'}
        with patch('run_worker._selected_run',return_value=run), patch('execution_lease.tool_activity',return_value=activity):
            value=self.service.result({'review_id':'review'})
        self.assertEqual(value['state'],'running')
        self.assertEqual(value['run_updated_at'],'original-receipt-time')
        self.assertEqual(value['last_tool_activity'],activity)
        self.read_run.assert_called_once_with('/fixture','run')
        self.assertEqual(self.reader.call_count,1)
        self.service.cancelled.wait.assert_not_called()
    def test_explicit_wait_collects_reconciled_result(self):
        running={'id':'run','outcome':'running'};done={'id':'run','outcome':'succeeded'}
        opinion={'return_id':'original-return','opinion':{'text':'Fix denominator','submission':{'artifact_refs':[]}}}
        self.read_run.side_effect=[running,done]
        with patch('run_worker._selected_run',side_effect=[running,done]),patch('run_worker._program_opinion',return_value=opinion):
            value=self.service.result({'review_id':'review','wait_seconds':10})
        self.assertEqual(value['return_id'],'original-return')
        self.service.record_delivery.assert_called_once_with('review',opinion)
        self.assertEqual(self.service.cancelled.wait.call_count,1)

    def test_unindexed_original_final_remains_readable_without_false_delivery_record(self):
        done={'id':'run','outcome':'succeeded'}
        self.read_run.return_value=done
        opinion={'return_id':None,'opinion':{'text':'Complete original opinion before indexing.',
            'submission':{'artifact_refs':[]}}}
        with patch('run_worker._selected_run',return_value=done),patch('run_worker._program_opinion',return_value=opinion):
            value=self.service.result({'review_id':'review'})
        self.assertEqual(value['state'],'received')
        self.assertEqual(value['opinion']['text'],opinion['opinion']['text'])
        self.assertEqual(value['delivery_recording']['status'],'index_pending')
        self.service.record_delivery.assert_not_called()

    def test_complete_opinion_is_delivered_only_after_model_seat_reacquisition(self):
        from concurrent.futures import ThreadPoolExecutor
        reacquiring=threading.Event();admitted=threading.Event();order=[]
        @contextlib.contextmanager
        def review_wait(guard,review_id):
            order.append('released');yield
            reacquiring.set()
            if not admitted.wait(5):raise TimeoutError('Isolated test did not admit original author')
            order.append('reacquired')
        self.service.config['_dsh_capacity']=SimpleNamespace(review_wait=review_wait)
        running={'id':'run','outcome':'running'};done={'id':'run','outcome':'succeeded'}
        opinion={'return_id':'original-return','opinion':{'text':'Fix denominator','submission':{'artifact_refs':[]}}}
        self.service.record_delivery.side_effect=lambda *args:order.append('full_delivery')
        self.read_run.side_effect=[running,done]
        with patch('run_worker._selected_run',side_effect=[running,done]),patch('run_worker._program_opinion',return_value=opinion),ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(self.service.result,{'review_id':'review','wait_seconds':10})
            try:
                self.assertTrue(reacquiring.wait(3))
                self.assertFalse(result.done());self.service.record_delivery.assert_not_called()
            finally:admitted.set()
            self.assertEqual(result.result(timeout=3)['state'],'received')
        self.assertEqual(order,['released','reacquired','full_delivery'])
    def test_author_stop_and_wait_cancel_preserve_review(self):
        value=self.service.result({'review_id':'review'},cancelled=lambda:True)
        self.assertEqual(value['state'],'wait_cancelled')
        self.assertFalse(self.service.cancelled.is_set())
        self.service.guard=Mock(side_effect=StopRequested('Explicit stop','author-run'))
        with self.assertRaises(StopRequested):self.service.result({'review_id':'review'})
    def test_wait_bounds_and_failure_are_not_completion(self):
        for invalid in (-1,601,float('inf'),float('nan'),True,'60'):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                self.service.result({'review_id':'review','wait_seconds':invalid})
        receipt = {'path':'original-failure'}
        failed = {'id':'run','outcome':'failed','latest_receipt':receipt, 'target_host':'deepseek_harness'}
        self.read_run.return_value=failed
        self.reader.return_value={'receipt':receipt,'result':{'result':'Partial stream text.', 'native_error':{
            'code':'PI_AI_ERROR', 'message':'Error reading response: malformed server-sent event JSON.'}}}
        with patch('run_worker._selected_run',return_value=failed):
            result=self.service.result({'review_id':'review','wait_seconds':600})
        self.assertEqual(result['state'],'failed')
        self.assertIsNone(result['last_tool_activity'])
        self.assertEqual(result['transport_diagnostic']['kind'],'sse_json_decode')
        self.assertEqual(result['transport_diagnostic']['phase'],'unobserved')
        self.assertNotIn('opinion',result)
        self.assertFalse(result['replay_allowed'])
        with patch('run_worker._selected_run',return_value=None):
            self.assertEqual(self.service.result({'review_id':'review'})['state'],'not_started_or_launch_unresolved')
    def test_wait_reports_exited_launcher_but_preserves_live_and_unobserved_launches(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'reviews' / 'review'
            home.mkdir(parents=True)
            launch = home / 'launch.json'
            request = {'id':'review', 'parent_request_id':'author','task_id':'task','independent': True,
                       'input_ref':{'path':str(home / 'packet.json')}}
            launch.write_text(json.dumps({'review_request':'review','parent_request':'author',
                                         'phase':'spawned','child':{'pid':123}}))
            with (patch('harness_tasks.read_request', return_value=request),
                  patch('run_worker._selected_run',return_value=None),
                  patch('execution_lease.matches') as matches):
                for observation in (True, None):
                    matches.return_value = observation
                    self.assertEqual(self.service.result({'review_id':'review'})['state'],
                                     'not_started_or_launch_unresolved')
                matches.return_value = False
                result = self.service.result({'review_id':'review','wait_seconds':600})
                self.assertEqual(result['state'],'launcher_exited_before_run')
                self.assertEqual(result['launch_path'],str(launch))
                self.assertFalse(result['replay_allowed'])
                self.service.cancelled.wait.assert_not_called()

    def test_close_never_joins_children(self):
        child=Mock();self.service.jobs={'review':{'thread':child}}
        self.service.close();self.service.wait()
        child.join.assert_not_called()

    def test_projection_failure_preserves_collected_opinion_and_author_final(self):
        import sqlite3
        self.service.submissions={'original-review':{'artifact_refs':[]}}
        self.service.collected={'original-review':{'return_id':'original-return'}}
        self.service.availability=Mock(side_effect=sqlite3.OperationalError('locked'))
        value=self.service.finish()
        self.assertEqual(value['collected_review_return_ids'],['original-return'])
        self.assertEqual(value['requested_review_ids'],['original-review'])
        self.assertEqual(value['review_availability_issue']['status'],'unavailable')

    def test_queue_observation_reaches_author_without_claiming_native_start(self):
        run={'id':'run','outcome':'intent'}
        self.read_run.return_value=run
        self.reader.return_value={'run_id':'run','outcome':'intent','execution_state':'queued',
            'capacity':{'waiting_reason':'workspace_total_capacity'},'replay_allowed':False}
        with patch('run_worker._selected_run',return_value=run):
            value=self.service.result({'review_id':'review'})
        self.assertEqual(value['state'],'queued')
        self.assertEqual(value['capacity']['waiting_reason'],'workspace_total_capacity')
        self.assertIsNone(value['last_tool_activity'])
        self.assertFalse(value['replay_allowed'])

    def test_tool_activity_is_bound_to_the_original_execution_and_turn(self):
        from execution_lease import metadata_path, tool_activity
        with tempfile.TemporaryDirectory() as binding:
            native={'host':'deepseek_harness','kind':'session','id':'session','turn_id':'1'}
            run={'id':'run','request_id':'review','native_identity':native}
            path=metadata_path(binding,'run')
            path.parent.mkdir(parents=True)
            tools={'legacy':{'phase':'spawned','process':{'start':'Fri Nov 20 16:19:53 2026'}},
                   'intent':{'phase':'launch_intent','started_at':'2026-11-21T05:00:00+00:00'}}
            record={'run_id':'run','request_id':'review','native_identity':native,'tools':tools}
            path.write_text(json.dumps(record))
            with patch('harness_package.launch_spec',return_value={'evidence_home':binding}):
                self.assertEqual(tool_activity(binding,run),{'at':'Fri Nov 20 16:19:53 2026',
                    'source':'original_tool_os_start','clock':'original_host_local_time'})
                tools['utc']={'phase':'spawned','started_at':'2026-11-21T00:44:05+00:00'}
                path.write_text(json.dumps(record))
                self.assertEqual(tool_activity(binding,run),{'at':'2026-11-21T00:44:05+00:00',
                    'source':'original_tool_spawn'})
                self.assertIsNone(tool_activity(binding,{**run,'native_identity':{**native,'turn_id':'2'}}))
                self.assertIsNone(tool_activity(binding,{**run,'request_id':'other-review'}))
                path.unlink()
                self.assertIsNone(tool_activity(binding,run))


class LiveReviewScopeTests(unittest.TestCase):
    from test_agent_runtime import RuntimeTests as Fixture
    setUp=Fixture.setUp
    ref=Fixture.ref
    create_request=Fixture.create_request
    execute=Fixture.execute
    prepare_review=Fixture.prepare_review

    def test_native_success_before_return_index_uses_retained_read_to_repair_same_opinion(self):
        import agent_runtime
        from test_agent_runtime import CompletedAdapter
        import harness_tasks as tasks
        import run_worker
        author,packet=self.prepare_review()
        worker=agent_runtime.Runtime(self.root,adapter_factory=lambda root,config:CompletedAdapter(root,config,
            author=agent_runtime.identity('codex','thread','isolated-reviewer',root,'review-turn')))
        original=worker._return;received=[]
        def during_publication(run_id,receipt,result):
            run=tasks.read_run(self.root,run_id)
            self.assertEqual(run['outcome'],'succeeded');self.assertEqual(run['return_ids'],[])
            reader=agent_runtime.Runtime(self.root,adapter_factory=lambda *_:self.fail('Retained review read cannot start a model'))
            opinion=run_worker._program_opinion(self.root,reader,tasks.read_request(self.root,'request'),run)
            self.assertEqual(opinion['return_id'],'runtime-return:'+run_id)
            self.assertIn('Reviewed exact supplied scope',opinion['opinion']['text'])
            received.append(opinion)
            return original(run_id,receipt,result)
        with patch.object(worker,'_return',side_effect=during_publication):
            done=worker.execute(config_path='runtime.json',role='scientific_reviewer',task_id='task',
                request_id='review',run_id='review-run',plan_revision=1,input_path=str(packet),
                input_sha256=self.ref(packet)['sha256'],evidence_home=str(self.home))
        self.assertEqual(done['outcome'],'succeeded');self.assertEqual(len(received),1)
        self.assertEqual(tasks.read_run(self.root,'review-run')['return_ids'],['runtime-return:review-run'])

    def test_long_lived_author_inherits_current_explicit_scope_for_new_review(self):
        import harness_scope
        import harness_tasks as tasks
        import run_worker
        import json
        self.execute()
        service=self.adapters[0].review_service
        self.assertEqual(service.request['required_scope'],['science'])
        harness_scope.apply_scope(self.root,'task',{
            'items':[{'scope_id':sid,'kind':'requirement','label':sid,'source_refs':[self.evidence]} for sid in ('A11','A12')],
            'links':[{'request_id':'request','scope_ids':['A11','A12']}],
            'source_ref':self.evidence},self.actor)
        artifact=self.home/'candidate.json';artifact.write_text('{"mass_kg": 1}')
        # Keep this test on the registration/check boundary, without a model job.
        existing={'id':'fixture-review-run','outcome':'intent','latest_receipt':None}
        with patch('run_worker._selected_run',return_value=existing):
            response=service.request_review({'question':'Check original mass and source basis.','artifacts':[str(artifact)]})
        review=tasks.read_request(self.root,response['review_id'])
        packet=run_worker._review_packet(self.root,review['input_ref'])
        self.assertEqual(review['required_scope'],['A11','A12'])
        self.assertEqual(packet['scope_keys'],['A11','A12'])
        self.assertEqual(service.request['required_scope'],['science'])
        self.assertTrue(harness_scope.check(self.root,'request',review['id'],'dispatch')['ready'])
        # Reusing the frozen question/candidate keeps its original review and bytes.
        original_bytes=json.dumps(packet,sort_keys=True)
        again=run_worker._program_review_request(self.root,service.worker,service.request,packet)
        self.assertEqual(again['id'],review['id'])
        self.assertEqual(json.dumps(run_worker._review_packet(self.root,again['input_ref']),sort_keys=True),original_bytes)
