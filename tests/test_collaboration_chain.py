"""Public commands + real worker transport with scripted NDJSON (no model or remote writes)."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import signal
import time
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_dsh_headless as fixtures
from task_ledger import sessions
import harness_tasks as tasks
import harness_scope
import run_worker
import review_fixtures as review_data
from dsh_test_runtime import installed_dsh, requires_dsh


class CollaborationChainTests(unittest.TestCase):
    def setUp(self):
        self.native_query = fixtures.dsh_headless.DeepSeekAdapter._query
        self.f = fixtures.DeepSeekProgramTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(Path(tasks.__file__).with_name('harness.py')),
            '--root', str(self.f.root), *args], env=dict(os.environ), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def evidence_contract(self, task_id, request_id, scope_id='science'):
        f=self.f;request=tasks.read_request(f.root,request_id);source=request['input_ref']
        harness_scope.apply_scope(f.root,task_id,{'items':[{'scope_id':scope_id,'kind':'deliverable',
            'label':'The original isolated evidence and transport proof','source_refs':[source],
            'target_endpoint':'evidence'}],'links':[{'request_id':request_id,'scope_ids':[scope_id]}],
            'verified_scope_ids':[scope_id],'source_ref':source},f.actor)
        body={'purpose':'Deliver local fixture evidence with the original limits','endpoint':'evidence',
            'scope_boundary':{'in_scope':'Original isolated evidence, review and revision',
                'out_of_scope':'No real scientific acceptance, online writes or publication'},
            'acceptance':{'text':'Retain the exact report and original source/limitations','refs':[source]},
            'authority_refs':[],'interfaces':{'text':'Lead receives the original evidence','refs':[]}}
        with tasks.transaction(f.root) as c:
            harness_scope.set_contract(c,f.root,'task',task_id,body,source,f.actor)
            harness_scope.set_contract(c,f.root,'request',request_id,body,source,f.actor)

    def final_evidence_adoption(self, run, note, scope_id='science'):
        returned=run_worker.return_result(self.f.root,self.f.actor,run_id=run['id'],scope_ids=[scope_id])
        self.cli('work-dispose','--return-id',returned['id'],'--use','adopt','--decision',str(note),
            '--scope',scope_id,'--acceptance','final')

    def offline_provider_pilot(self):
        import dsh_capacity as capacity
        from execution_lease import atomic_json
        queue = capacity._queue(self.f.root)
        self.assertFalse(queue['entries'])
        queue.update(policy=capacity.PILOT_POLICY, pilot_provider='htam-offline')
        atomic_json(capacity._paths(self.f.root)[0], queue)

    def managed_offline_recovery(self):
        from execution_lease import atomic_json
        atomic_json(self.f.root/'.control/dsh-recovery.json', {'provider':'htam-offline'})

    @contextmanager
    def installed_host(self, initial_phase='seed', **options):
        f = self.f
        node, module = installed_dsh()
        provider = Path(__file__).with_name('dsh_scripted_provider.mjs').resolve()
        config = json.loads(f.config_path.read_text())
        config['hosts']['dsh'].update(binary=str(Path(__file__).with_name('dsh_fixture_cli.py').resolve()),
            node=node, launch_mode='desktop-cli', module_entry=module)
        config['providers']['dsh']['dsh_provider'] = 'htam-offline'
        config['roles']['executor'].pop('effort', None)
        broken_policy = options.pop('broken_policy', False)
        config['roles']['executor'].update(profile='native-workspace', native_policy={
            'sandbox': 'workspace-write', 'network_access': options.pop('network', False),
            'writable_roots': [str(f.home), str(f.root / 'future-package')]})
        config['roles']['scientific_reviewer'] = {**config['roles']['executor'],
            'profile': 'read-only-evidence', 'native_policy': {'network_access': False}}
        f.config_path.write_text(json.dumps(config))
        (f.root/'ISOLATED-NATIVE-COLLABORATION').touch()
        state=f.home/'native-fixture-provider.json'
        value={'root':str(f.root),'node':node,'provider':str(provider),'moduleEntry':module,
               'phase':initial_phase,'candidate':str(f.candidate),'broken_policy':broken_policy,**options}
        state.write_text(json.dumps(value))
        class Phase(list):
            def __setitem__(self,index,phase):
                super().__setitem__(index,phase)
                value['phase']=phase;state.write_text(json.dumps(value))
        phase=Phase([initial_phase])
        with patch.dict(os.environ,HTAM_NATIVE_FIXTURE_CONFIG=str(state)), \
             patch.object(fixtures.dsh_headless.DeepSeekAdapter, '_query', new=self.native_query):
            yield phase

    @requires_dsh
    def test_responses_wire_distinguishes_calls_text_and_sse_failure(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        for scenario in ('structured', 'text', 'broken', 'effect_then_broken', 'automatic', 'read_only_then_broken',
                         'timeout_once', 'timeout_capped', 'timeout_after_content', 'upstream_timeout'):
            with self.subTest(scenario=scenario):
                self.f = fixtures.DeepSeekProgramTests('runTest')
                self.f.setUp()
                self.addCleanup(self.f.doCleanups)
                self._qualify_responses(scenario, BaseHTTPRequestHandler, ThreadingHTTPServer, Thread)

    def _qualify_responses(self, scenario, handler, server_type, thread_type):
        f = self.f
        requests = []
        traces = []
        literal = '<</bash> printf I-AM-PLAIN-TEXT\nA legitimate plain-text result.'
        package = run_worker.program_home(tasks.read_request(f.root, 'request'), root=f.root)
        effect = package / 'work' / 'wire-effect.txt'

        class Responses(handler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(data)
                traces.append(self.headers.get('traceparent'))
                step = len(requests)
                if scenario == 'timeout_once' and step == 1:
                    # No headers arrive for the first request; the next one succeeds.
                    time.sleep(1.5)
                    return
                reply = self.frames(step, data)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('x-request-id','offline-wire-'+str(step))
                if scenario in ('timeout_capped','timeout_after_content'):
                    self.end_headers()
                    if scenario=='timeout_after_content':
                        self.wfile.write(reply)
                    self.wfile.flush()
                    time.sleep(1.5)
                    return
                self.send_header('Content-Length', str(len(reply)))
                self.end_headers()
                if scenario=='upstream_timeout':
                    prefix, boundary, terminal=reply.partition(b'event: response.failed\n')
                    self.wfile.write(prefix)
                    self.wfile.flush()
                    time.sleep(60 if os.environ.get('HTAM_DSH_1122_LONG_STALL') else .2)
                    reply=boundary+terminal
                self.wfile.write(reply)

            def frames(self, step, data):
                if (scenario == 'broken' or scenario == 'effect_then_broken' and step == 2
                        or scenario in ('automatic', 'read_only_then_broken') and step == 3):
                    return b'event: response.completed\ndata: {"type": SECRET_FIXTURE_INVALID_JSON\n\n'
                call = None
                if scenario in ('structured', 'automatic', 'read_only_then_broken') and step == 1:
                    call = ('create_goal', {'objective': 'Qualify real Responses tool calls', 'max_goal_rounds': 4})
                if (scenario,step) in (('structured',2),('automatic',6),('read_only_then_broken',5)):
                    previous = next(item for item in reversed(data['input']) if item.get('type') == 'function_call_output')
                    tool_result = previous['output']
                    if isinstance(tool_result, list):
                        tool_result = ''.join(part['text'] for part in tool_result if 'text' in part)
                    goal = json.loads(tool_result)['goal']
                    call = ('update_goal', {'goal_id': goal['id'], 'revision': goal['revision'], 'action': 'complete'})
                if (scenario, step) in (('structured',3),('effect_then_broken',1),('automatic',2)):
                    call = ('write', {'file_path': str(effect), 'content': 'one original effect'})
                if (scenario, step) == ('read_only_then_broken', 2):
                    call = ('bash', {'command': 'ls .', 'description': 'Read a directory without changing the original work'})
                if scenario=='automatic' and step==4:
                    call=('read',{'file_path':str(effect)})
                if (scenario,step) in (('automatic',5),('read_only_then_broken',4)):
                    call=('get_goal',{})
                if call:
                    name, arguments = call
                    item = {'type': 'function_call', 'name': name, 'arguments': json.dumps(arguments),
                            'call_id': 'call_qualified_'+str(step), 'id': 'fc_qualified_'+str(step), 'status': 'completed'}
                    events = [('response.output_item.added', {'output_index': 0,
                        'item': {**item, 'arguments': '', 'status': 'in_progress'}}),
                        ('response.function_call_arguments.delta', {'output_index': 0, 'item_id': item['id'],
                            'delta': item['arguments']}),
                        ('response.output_item.done', {'output_index': 0, 'item': item})]
                else:
                    content = {'type': 'output_text', 'text': literal, 'annotations': []}
                    item = {'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'id': 'msg_qualified', 'content': [content]}
                    events = [('response.output_item.added', {'output_index': 0, 'item': {**item, 'content': []}}),
                              ('response.content_part.added', {'output_index': 0, 'content_index': 0,
                                  'part': {**content, 'text': ''}}),
                              ('response.output_text.delta', {'output_index': 0, 'content_index': 0, 'delta': literal}),
                              ('response.output_item.done', {'output_index': 0, 'item': item})]
                usage = {'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13}
                if scenario=='timeout_after_content':
                    events=events[:-1]
                    return self.encode(events)
                if scenario=='upstream_timeout':
                    failure={'code':'TIMEOUT','message':'Upstream stalled: no response data within the proxy timeout'}
                    return self.encode(events[:-1]+[('response.failed',{'response':{
                        'id':'resp_'+str(step),'status':'failed','output':[item],'error':failure}})])
                events.append(('response.completed', {'response': {'id': 'resp_' + str(step), 'status': 'completed',
                    'output': [item], 'usage': usage}}))
                return self.encode(events)

            @staticmethod
            def encode(events):
                return ''.join('event: '+kind+'\ndata: '+json.dumps({'type': kind, **body})+'\n\n'
                               for kind, body in events).encode()

        # Each dispatch gets a new isolated task ledger and native home.
        with server_type(('127.0.0.1', 0), Responses) as server:
            thread = thread_type(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_port)
                if scenario in ('broken', 'automatic', 'read_only_then_broken'):
                    self.offline_provider_pilot()
                timeout_fixture = {'wire_idle_timeout_ms': 550, 'wire_retries': 5} if scenario.startswith('timeout_') else {}
                if scenario == 'timeout_once':
                    timeout_fixture['wire_retry_delay_ms'] = 1500
                if scenario=='timeout_after_content':
                    timeout_fixture['wire_retries']=1
                with self.installed_host('wire', wire=url+'/v1', wire_model='deepseek-fixture',
                                         network=scenario == 'read_only_then_broken', **timeout_fixture):
                    if scenario == 'timeout_once':
                        from dsh_capacity import snapshot
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            future = pool.submit(f._dispatch)
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline and not future.done():
                                view = snapshot(f.root)
                                if view.get('waiting_retry'):
                                    self.assertEqual(view['counts']['occupied']['total'], 0)
                                    self.assertEqual(len(requests), 1)
                                    break
                                time.sleep(.04)
                            else:
                                self.fail('Original backoff did not yield the model seat')
                            outcome = future.result(timeout=10)
                    else:
                        outcome = f._dispatch()
            finally:
                server.shutdown()
                thread.join(3)
        run = tasks.read_run(f.root, outcome['run_id'])
        terminal = fixtures.runtime.read_receipt(f.root, run['latest_receipt'])
        self.assertIn('result',terminal,outcome)
        receipt = terminal['result']
        if not requests:
            notes = [path.read_text(errors='replace') for path in package.glob('runs/*/diagnostics/*.json')]
            self.fail(f"No loopback request. {receipt.get('native_error')}\n" + '\n'.join(notes))
        tool = next(tool for tool in requests[0]['tools'] if tool.get('name') == 'request_review')
        self.assertEqual(tool['parameters']['required'], ['artifacts', 'question'])
        self.assertIn('update_goal', [t.get('name') for t in requests[0]['tools']])
        observation = receipt['native_session_observation']
        timings = receipt['turn_activity']['request_timings']
        initial_attempts = 3 if scenario in ('automatic','read_only_then_broken') else len(requests)
        self.assertEqual(len(timings), initial_attempts, timings)
        for index, attempt in enumerate(timings, 1):
            self.assertGreater(attempt['step'], 0)
            wire, = attempt['http']
            self.assertRegex(wire['client_traceparent'],r'^00-[0-9a-f]{32}-[0-9a-f]{16}-00$')
            self.assertEqual(wire['client_traceparent'],traces[index-1])
            self.assertLessEqual(attempt['started_at_ms'], wire['sent_at_ms'])
            self.assertLessEqual(wire['sent_at_ms'], attempt['finished_at_ms'])
            if (scenario, index) != ('timeout_once', 1):
                self.assertLessEqual(wire['sent_at_ms'], wire['headers_at_ms'])
                self.assertEqual(wire['request_ids'], {'x-request-id': 'offline-wire-'+str(index)})
                self.assertLessEqual(wire['headers_at_ms'], attempt.get('first_content_at_ms', attempt['finished_at_ms']))
            else:
                self.assertNotIn('headers_at_ms', wire)
            if 'first_content_at_ms' in attempt:
                self.assertLessEqual(attempt['first_content_at_ms'], attempt['last_content_at_ms'])
                self.assertLessEqual(attempt['last_content_at_ms'], attempt['finished_at_ms'])
        self.assertNotIn('isolated-fixture', json.dumps(timings))
        self.assertNotIn('SECRET_FIXTURE_INVALID_JSON', json.dumps(timings))
        if scenario == 'timeout_once':
            self.assertEqual([item.get('error_code') for item in timings], ['TIMEOUT', None])
            scheduled, started = receipt['turn_activity']['request_retries']
            self.assertEqual((scheduled['phase'], started['phase']), ('scheduled', 'started'))
            self.assertEqual(scheduled['step'], started['step'])
            self.assertLessEqual(scheduled['at_ms'], started['at_ms'])
        if scenario == 'timeout_capped':
            self.assertEqual(len(timings), 5)
            self.assertTrue(all(item.get('error_code') == 'TIMEOUT' for item in timings), timings)
            self.assertEqual(len(receipt['turn_activity']['request_retries']), 8)
            self.assertEqual(receipt['turn_activity']['timeout_retry_limits'][0]['failures'], 5)
            self.assertEqual(outcome['transport_diagnostic']['phase'], 'after_headers_before_model_content')
            self.assertEqual(outcome['transport_diagnostic']['step'], timings[-1]['step'])
        if scenario=='timeout_after_content':
            self.assertEqual(outcome['transport_diagnostic']['phase'],'after_model_content')
            wire=timings[-1]['http'][-1]
            self.assertGreater(wire['sse_body']['bytes_received'],0)
            self.assertNotIn('error_event',wire['sse_body'])
            self.assertGreaterEqual(timings[-1]['finished_at_ms']-wire['sse_body']['last_byte_at_ms'],450)
            self.assertEqual(outcome['transport_diagnostic']['local_idle_timeout']['code'],'LLM_STREAM_IDLE_TIMEOUT')
        if scenario=='upstream_timeout':
            wire=timings[-1]['http'][-1]
            terminal=wire['sse_body']['error_event']
            self.assertEqual((terminal['type'],terminal['code']),('response.failed','TIMEOUT'))
            self.assertLessEqual(terminal['at_ms'],timings[-1]['finished_at_ms'])
            self.assertGreaterEqual(terminal['at_ms']-timings[-1]['last_content_at_ms'],
                                    59_000 if os.environ.get('HTAM_DSH_1122_LONG_STALL') else 120)
            self.assertEqual(outcome['outcome'],'failed',receipt)
        if scenario in ('timeout_once', 'timeout_capped'):
            capacity_events = [event for event in run['events'] if event['kind'] == 'capacity']
            phases = [event['payload']['phase'] for event in capacity_events]
            self.assertEqual(phases, ['queued','admitted'] + ['waiting_retry','queued','admitted']*(len(timings)-1) + ['released'])
            if scenario == 'timeout_capped':
                closed = datetime.fromisoformat(receipt['turn_activity']['native_terminal_at'])
                released = datetime.fromisoformat(capacity_events[-1]['created_at'])
                seconds = (released - closed).total_seconds()
                self.assertGreaterEqual(seconds, 0)
                self.assertLess(seconds, 5)
                if directory := os.environ.get('HTAM_1014_EVIDENCE'):
                    (Path(directory)/'native-model-release.json').write_text(json.dumps({
                        'terminal_to_release_seconds': seconds, 'model_requests': len(requests)}, indent=2)+'\n')
        expected_calls = {'structured': ['create_goal', 'update_goal', 'write'], 'effect_then_broken': ['write'],
                          'automatic':['create_goal','write'], 'read_only_then_broken':['create_goal','bash']}
        self.assertEqual([call['name'] for call in observation['tool_calls']], expected_calls.get(scenario, []), receipt)
        if scenario == 'structured':
            self.assertEqual(observation['native_goal']['phase'], 'complete')
        if scenario in expected_calls:
            self.assertTrue(all(call['result'] == 'returned' for call in observation['tool_calls']),
                            [call for call in observation['tool_calls'] if call['result'] != 'returned'])
        if 'write' in expected_calls.get(scenario, []):
            self.assertEqual(effect.read_text(), 'one original effect')
            self.assertTrue(any(item.get('type') == 'function_call_output' for item in requests[1]['input']))
        if 'broken' in scenario or scenario in ('automatic','timeout_capped','timeout_after_content'):
            self.assertEqual(outcome['outcome'], 'failed', receipt)
            self.assertEqual(observation['native_error']['code'],
                             'TIMEOUT' if scenario.startswith('timeout_') else 'PI_AI_ERROR')
            reread = run_worker.programmatic(f.root, 'request', read_result=True, exact_run_id=run['id'])
            self.assertEqual(reread['transport_diagnostic'], outcome['transport_diagnostic'])
            if not scenario.startswith('timeout_'):
                diagnostic = outcome['transport_diagnostic']
                self.assertEqual(diagnostic['phase'], 'invalid_complete_event')
                self.assertEqual(diagnostic['response']['request_ids'], timings[-1]['http'][-1]['request_ids'])
                self.assertNotIn('SECRET_FIXTURE_INVALID_JSON', json.dumps(diagnostic))
        elif scenario!='upstream_timeout':
            self.assertEqual(outcome['outcome'], 'succeeded', receipt)
            self.assertEqual(observation['final_text'], literal)
        if scenario in ('broken', 'automatic', 'read_only_then_broken'):
            from dsh_auto_resume import FAULT, CLAIM
            facts=tasks.read_run(f.root,run['id'])['events']
            fault,=[event for event in facts if event['kind']==FAULT]
            self.assertEqual(fault['payload']['kind'],'sse_json_decode')
            probes=[json.loads(path.read_bytes()) for path in (package/'runs'/run['id']/'diagnostics').glob('sse-*.json')
                    if not path.name.startswith('sse-timing')]
            self.assertEqual(len(probes),1,probes)
            self.assertEqual(probes[0]['failed_event']['body_omitted'],True)
            self.assertNotIn('SECRET_FIXTURE_INVALID_JSON',json.dumps(probes))
            if scenario in ('automatic','read_only_then_broken'):
                child,=[event for event in facts if event['kind']==CLAIM]
                resumed=tasks.read_run(f.root,child['payload']['next_run_id'])
                self.assertEqual(resumed['outcome'],'succeeded',resumed)
                self.assertEqual(resumed['native_identity']['id'],run['native_identity']['id'])
                answer=fixtures.runtime.read_receipt(f.root,resumed['latest_receipt'])['result']
                self.assertEqual(answer['native_session_observation']['native_goal']['id'],observation['native_goal']['id'])
                self.assertEqual(answer['native_goal_observation']['goal']['phase'],'complete')
                if scenario == 'automatic':
                    self.assertEqual(len(requests),7)
                    self.assertIsNotNone(answer['turn_activity']['first_effective_action_at'])
                else:
                    self.assertEqual(len(requests),6)
                    self.assertEqual(receipt['turn_activity']['ending_turn_verified_progress'], [])
                    self.assertEqual(receipt['shell_results'][0]['exit_code'], 0)
                    self.assertEqual(observation['native_goal']['roundsStarted'], 0)
                    self.assertEqual(answer['native_session_observation']['native_goal']['phase'],'complete')
                    self.assertFalse(effect.exists())
            else:
                self.assertFalse(any(event['kind']==CLAIM for event in facts))
                self.assertEqual(receipt['turn_activity']['ending_turn_verified_progress'], [])
        if directory := os.environ.get('HTAM_1010_EVIDENCE'):
            path = Path(directory) / ('fe-' + scenario + '.json')
            path.write_text(json.dumps({'outcome': outcome['outcome'], 'observed': observation,
                'tool_choice': requests[0].get('tool_choice'), 'tool_declaration': tool,
                'received_http_requests': len(requests), 'local_effect': effect.exists()}, ensure_ascii=False, indent=2))
        if (directory := os.environ.get('HTAM_DSH_1122_EVIDENCE')) and scenario in (
                'text','broken','timeout_after_content','upstream_timeout'):
            (Path(directory)/('wire-'+scenario+'.json')).write_text(json.dumps({
                'fixture':scenario,'diagnostic':outcome.get('transport_diagnostic'),'requests':timings,
                'native_error':observation.get('native_error'), 'native_terminal':observation['terminal'],
                'client_and_server_trace_ids_match':True},ensure_ascii=False,indent=2)+'\n')
        if (directory := os.environ.get('HTAM_1014_EVIDENCE')) and (observer := os.environ.get('HTAM_1014_OBSERVER')):
            from importlib.util import module_from_spec, spec_from_file_location
            source=Path(observer)
            spec=spec_from_file_location('dsh_benchmark_receipt',source)
            benchmark=module_from_spec(spec);spec.loader.exec_module(benchmark)
            measured=benchmark.observation(f.root,run,{'case':scenario,'arm':'scripted','load_target':1})
            for key in ('receipt','opinion_refs'):measured.pop(key)
            measured.update(model_http_requests=len(requests),production_operations=0,isolated=True)
            (Path(directory)/('wire-'+scenario+'.json')).write_text(json.dumps(measured,ensure_ascii=False,indent=2)+'\n')

    @requires_dsh
    def test_persisted_goal_resume_through_public_dispatch_review_revision_and_adoption(self):
        f = self.f
        case = os.environ.get('HTAM_1010_A08')
        options = {}
        if case:
            import shutil
            originals = Path(case)
            for name in ('original_candidate', 'revised_candidate', 'calculation'):
                path = f.home / (name + ('.md' if name == 'calculation' else '.json'))
                shutil.copyfile(originals / path.name, path)
                options[name] = str(path)
        with self.installed_host(slow_flush_ms=60, **options) as phase:
            self.evidence_contract('task','request')
            self.assertFalse((f.root / 'future-package').exists())
            started = f._dispatch()
            self.assertTrue((f.root / 'future-package').is_dir())
            self.assertEqual(started['outcome'], 'failed', started)
            original = tasks.read_run(f.root, started['run_id'])
            old_receipt = fixtures.runtime.read_receipt(f.root, original['latest_receipt'])
            self.assertIn('native_goal', old_receipt['result']['native_session_observation'], old_receipt['result'])
            old_goal = old_receipt['result']['native_session_observation']['native_goal']
            self.assertIsNotNone(old_goal, old_receipt['result'])
            self.assertEqual(old_goal['phase'], 'active')
            note = f.home / 'continue-native.md'
            note.write_text('Continue the original complete package: resume its Goal, obtain review, revise and finish. Read roles/data.md.')
            phase[0] = 'resume'
            continued = f._dispatch(instructions=note)
            self.assertEqual(continued['outcome'], 'succeeded', continued)
            current = tasks.read_run(f.root, continued['run_id'])
            self.assertEqual(current['native_identity']['id'], original['native_identity']['id'])
            self.assertGreater(int(current['native_identity']['turn_id']),int(original['native_identity']['turn_id']))
            receipt = fixtures.runtime.read_receipt(f.root, current['latest_receipt'])['result']
            self.assertEqual(receipt['native_goal_observation']['goal']['id'], old_goal['id'])
            self.assertEqual(receipt['native_goal_observation']['goal']['phase'], 'complete')
            self.assertGreater(receipt['native_goal_observation']['goal']['roundsStarted'],old_goal['roundsStarted'])
            self.assertEqual(receipt['native_goal_observation']['goal']['objective'],old_goal['objective'])
            self.assertEqual(receipt['native_goal_observation']['goal']['activation'], 'disarmed')
            candidate = run_worker.program_home(tasks.read_request(f.root, 'request'), root=f.root) / 'work/candidate.json'
            if case:
                self.assertEqual(candidate.read_bytes(), Path(options['revised_candidate']).read_bytes())
                self.assertEqual(len(json.loads(candidate.read_bytes())['processDataSet']['exchanges']['exchange']), 19)
            else:
                self.assertEqual(json.loads(candidate.read_text()), {'basis': 'dry', 'value': 4})
            snapshot = tasks.read_task(f.root, 'task')
            review, = [r for r in snapshot['runs'] if r['request_id'] != 'request']
            self.assertEqual(review['outcome'], 'succeeded')
            self.assertNotEqual(review['native_identity']['id'], current['native_identity']['id'])
            artifacts = json.loads(Path(tasks.read_request(f.root, review['request_id'])['input_ref']['path']).read_text())['artifact_refs']
            if case:
                import hashlib
                self.assertEqual({ref['sha256'] for ref in artifacts}, {
                    hashlib.sha256(Path(options[name]).read_bytes()).hexdigest()
                    for name in ('original_candidate', 'calculation')})
                for ref in artifacts:
                    self.assertEqual(hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest(), ref['sha256'])
                self.assertTrue(receipt['collected_review_return_ids'])
                self.assertEqual(list(f.root.glob('work/**/delivery/operation-*')), [])
            else:
                frozen, = artifacts
                self.assertEqual(json.loads(Path(frozen['path']).read_text()), {'basis': 'wet', 'value': 8})
            self.assertEqual(len(snapshot['runs']), 3)
            f._dispatch(instructions=note,run_id=original['id'])
            self.assertEqual(len(tasks.read_task(f.root, 'task')['runs']), 3)
            self.assertEqual(snapshot['task']['state'], 'active')
            packages = self.cli('work-packages', '--task', 'task')['packages']
            package, = packages
            self.assertEqual(package['currentPlan'], snapshot['task']['plan_ref'])
            resumed, = [p for p in package['programs'] if p['runId'] == current['id']]
            self.assertEqual(resumed['continuationRef']['path'], str(note))
            self.assertEqual(resumed['goalObservation']['goal']['phase'], 'complete')
            self.assertEqual(resumed['goalObservation']['source'], 'dsh_live_agent')
            self.assertEqual(resumed['goalObservation']['observedAt'], 'invocation_end')
            self.assertEqual(tasks.read_run(f.root, original['id'])['latest_receipt'], original['latest_receipt'])
            accepted = f.home / 'adopt-native.md'; accepted.write_text('Adopt the revised dry-mass fixture. Original complete local objective fulfilled.')
            self.cli('work-dispose', '--return-id', current['return_ids'][0], '--use', 'adopt', '--decision', str(accepted))
            pending=self.cli('work-complete','--task','task','--note',str(accepted))
            self.assertEqual(pending['status'],'completion_pending')
            self.final_evidence_adoption(current,accepted)
            self.cli('work-complete', '--task', 'task', '--note', str(accepted))
            self.assertEqual(tasks.read_task(f.root, 'task')['task']['state'], 'completed')
            if case and (directory := os.environ.get('HTAM_1010_EVIDENCE')):
                proof = {'author_session': current['native_identity']['id'],
                    'reviewer_session': review['native_identity']['id'],
                    'original_goal': old_goal, 'final_goal': receipt['native_goal_observation']['goal'],
                    'frozen': [{'id': ref['id'], 'sha256': ref['sha256']} for ref in artifacts],
                    'revised_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    'review_returns': receipt['collected_review_return_ids'], 'remote_operations': 0,
                    'lead_task_state': tasks.read_task(f.root, 'task')['task']['state']}
                (Path(directory)/'a08-native-chain.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))

    @requires_dsh
    def test_installed_initial_review_gets_exact_facts_and_sources(self):
        from harness_package import launch_spec
        f = self.f
        candidate, flow, source = (f.home/name for name in ('candidate.json','flow.json','basis.txt'))
        body = review_data.process()
        review_data.save(candidate, body)
        review_data.save(flow, review_data.flow())
        source.write_text('Measured 125 g of dry input per kg of product.')
        selected = [str(candidate), str(flow), str(source)]
        verifier = f.home/'check-review-facts.py'
        verifier.write_text('''import json,sys
from hashlib import sha256
from pathlib import Path
facts=json.loads(Path(sys.argv[1]).read_bytes())
subject,=facts['processes']
assert subject['readback']['status']=='complete_match'
assert subject['basis']['reference_exchanges'][0]['resulting_amount']=='1'
assert subject['exchanges'][1]['resulting_amount']=='0.125'
flow,=facts['frozen_datasets'].values()
assert json.loads(Path(flow['path']).read_bytes())['flowDataSet']
basis,=[ref for ref in facts['original_artifacts'] if ref['id'].endswith('basis.txt')]
raw=Path(basis['path']).read_bytes()
assert sha256(raw).hexdigest()==basis['sha256'] and b'125 g' in raw
print('opening-review-facts-verified')
''')
        self.managed_offline_recovery()
        with self.installed_host('seed', opening_artifacts=selected, opening_verifier=str(verifier),
                                 reviewer_transport_error=True, python=sys.executable) as phase:
            self.evidence_contract('task','request')
            seed = f._dispatch()
            author = tasks.read_run(f.root, seed['run_id'])
            archive = Path(launch_spec(f.root, author)['evidence_home'])/'runs'/author['id']/'delivery'
            readback = review_data.readback(archive, 'before', run_id=author['id'], request_id=author['request_id'],
                                           finished_at=fixtures.runtime.now(), body=body)
            phase[0] = 'opening_review'
            note = f.home/'resume-review.md'
            note.write_text('Resume the exact original author and request review of the isolated candidates.')
            outcome = f._dispatch(instructions=note)
        self.assertEqual(outcome['outcome'], 'succeeded', outcome)
        current = tasks.read_run(f.root, outcome['run_id'])
        self.assertEqual(current['native_identity']['id'], author['native_identity']['id'])
        initial_review, run = [r for r in tasks.read_task(f.root,'task')['runs'] if r['request_id']!='request']
        self.assertEqual(initial_review['outcome'], 'failed')
        self.assertEqual(initial_review['return_ids'], [])
        self.assertEqual(run['request_id'], initial_review['request_id'])
        self.assertEqual(run['native_identity']['id'], initial_review['native_identity']['id'])
        self.assertNotEqual(run['native_identity']['id'], author['native_identity']['id'])
        self.assertEqual(run['outcome'], 'succeeded', run)
        result = fixtures.runtime.read_receipt(f.root,run['latest_receipt'])['result']
        self.assertIn('opening-review-facts-verified', result['result'])
        self.assertEqual(result['turn_activity']['tool_calls'], 1)
        packet = tasks.read_request(f.root,run['request_id'])['input_ref']
        review_packet = json.loads(Path(packet['path']).read_bytes())
        frozen = json.loads(Path(review_packet['facts_ref']['path']).read_bytes())
        process, = frozen['processes']
        self.assertEqual(process['readback']['body_ref'], readback)
        self.assertEqual({ref['sha256'] for ref in review_packet['artifact_refs']},
                         {f.ref(Path(path))['sha256'] for path in selected})
        self.assertIn(run['return_ids'][0],
                      fixtures.runtime.read_receipt(f.root,current['latest_receipt'])['result']['collected_review_return_ids'])

    @requires_dsh
    def test_installed_focused_review_receives_original_and_changed_fields_then_lead_adopts(self):
        f=self.f
        options={}
        for name, document in zip(('original_candidate', 'revised_candidate'), review_data.revision_documents()):
            path = f.home/(name+'.json')
            review_data.save(path, document)
            options[name] = str(path)
        checker=f.home/'verify-delta.py'
        checker.write_text('''import json,sys
from pathlib import Path
d=json.loads(Path(sys.argv[1]).read_text())
assert len(d['changes'])==2
process=next(row for row in d['changes'] if row['new_identity'][0]=='process')
assert process['self_uri']['before']['status']=='differs'
assert process['self_uri']['after']['status']=='matches'
assert any('zh' in row.get('changed_languages',[]) for row in process['fields'])
assert process['exchange_diff']['changes']
print('delta-local-check-passed')
''')
        with self.installed_host('focused',focused=True,delta_verifier=str(checker),python=sys.executable,**options):
            self.evidence_contract('task','request')
            self.offline_provider_pilot()
            submitted=f._dispatch()
        self.assertEqual(submitted['outcome'],'succeeded',submitted)
        author=tasks.read_run(f.root,submitted['run_id'])
        self.assertIn('waiting_review',[e['payload'].get('phase') for e in author['events'] if e['kind']=='capacity'])
        self.assertGreater(len([e for e in author['events'] if e['kind']=='capacity' and e['payload'].get('phase')=='admitted']),1)
        direct=[r for r in tasks.read_task(f.root,'task')['runs'] if r['request_id']!='request']
        self.assertEqual(len(direct),2)
        first,focused=direct
        packet=tasks.read_request(f.root,focused['request_id'])['input_ref']
        data=json.loads(Path(packet['path']).read_bytes())
        self.assertEqual(data['prior_review']['review_id'],first['request_id'])
        self.assertEqual(data['prior_review']['return_id'],first['return_ids'][0])
        full=fixtures.runtime.read_receipt(f.root,focused['latest_receipt'])['result']
        self.assertIn('local_check',full['tools_called'])
        self.assertNotEqual(focused['native_identity']['id'],author['native_identity']['id'])
        returned=fixtures.runtime.read_receipt(f.root,author['latest_receipt'])['result']
        self.assertIn(focused['return_ids'][0],returned['collected_review_return_ids'])
        self.assertFalse(list(f.root.glob('work/**/delivery/operation-*')))
        note=f.home/'accept-fixture.md';note.write_text('Adopt the evidence of original-to-focused independent review delivery and isolation.')
        self.final_evidence_adoption(author,note)
        self.cli('work-complete','--task','task','--note',str(note))
        self.assertEqual(tasks.read_task(f.root,'task')['task']['state'],'completed')
        if directory:=os.environ.get('HTAM_1014_EVIDENCE'):
            evidence={'author':author['id'],'first_review':first['request_id'],'focused_review':focused['request_id'],
                      'opinion_return':focused['return_ids'][0],'focus':{k:v for k,v in data['focus'].items() if not k.endswith('_ref')},
                      'read_delivery_recorded':True,'lead_adopted_engineering_evidence':True,'production_operations':0}
            (Path(directory)/'focused-native-chain.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')

    @requires_dsh
    def test_failed_startup_dependency_returns_not_started_instead_of_hanging(self):
        f = self.f
        with self.installed_host(broken_policy=True):
            result = f._dispatch()
        self.assertEqual(result['outcome'], 'failed', result)
        run = tasks.read_run(f.root, result['run_id'])
        self.assertIsNone(run['native_identity'])
        receipt = fixtures.runtime.read_receipt(f.root, run['latest_receipt'])
        self.assertEqual(receipt['native_status'], 'not_started', receipt)
        self.assertIn('DSH_STARTUP_UNAVAILABLE', str(receipt))

    def test_original_brief_review_revision_adoption_and_lead_handoff(self):
        f = self.f
        brief = f.home / 'complete-package.json'
        # These domain fields used to trigger an inferred continuation or fail registration.
        brief.write_text(json.dumps({'purpose': {'consumer': 'local materials database'},
            'continues_request_id': 'an example in the source, not a command',
            'requires_review': True, 'coverage': 'ordinary source prose',
            'goal': 'Make the candidate useful, obtain review, revise and hand it to the lead.'}))
        task = self.cli('work-create', '--brief', str(brief), '--owner', f.actor['id'])
        request = self.cli('work-request', '--task', task['id'], '--brief', str(brief))
        self.evidence_contract(task['id'],request['id'])
        self.assertLessEqual(len(request['summary']), 240)
        self.assertEqual(Path(tasks.evidence_ref(f.root, request['input_ref'])['path']).read_bytes(), brief.read_bytes())
        self.assertEqual(self.cli('work-request', '--task', task['id'], '--brief', str(brief))['id'], request['id'])
        config = json.loads(f.config_path.read_text())
        config['roles']['executor'].update(profile='native-workspace', native_policy={
            'sandbox': 'workspace-write', 'network_access': False, 'writable_roots': [str(f.home)]})
        config['roles']['scientific_reviewer'] = {**config['roles']['executor'],
            'profile': 'read-only-evidence', 'native_policy': {'network_access': False}}
        f.config_path.write_text(json.dumps(config))
        candidate = run_worker.program_home(request, root=f.root) / 'work/candidate.json'
        first = f._stream(final='The candidate needs the reviewer’s suggested check. Current result retained.')
        first['write_artifact'] = True
        first['checks'] = [
            {'kind': 'request_review', 'accepted': True, 'text': json.dumps({
                'artifacts': [str(candidate)], 'question': 'Check the denominator for the local materials purpose.'})},
            {'kind': 'review_result', 'accepted': True}]
        reviewer = f._stream(final='Check dry mass before adoption; the existing value needs evidence.')
        reviewer['events'] = [e for e in reviewer['events'] if e['type'] not in ('tool_call', 'tool_result', 'htam_shell_result')]
        for event in reviewer['events']:
            if 'sessionId' in event:
                event['sessionId'] = 'fresh-independent-review'
        second = f._stream(turn=5, final='Revised using dry-mass evidence. Ready for the agreed local use; lead owns submission.')
        second['write_artifact'] = True
        second['artifact_body'] = '{"synthetic":4,"basis":"dry mass"}'
        f.state.write_text(json.dumps({**first, 'reviewer': reviewer, 'resumed': second}))
        def dispatch(instructions=None):
            with patch.dict(os.environ, HTAM_FIXTURE_ARTIFACT=str(candidate)):
                return sessions.dispatch(f.root, f.actor, request['id'], execute=True,
                    config=str(f.config_path), instructions=str(instructions) if instructions else None,
                    run_id=original['id'] if instructions else None)
        observed = dispatch()
        original = tasks.read_run(f.root, observed['run_id'])
        opinion_run, = [r for r in tasks.read_task(f.root, task['id'])['runs'] if r['request_id'] != request['id']]
        self.assertEqual(opinion_run['native_identity']['id'], 'fresh-independent-review')
        self.assertNotEqual(opinion_run['native_identity']['id'], original['native_identity']['id'])
        before = tasks.read_task(f.root, task['id'])
        self.assertEqual(before['dispositions'], [])
        self.assertEqual(before['task']['state'], 'active')
        frozen = json.loads(Path(tasks.read_request(f.root, opinion_run['request_id'])['input_ref']['path']).read_text())['artifact_refs'][0]
        self.assertEqual(Path(frozen['path']).read_text(), '{"synthetic":8}')
        hold = f.home / 'use.md'; hold.write_text('Not adopted yet; the author owns the remaining scientific work.')
        self.cli('work-dispose', '--return-id', original['return_ids'][0], '--use', 'hold', '--decision', str(hold))
        self.assertEqual(len(f.argv_log.read_text().splitlines()), 2)
        current_plan = f.home / 'current-plan.md'
        current_plan.write_text('Use the new dry-mass evidence for the original local delivery. Keep the original author and authorization.')
        self.cli('work-revise', '--task', task['id'], '--base-sha256', task['plan_ref']['sha256'],
                 '--note', str(current_plan), '--reason-file', str(hold), '--affected-request', request['id'])
        self.assertEqual(tasks.read_task(f.root, task['id'])['task']['plan_revision'], 2)
        self.assertEqual(tasks.read_request(f.root, request['id'])['plan_revision'], 1)
        instruction = f.home / 'continue.md'
        instruction.write_text('Finish the original local delivery using the review already received. Submission remains with the lead.')
        resumed = dispatch(instruction)
        resumed_run = tasks.read_run(f.root, resumed['run_id'])
        self.assertEqual(resumed_run['native_identity']['id'], original['native_identity']['id'])
        self.assertEqual(candidate.read_text(), second['artifact_body'])
        self.assertEqual(Path(frozen['path']).read_text(), '{"synthetic":8}')
        self.assertEqual(len(f.argv_log.read_text().splitlines()), 3)
        resumed_input = json.loads(f.argv_log.read_text().splitlines()[-1])['input']
        for original_path in (current_plan, brief, instruction):
            self.assertIn(str(original_path), resumed_input)
        self.assertNotIn(current_plan.read_text(), resumed_input)
        dispatch(instruction)
        self.assertEqual(len(f.argv_log.read_text().splitlines()), 3)
        adopted = f.home / 'adopt.md'; adopted.write_text('Adopt the revised local dataset. Platform submission is a separate lead action if authorized.')
        self.cli('work-dispose', '--return-id', resumed_run['return_ids'][0], '--use', 'adopt', '--decision', str(adopted))
        snapshot = tasks.read_task(f.root, task['id'])
        self.assertEqual(snapshot['task']['plan_revision'], 2)
        self.assertEqual(snapshot['task']['state'], 'active')
        self.assertEqual(len(snapshot['runs']), 3)
        # Completion is an explicit lead decision, not a final, verdict, review or receipt count.
        self.assertEqual(self.cli('work-complete','--task',task['id'],'--note',str(adopted))['status'],'completion_pending')
        self.final_evidence_adoption(resumed_run,adopted)
        self.cli('work-complete','--task',task['id'],'--note',str(adopted))
        self.assertEqual(tasks.read_task(f.root, task['id'])['task']['state'], 'completed')

    def wait_native_turn(self, turn):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            runs=tasks.read_task(self.f.root,'task')['runs']
            if runs and (runs[-1]['native_identity'] or {}).get('turn_id')==str(turn): return runs[-1]
            time.sleep(.02)
        self.fail('The isolated native turn did not start')

    @requires_dsh
    def test_native_pause_preserves_goal_without_an_extra_round(self):
        with self.installed_host('pause'):
            result=self.f._dispatch()
            self.assertEqual(result['outcome'],'succeeded',result)
            run=tasks.read_run(self.f.root,result['run_id'])
            observed=fixtures.runtime.read_receipt(self.f.root,run['latest_receipt'])['result']['native_goal_observation']['goal']
            self.assertEqual(observed['phase'],'paused')
            self.assertEqual(observed['roundsStarted'],0)
            self.assertEqual(run['native_identity']['turn_id'],'1')

    @requires_dsh
    def test_native_shell_failure_is_observation_not_author_restart(self):
        with self.installed_host('shell',network=True):
            result=self.f._dispatch()
            self.assertEqual(result['outcome'],'succeeded',result)
            run=tasks.read_run(self.f.root,result['run_id'])
            observed=fixtures.runtime.read_receipt(self.f.root,run['latest_receipt'])['result']
            self.assertEqual([x['exit_code'] for x in observed['shell_results']],[7])
            self.assertEqual(len(tasks.read_task(self.f.root,'task')['runs']),1)
            self.assertIn('command failed',observed['result'])

    @requires_dsh
    def test_native_explicit_stop_reaches_original_invocation(self):
        adapters=[];initialize=fixtures.dsh_headless.DeepSeekAdapter.__init__
        def capture(adapter,*args,**kwargs):
            initialize(adapter,*args,**kwargs);adapters.append(adapter)
        with self.installed_host('hold'), patch.object(fixtures.dsh_headless.DeepSeekAdapter,'__init__',new=capture), ThreadPoolExecutor(max_workers=1) as pool:
            running=pool.submit(self.f._dispatch)
            original=self.wait_native_turn(2)
            tasks.mutate(self.f.root,'run-stop',self.f.actor,{'id':original['id'],'summary':'Explicit isolated stop'})
            with self.assertRaises(fixtures.runtime.StopRequested):
                running.result(timeout=12)
            result=run_worker.programmatic(self.f.root,'request',config_path=str(self.f.config_path),read_result=True,exact_run_id=original['id'])
            stopped=tasks.read_run(self.f.root,original['id'])
            self.assertTrue(stopped['stop_requested'])
            native=next(adapter for adapter in adapters if adapter.process is not None)
            self.assertIsNotNone(native.process.poll(),'The exact original transport must have exited')
            with self.assertRaises(ProcessLookupError):os.killpg(native.process.pid,0)
            observed=native._query(stopped['native_identity'])
            if observed.get('terminal')=='aborted' and str(observed.get('latest_turn'))==stopped['native_identity']['turn_id']:
                self.assertEqual(result['outcome'],'stopped',result)
            else:
                # Process exit is separate from a native abort receipt. Keep
                # unresolved effects/activation unknown rather than forge ack.
                self.assertEqual(result['outcome'],'unknown',result)
            note=self.f.home/'ordinary-stop-continuation.md';note.write_text('Read the original stop; no authority to resume it.')
            with self.assertRaisesRegex(ValueError,'stop.*force|paused.*resume-stopped'):
                self.f._dispatch(instructions=note)
            self.assertEqual(stopped['native_identity']['id'],original['native_identity']['id'])
            self.assertEqual(len(tasks.read_task(self.f.root,'task')['runs']),1)

    @requires_dsh
    def test_native_disconnect_keeps_prior_text_and_unknown_invocation(self):
        adapters=[]
        initialize=fixtures.dsh_headless.DeepSeekAdapter.__init__
        def capture(adapter,*args,**kwargs):
            initialize(adapter,*args,**kwargs);adapters.append(adapter)
        with self.installed_host('hold'), patch.object(fixtures.dsh_headless.DeepSeekAdapter,'__init__',new=capture), ThreadPoolExecutor(max_workers=1) as pool:
            running=pool.submit(self.f._dispatch)
            original=self.wait_native_turn(2)
            adapter=adapters[0]
            self.assertIsNone(adapter.process.poll())
            os.killpg(adapter.process.pid,signal.SIGKILL)
            result=running.result(timeout=12)
            self.assertEqual(result['outcome'],'unknown',result)
            retained=adapter.readresult({**original['native_identity'],'turn_id':'1'})
            self.assertEqual(retained['native_status'],'unknown')
            self.assertEqual(retained['result'],'Original completed turn before interruption.')
            self.assertEqual(retained['native_session_observation']['native_goal']['phase'],'active')
            self.assertEqual(len(tasks.read_task(self.f.root,'task')['runs']),1)

    @requires_dsh
    def test_real_unclosed_native_turn_continues_same_session_without_replaying_effect(self):
        import execution_lease
        from harness_package import launch_spec
        f=self.f;effect=f.home/'unique-original-effect.txt';flushed=f.home/'open-turn-flushed.txt'
        adapters=[];initialize=fixtures.dsh_headless.DeepSeekAdapter.__init__
        def capture(adapter,*args,**kwargs):initialize(adapter,*args,**kwargs);adapters.append(adapter)
        with self.installed_host('hold',network=True,effect_file=str(effect),flush_open_turn_marker=str(flushed)) as phase,patch.object(fixtures.dsh_headless.DeepSeekAdapter,'__init__',new=capture),ThreadPoolExecutor(max_workers=1) as pool:
            running=pool.submit(f._dispatch);original=self.wait_native_turn(2)
            deadline=time.monotonic()+5
            while not flushed.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(flushed.exists(),'Crash only after the actual installed native persistence saved turn/start')
            os.killpg(adapters[0].process.pid,signal.SIGKILL)  # This exact isolated local-provider process only.
            result=running.result(timeout=12);self.assertEqual(result['outcome'],'unknown')
            original=tasks.read_run(f.root,original['id'])
            receipt=fixtures.runtime.read_receipt(f.root,original['latest_receipt'])['result']
            self.assertIn('bash',receipt['tools_called'],'The unique effect must be an actual installed native tool action')
            observed=adapters[0]._query(original['native_identity'])
            self.assertIsNone(observed.get('terminal'),'SIGKILL must leave a genuinely unclosed installed-native turn')
            metadata=execution_lease.metadata_path(launch_spec(f.root,original)['evidence_home'],original['id'])
            retained=f.home/'original-execution-before-legacy-copy.json';retained.write_bytes(metadata.read_bytes());metadata.unlink()
            observation=execution_lease.observe_run(f.root,original)
            deadline=time.monotonic()+20
            while observation['state']=='unknown' and observation.get('reason','').startswith('OS attribution incomplete'):
                if time.monotonic()>=deadline:break
                self.assertEqual([r['id'] for r in tasks.read_task(f.root,'task')['runs']],[original['id']])
                time.sleep(.1);observation=execution_lease.observe_run(f.root,original)
            self.assertEqual(observation['state'],'exited',observation)
            self.assertEqual(observation['native_result_state'],'unknown')
            phase[0]='resume'
            argv=[sys.executable,str(Path(tasks.__file__).with_name('harness.py')),'--root',str(f.root),
                'work-dispatch','--request','request','--config',str(f.config_path),'--execute','--continue','--run-id',original['id']]
            deadline=time.monotonic()+20
            while True:
                continued=subprocess.run(argv,env=dict(os.environ),text=True,capture_output=True)
                if continued.returncode==0:
                    value=json.loads(continued.stdout);break
                # Real unrelated OS process churn can make attribution unknown.
                # Preserve that guard; only a confirmed pre-intent refusal may
                # be observed again, never replay an admitted native invocation.
                self.assertIn('Original execution',continued.stderr)
                self.assertIn('OS attribution incomplete',continued.stderr)
                self.assertEqual([r['id'] for r in tasks.read_task(f.root,'task')['runs']],[original['id']])
                if time.monotonic()>=deadline:self.fail(continued.stderr)
                time.sleep(.1)
            self.assertEqual(value['outcome'],'succeeded',value)
            resumed=tasks.read_run(f.root,value['run_id'])
            self.assertEqual(resumed['native_identity']['id'],original['native_identity']['id'])
            self.assertEqual(tasks.read_run(f.root,original['id'])['outcome'],'unknown')
            self.assertEqual(effect.read_text(),'original-effect\n')
            self.assertEqual(json.loads(f.candidate.read_text()),{'basis':'dry','value':4})

    @requires_dsh
    def test_only_the_stuck_model_request_times_out_and_releases_the_seat(self):
        from dsh_capacity import snapshot
        f = self.f
        with self.installed_host('hold'):
            config = json.loads(f.config_path.read_bytes())
            config['hosts']['dsh']['model_idle_watchdog_seconds'] = 1
            f.config_path.write_text(json.dumps(config))
            original = f._dispatch()
        self.assertEqual(original['outcome'], 'unknown')
        self.assertEqual(original['transport_diagnostic']['source'], 'original_model_watchdog')
        run = tasks.read_run(f.root, original['run_id'])
        phases = [e['payload']['phase'] for e in run['events'] if e['kind'] == 'capacity']
        self.assertEqual(phases, ['queued','admitted','released'])
        self.assertEqual(snapshot(f.root)['counts']['occupied']['total'], 0)
        self.assertEqual(run['native_identity']['turn_id'], '2')
        self.assertFalse(run['stop_requested'])

    @requires_dsh
    def test_restarted_recovery_host_reopens_a_dead_managers_native_session_once(self):
        import select
        import dsh_capacity as capacity
        from agent_base import child_environment
        from dsh_auto_resume import CLAIM, WATCH

        if not hasattr(select, 'kqueue'):
            self.skipTest('Kernel process-exit notifications are required for this installed-host test')
        f = self.f
        self.managed_offline_recovery()
        host = Path(tasks.__file__).with_name('dsh_recovery_host.py')
        harness = host.with_name('harness.py')
        marker = f.home/'original-request-persisted.txt'

        def start_host():
            log = (f.home/'isolated-supervisor.log').open('ab')
            self.addCleanup(log.close)
            proc = subprocess.Popen([sys.executable, str(host), '--root', str(f.root), '--serve'],
                                    env=child_environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            self.addCleanup(stop, proc)
            return proc

        def stop(proc):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=8)

        with self.installed_host('hold', flush_open_turn_request=str(marker)) as phase:
            supervisor = start_host()
            with (f.home/'isolated-manager.log').open('wb') as log:
                manager = subprocess.Popen([sys.executable, str(harness), '--root', str(f.root),
                    'work-dispatch', '--request', 'request', '--config', str(f.config_path), '--execute'],
                    env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                self.addCleanup(stop, manager)
                original = self.wait_native_turn(2)
                limit = time.monotonic()+7
                while not marker.exists() and time.monotonic()<limit:
                    time.sleep(.03)
                self.assertTrue(marker.exists())
                original = tasks.read_run(f.root, original['id'])
                self.assertEqual(len([e for e in original['events'] if e['kind'] == WATCH]), 1)
                self.assertEqual(capacity.snapshot(f.root)['counts']['occupied']['author'], 1)
                phase[0] = 'finish_recovery'
                stop(supervisor)
                stop(manager)
                stopped_at = time.time()
                replacement = start_host()
                limit = time.monotonic()+22
                while time.monotonic() < limit:
                    current = tasks.read_task(f.root, 'task')['runs']
                    if len(current)==2 and current[-1]['outcome']=='succeeded':
                        break
                    if replacement.poll() is not None or len(current)>2:
                        break
                    time.sleep(.1)
                self.assertEqual([r['outcome'] for r in current], ['unknown','succeeded'],
                                 (current, (f.home/'isolated-supervisor.log').read_text()))
                attempt = tasks.read_run(f.root, original['id'])
                claim, = [e for e in attempt['events'] if e['kind'] == CLAIM]
                self.assertEqual(claim['payload']['next_run_id'], current[-1]['id'])
                self.assertEqual(current[-1]['native_identity']['id'], original['native_identity']['id'])
                self.assertEqual(f.candidate.read_text(), 'resumed once')
                self.assertFalse(capacity._queue(f.root)['entries'])
                if directory := os.environ.get('HTAM_1014_EVIDENCE'):
                    source_released, = [e['created_at'] for e in attempt['events'] if e['kind'] == 'capacity'
                                        and e['payload']['phase'] == 'released']
                    child = tasks.read_run(f.root, current[-1]['id'])
                    final = fixtures.runtime.read_receipt(f.root, child['latest_receipt'])['result']
                    first_action = final['turn_activity']['first_effective_action_at']
                    released_at = datetime.fromisoformat(source_released).timestamp()
                    claim_at = datetime.fromisoformat(claim['created_at']).timestamp()
                    (Path(directory)/'native-recovery-timing.json').write_text(json.dumps({
                        'disconnect_to_release_seconds': round(released_at-stopped_at, 3),
                        'release_to_claim_seconds': round(claim_at-released_at, 3),
                        'claim_to_first_effective_action_seconds':
                            round(datetime.fromisoformat(first_action).timestamp()-claim_at, 3),
                        'same_session': True, 'automatic_successors': 1, 'original_effect_writes': 1}, indent=2)+'\n')

    @requires_dsh
    def test_recovery_collects_a_persisted_terminal_without_a_new_native_action(self):
        from agent_base import child_environment
        from dsh_auto_resume import CLAIM
        from dsh_recovery_host import _watched
        import select

        if not hasattr(select, 'kqueue'):
            self.skipTest('Kernel process-exit notifications are required for this installed-host test')
        f = self.f
        self.managed_offline_recovery()
        host = Path(tasks.__file__).with_name('dsh_recovery_host.py')
        marker = f.home/'native-final-before-management-receipt'
        manager_script = """
import sys, threading
from pathlib import Path
marker = Path(sys.argv.pop(1))
sys.path.insert(0, sys.argv.pop(1))
from agent_runtime import Runtime
from harness import main
original = Runtime._record_result
def interrupt(self, run_id, config, result, *args, **kwargs):
    if result['native_status'] == 'completed' and result.get('result'):
        marker.write_text(result['result'])
        threading.Event().wait()
    return original(self, run_id, config, result, *args, **kwargs)
Runtime._record_result = interrupt
raise SystemExit(main())
"""
        with self.installed_host('shell'):
            manager = subprocess.Popen([sys.executable,'-c',manager_script,str(marker),str(host.parent),
                '--root',str(f.root),'work-dispatch','--request','request','--config',str(f.config_path),'--execute'],
                env=dict(os.environ), stdin=subprocess.DEVNULL)
            try:
                limit = time.monotonic()+10
                while not marker.is_file() and manager.poll() is None and time.monotonic() < limit:
                    time.sleep(.03)
                self.assertTrue(marker.is_file())
                original, = tasks.read_task(f.root,'task')['runs']
                self.assertEqual(original['outcome'], 'running')
            finally:
                manager.kill()
                manager.wait(timeout=8)
            supervisor = subprocess.Popen([sys.executable,str(host),'--root',str(f.root),'--serve'],
                env=child_environment(), stdin=subprocess.DEVNULL)
            try:
                limit = time.monotonic()+12
                while _watched(f.root) and supervisor.poll() is None and time.monotonic() < limit:
                    time.sleep(.04)
                self.assertFalse(_watched(f.root))
                recovered, = tasks.read_task(f.root,'task')['runs']
                recovered = tasks.read_run(f.root, recovered['id'])
                self.assertEqual(recovered['outcome'], 'succeeded')
                self.assertEqual(recovered['native_identity'], original['native_identity'])
                self.assertFalse(any(e['kind'] == CLAIM for e in recovered['events']))
                result = fixtures.runtime.read_receipt(f.root,recovered['latest_receipt'])['result']
                self.assertEqual(result['result'], marker.read_text())
            finally:
                supervisor.kill()
                supervisor.wait(timeout=8)

    @requires_dsh
    def test_owner_grant_edits_the_exact_exhausted_native_goal_and_continues(self):
        f=self.f
        with self.installed_host('budget_exhaustion') as phase:
            seed=f._dispatch()
            self.assertEqual(seed['outcome'],'succeeded',seed)
            original=tasks.read_run(f.root,seed['run_id'])
            before=fixtures.runtime.read_receipt(f.root,original['latest_receipt'])
            goal=before['result']['native_session_observation']['native_goal']
            self.assertEqual((goal['phase'],goal['roundsStarted'],goal['maxGoalRounds']),('blocked',40,40))
            self.assertEqual(goal['blockedReason']['code'],'round-limit')
            note=f.home/'owner-investment.md'
            note.write_text('The original owner approves exactly sixteen additional rounds for the same Goal.')
            command=['work-dispatch','--request','request','--run-id',original['id'],'--config',str(f.config_path),
                     '--execute','--instructions',str(note),'--goal-max-rounds']
            def refused(value):
                result=subprocess.run([sys.executable,str(Path(tasks.__file__).with_name('harness.py')),
                    '--root',str(f.root),*command,value],capture_output=True,text=True)
                self.assertNotEqual(result.returncode,0,result.stdout)
                return json.loads(result.stderr)['error']
            phase[0]='budget_recovered'
            self.assertIn('increased total',refused('39'))
            result=self.cli(*command,'56')
            self.assertEqual(result['outcome'],'succeeded',result)
            resumed=tasks.read_run(f.root,result['run_id'])
            self.assertEqual(resumed['native_identity']['id'],original['native_identity']['id'])
            retained=fixtures.runtime.read_receipt(f.root,resumed['latest_receipt'])['result']
            evidence=retained['native_goal_budget']
            edited,armed=evidence['observations']
            self.assertEqual((edited['stage'],edited['goal']['revision'],edited['goal']['maxGoalRounds']),
                             ('edited',goal['revision']+1,56))
            self.assertEqual((armed['stage'],armed['goal']['revision'],armed['goal']['activation']),
                             ('resumed',goal['revision']+2,'armed'))
            end=retained['native_session_observation']['native_goal']
            self.assertEqual((end['id'],end['maxGoalRounds'],end['phase']),(goal['id'],56,'complete'))
            self.assertGreaterEqual(int(resumed['native_identity']['turn_id']),int(original['native_identity']['turn_id'])+2)
            self.assertEqual(f.candidate.read_text(),'Original work after the authorized increase.')
            self.assertIsNotNone(retained['turn_activity']['first_effective_action_at'])
            self.assertEqual(self.cli(*command,'56')['run_id'],resumed['id'])
            self.assertIn('different continuation',refused('55'))
            self.assertEqual(len(tasks.read_task(f.root,'task')['runs']),2)
            if destination:=os.environ.get('HTAM_DSH_1122_EVIDENCE'):
                (Path(destination)/'native-goal-budget.json').write_text(json.dumps({
                    'original_run':original['id'],'continued_run':resumed['id'],'original_turn':original['native_identity']['turn_id'],
                    'final_turn':resumed['native_identity']['turn_id'],'before':goal,'budget':evidence,'after':end,
                    'receipt_sha256':resumed['latest_receipt']['sha256'],'effective_action_at':retained['turn_activity']['first_effective_action_at'],
                    'same_session':True,'isolated':True},ensure_ascii=False,indent=2)+'\n')

    @requires_dsh
    def test_missing_native_session_is_not_replaced(self):
        with self.installed_host():
            config=fixtures.runtime.load_config(self.f.root,str(self.f.config_path),'executor')
            adapter=fixtures.dsh_headless.DeepSeekAdapter(self.f.root,config)
            previous=fixtures.runtime.identity('deepseek_harness','session','session-missing-original',self.f.root)
            previous['turn_id']='1'
            with self.assertRaises(fixtures.dsh_headless.NativeStartRejected) as caught:
                adapter.execute('Continue only the original Session.',lambda *args:None,lambda:None,previous=previous)
            self.assertEqual(caught.exception.observation['native_submission'],'not_started')
            self.assertIsNone(adapter.native)
            self.assertEqual(tasks.read_task(self.f.root,'task')['runs'],[])
