"""Public commands + real worker transport with scripted NDJSON (no model or remote writes)."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
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
import harness_sessions as sessions
import harness_tasks as tasks
import harness_scope
import run_worker


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

    @contextmanager
    def installed_host(self, initial_phase='seed', **options):
        f = self.f
        node = '/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness'
        module = '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
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

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),
                         'Requires the installed Responses adapter')
    def test_responses_wire_distinguishes_calls_text_and_sse_failure(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        for scenario in ('structured', 'text', 'broken', 'effect_then_broken', 'automatic', 'read_only_then_broken',
                         'timeout_once', 'timeout_capped'):
            with self.subTest(scenario=scenario):
                self.f = fixtures.DeepSeekProgramTests('runTest')
                self.f.setUp()
                self.addCleanup(self.f.doCleanups)
                self._qualify_responses(scenario, BaseHTTPRequestHandler, ThreadingHTTPServer, Thread)

    def _qualify_responses(self, scenario, handler, server_type, thread_type):
        f = self.f
        requests = []
        original = Path(__file__).resolve().parents[1] / ('materials/packages/'
            'tiangong-package-gap-global-main-20261110/apple-scm-reuse-20261110/apple/'
            'material-dispatch-20261115/fe-return-20261117-1710.md')
        literal = original.read_text() if original.is_file() else '<</bash> printf I-AM-PLAIN-TEXT'
        package = run_worker.program_home(tasks.read_request(f.root, 'request'), root=f.root)
        effect = package / 'work' / 'wire-effect.txt'

        class Responses(handler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(data)
                step = len(requests)
                if scenario == 'timeout_once' and step == 1:
                    # No headers arrive for the first request; the next one succeeds.
                    time.sleep(1.5)
                    return
                reply = self.frames(step, data)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('x-request-id','offline-wire-'+str(step))
                if scenario == 'timeout_capped':
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(1.5)
                    return
                self.send_header('Content-Length', str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def frames(self, step, data):
                if (scenario == 'broken' or scenario == 'effect_then_broken' and step == 2
                        or scenario in ('automatic', 'read_only_then_broken') and step == 3):
                    return b'event: response.completed\ndata: {"type": SECRET_FIXTURE_INVALID_JSON\n\n'
                call = None
                if scenario in ('structured', 'automatic', 'read_only_then_broken') and step == 1:
                    call = ('create_goal', {'objective': 'Qualify real Responses tool calls', 'max_goal_rounds': 4})
                if (scenario,step) in (('structured',2),('automatic',6)):
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
                if scenario=='automatic' and step==5:
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
                events.append(('response.completed', {'response': {'id': 'resp_' + str(step), 'status': 'completed',
                    'output': [item], 'usage': usage}}))
                return ''.join('event: '+kind+'\ndata: '+json.dumps({'type': kind, **body})+'\n\n'
                               for kind, body in events).encode()

        # Each dispatch gets a new isolated task ledger and native home.
        with server_type(('127.0.0.1', 0), Responses) as server:
            thread = thread_type(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_port)
                if scenario in ('broken', 'automatic', 'read_only_then_broken'):
                    import dsh_capacity as capacity
                    import execution_lease as execution
                    queue=capacity._queue(f.root)
                    queue.update(policy=capacity.PILOT_POLICY,pilot_provider='htam-offline')
                    execution.atomic_json(capacity._paths(f.root)[0],queue)
                timeout_fixture = {'wire_idle_timeout_ms': 550, 'wire_retries': 5} if scenario.startswith('timeout_') else {}
                with self.installed_host('wire', wire=url+'/v1', wire_model='deepseek-fixture',
                                         network=scenario == 'read_only_then_broken', **timeout_fixture):
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
        initial_attempts = len(requests) if scenario != 'automatic' else 3
        self.assertEqual(len(timings), initial_attempts, timings)
        for index, attempt in enumerate(timings, 1):
            self.assertGreater(attempt['step'], 0)
            wire, = attempt['http']
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
        if 'broken' in scenario or scenario in ('automatic', 'timeout_capped'):
            self.assertEqual(outcome['outcome'], 'failed', receipt)
            self.assertEqual(observation['native_error']['code'],
                             'TIMEOUT' if scenario == 'timeout_capped' else 'PI_AI_ERROR')
        else:
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
            if scenario=='automatic':
                child,=[event for event in facts if event['kind']==CLAIM]
                resumed=tasks.read_run(f.root,child['payload']['next_run_id'])
                self.assertEqual(resumed['outcome'],'succeeded',resumed)
                self.assertEqual(resumed['native_identity']['id'],run['native_identity']['id'])
                answer=fixtures.runtime.read_receipt(f.root,resumed['latest_receipt'])['result']
                self.assertEqual(answer['native_session_observation']['native_goal']['id'],observation['native_goal']['id'])
                self.assertEqual(answer['native_goal_observation']['goal']['phase'],'complete')
                self.assertEqual(len(requests),7)
                self.assertIsNotNone(answer['turn_activity']['first_effective_action_at'])
            else:
                self.assertFalse(any(event['kind']==CLAIM for event in facts))
                self.assertEqual(receipt['turn_activity']['ending_turn_verified_progress'], [])
                if scenario == 'read_only_then_broken':
                    self.assertEqual(receipt['shell_results'][0]['exit_code'], 0)
                    self.assertEqual(observation['native_goal']['phase'], 'active')
        if directory := os.environ.get('HTAM_1010_EVIDENCE'):
            path = Path(directory) / ('fe-' + scenario + '.json')
            path.write_text(json.dumps({'outcome': outcome['outcome'], 'observed': observation,
                'tool_choice': requests[0].get('tool_choice'), 'tool_declaration': tool,
                'received_http_requests': len(requests), 'local_effect': effect.exists()}, ensure_ascii=False, indent=2))
        if directory := os.environ.get('HTAM_1014_EVIDENCE'):
            from importlib.util import module_from_spec, spec_from_file_location
            source=Path(__file__).resolve().parents[1]/('installation/maintenance-20261116-noon/continuation-handoff/'
                'implementation/dsh-workflow-20261119/review-benchmark.py')
            spec=spec_from_file_location('dsh_benchmark_receipt',source)
            benchmark=module_from_spec(spec);spec.loader.exec_module(benchmark)
            measured=benchmark.observation(f.root,run,{'case':scenario,'arm':'scripted','load_target':1})
            for key in ('receipt','opinion_refs'):measured.pop(key)
            measured.update(model_http_requests=len(requests),production_operations=0,isolated=True)
            (Path(directory)/('wire-'+scenario+'.json')).write_text(json.dumps(measured,ensure_ascii=False,indent=2)+'\n')

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),
                         'Requires the installed DSH native services; no model calls')
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

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),
                         'Requires installed DSH; no external model calls')
    def test_installed_a02_review_gets_exact_facts_and_original_sources(self):
        from a02_review_verifier import A02_PACKET, read_exact
        from harness_package import launch_spec
        from review_facts import collect
        workspace = Path(__file__).resolve().parents[1]
        if not (workspace/A02_PACKET).is_file() or not (workspace/'.control/harness.sqlite').is_file():
            self.skipTest('Pinned A02 originals and work ledger are required')
        review = tasks.read_request(workspace, Path(A02_PACKET).parent.name)
        packet = json.loads(read_exact(review['input_ref']))
        initial = collect(workspace, tasks.read_request(workspace, review['parent_request_id']), packet, review['created_at'])
        f = self.f
        originals = f.home/'a02-originals'
        originals.mkdir()
        selected = []
        for ref in packet['artifact_refs']:
            path = originals/Path(ref['id']).name
            path.write_bytes(read_exact(ref))
            selected.append(str(path))
        verifier = f.home/'a02_review_verifier.py'
        verifier.write_bytes(Path(__file__).with_name(verifier.name).read_bytes())
        with self.installed_host('seed', a02_artifacts=selected, a02_question=packet['review_scope'],
                                 a02_verifier=str(verifier), python=sys.executable) as phase:
            self.evidence_contract('task','request')
            seed = f._dispatch()
            author = tasks.read_run(f.root, seed['run_id'])
            archive = Path(launch_spec(f.root, author)['evidence_home'])/'runs'/author['id']/'delivery'
            observations = [process['readback'] for process in initial['processes']]
            observations.extend(initial['verified_flow_reads'].values())
            for observation in observations:
                ref = observation['record_ref']
                record = json.loads(read_exact(ref))
                target = archive/Path(ref['path']).parent.name
                target.mkdir(parents=True)
                for key in ('stdout_ref', 'output_ref', 'candidate_ref'):
                    if not (original := record.get(key)):
                        continue
                    saved = target/Path(original['path']).name
                    saved.write_bytes(read_exact(original))
                    record[key] = {**original, 'path':str(saved)}
                record.update(run_id=author['id'], request_id=author['request_id'])
                (target/'record.json').write_text(json.dumps(record, ensure_ascii=False))
            phase[0] = 'a02_review'
            note = f.home/'resume-a02.md'
            note.write_text('Resume the exact original isolated author with the frozen A02 originals.')
            outcome = f._dispatch(instructions=note)
        self.assertEqual(outcome['outcome'], 'succeeded', outcome)
        current = tasks.read_run(f.root, outcome['run_id'])
        self.assertEqual(current['native_identity']['id'], author['native_identity']['id'])
        run, = [r for r in tasks.read_task(f.root,'task')['runs'] if r['request_id']!='request']
        self.assertEqual(run['outcome'], 'succeeded', run)
        result = fixtures.runtime.read_receipt(f.root,run['latest_receipt'])['result']
        self.assertIn('A02-facts-verified', result['result'])
        self.assertEqual(result['turn_activity']['tool_calls'], 1)
        review_packet = json.loads(read_exact(tasks.read_request(f.root,run['request_id'])['input_ref']))
        frozen = json.loads(read_exact(review_packet['facts_ref']))
        self.assertEqual([p['readback']['status'] for p in frozen['processes']], ['complete_match']*2)
        self.assertEqual({ref['sha256'] for ref in review_packet['artifact_refs']},
                         {ref['sha256'] for ref in packet['artifact_refs']})
        self.assertIn(run['return_ids'][0],
                      fixtures.runtime.read_receipt(f.root,current['latest_receipt'])['result']['collected_review_return_ids'])

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),
                         'Requires installed DSH; no external model calls')
    def test_installed_focused_review_receives_original_and_changed_fields_then_lead_adopts(self):
        import hashlib
        f=self.f
        original_root=Path(__file__).resolve().parents[1]/('materials/packages/tiangong-package-gap-global-main-20261110/'
            'apple-scm-reuse-20261110/apple/material-dispatch-20261115/program-38e55011e83b6769/reviews')
        options={}
        for name,ident in (('original_candidate','5f241439b6f783dbf8db0306058fb7b0'),
                           ('revised_candidate','3a314bf5d32fb447a04bc298199c5f25')):
            packet=original_root/('program-review:'+ident)/'packet.json'
            if not packet.is_file():self.skipTest('Pinned SALTS packet is unavailable')
            rows=[]
            for artifact in json.loads(packet.read_bytes())['artifact_refs']:
                raw=Path(artifact['path']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(),artifact['sha256'])
                rows.append(json.loads(raw))
            path=f.home/(name+'.json');path.write_text(json.dumps({'rows':rows},ensure_ascii=False))
            options[name]=str(path)
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
            tasks.mutate(f.root,'grant-role',f.actor,{'id':'maintainer','actor':f.actor,
                'org_role':'maintainer','evidence_ref':f.evidence})
            from dsh_capacity_handoff import provider_pilot
            provider_pilot(f.root,f.evidence['path'],'htam-offline')
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

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),
                         'Requires installed DSH; no external model calls')
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
        self.assertEqual(request['summary'], brief.read_text())
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
        self.cli('work-revise', '--task', task['id'], '--note', str(current_plan))
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

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),'Installed DSH required')
    def test_native_pause_preserves_goal_without_an_extra_round(self):
        with self.installed_host('pause'):
            result=self.f._dispatch()
            self.assertEqual(result['outcome'],'succeeded',result)
            run=tasks.read_run(self.f.root,result['run_id'])
            observed=fixtures.runtime.read_receipt(self.f.root,run['latest_receipt'])['result']['native_goal_observation']['goal']
            self.assertEqual(observed['phase'],'paused')
            self.assertEqual(observed['roundsStarted'],0)
            self.assertEqual(run['native_identity']['turn_id'],'1')

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),'Installed DSH required')
    def test_native_shell_failure_is_observation_not_author_restart(self):
        with self.installed_host('shell',network=True):
            result=self.f._dispatch()
            self.assertEqual(result['outcome'],'succeeded',result)
            run=tasks.read_run(self.f.root,result['run_id'])
            observed=fixtures.runtime.read_receipt(self.f.root,run['latest_receipt'])['result']
            self.assertEqual([x['exit_code'] for x in observed['shell_results']],[7])
            self.assertEqual(len(tasks.read_task(self.f.root,'task')['runs']),1)
            self.assertIn('command failed',observed['result'])

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),'Installed DSH required')
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

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),'Installed DSH required')
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

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),'Installed DSH required')
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

    @unittest.skipUnless(Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar').is_file(),'Installed DSH required')
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
