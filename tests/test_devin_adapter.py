"""Offline protocol/boundary tests. Live SWE-2 evidence is kept separately."""
import copy
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agent_base import CapabilityUnavailable, identity
from collaboration_mcp import Bridge
from collaboration_tools import ControlledTools
from devin_acp import ACPHost, ACPError, ClientTools, pre_prompt_rejection
from harness_hosts import NativeStartRejected
from devin_adapter import DevinAdapter


def configuration(root):
    return {'programmatic': True, 'role': 'executor', 'model': 'swe-2-high',
        'profile': 'native-workspace', 'provider': {'selection': 'existing-host-configuration'},
        'host': {'binary': 'offline-devin'}, 'diagnostic_directory': str(root / 'run/diagnostics'),
        'native_policy': {'sandbox': 'workspace-write', 'network_access': False, 'writable_roots': [str(root / 'candidate')]},
        'task_request_id': 'original', 'task_run_id': 'run'}


class DevinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'candidate').mkdir()
        self.config = configuration(self.root)

    def test_exact_standard_swe2_models_only(self):
        for model in ('swe-2-high', 'swe-2-medium', 'swe-2-max'):
            value = copy.deepcopy(self.config)
            value['model'] = model
            DevinAdapter.configure(self.root, value)
        for model in ('swe', 'swe-2', 'adaptive', 'swe-2-high-lightning', 'swe-2-flash-high'):
            value = copy.deepcopy(self.config)
            value['model'] = model
            with self.assertRaises(CapabilityUnavailable):
                DevinAdapter.configure(self.root, value)

    def test_resume_must_observe_exact_configured_model(self):
        adapter = DevinAdapter(self.root, self.config)
        self.assertEqual(adapter._verify_model({'configOptions': [{'id': 'model', 'currentValue': 'swe-2-high'}]}), 'swe-2-high')
        with self.assertRaises(CapabilityUnavailable):
            adapter._verify_model({'configOptions': [{'id': 'model', 'currentValue': 'adaptive'}]})

    def test_loaded_high_session_selects_exact_max_before_prompt(self):
        from unittest.mock import Mock
        self.config['model']='swe-2-max';adapter=DevinAdapter(self.root,self.config)
        option={'id':'model','currentValue':'swe-2-high','options':[{'value':v} for v in ('swe-2-high','swe-2-max')]}
        adapter.host=Mock();adapter.host.call.return_value={'configOptions':[{**option,'currentValue':'swe-2-max'}]}
        adapter._select_model({'configOptions':[option]},'original',lambda:None,None)
        adapter.host.call.assert_called_once_with('session/set_config_option',{'sessionId':'original','configId':'model','value':'swe-2-max'})
        self.assertEqual(adapter.verified_model,'swe-2-max');self.assertFalse(adapter.prompt_submitted)
        adapter.host.call.return_value={'configOptions':[option]}
        with self.assertRaises(CapabilityUnavailable):adapter._select_model({'configOptions':[option]},'original',lambda:None,None)

    def test_native_family_selector_thought_and_speed_prove_max_without_lightning(self):
        from unittest.mock import Mock
        self.config['model']='swe-2-max';adapter=DevinAdapter(self.root,self.config)
        options=[{'id':'model','currentValue':'swe-2-high','options':[{'value':'swe-2-high'}]},
            {'id':'thought_level','currentValue':'high','options':[{'value':v} for v in ('medium','high','max')]},
            {'id':'speed','currentValue':'standard','options':[{'value':'standard'},{'value':'fast'}]}]
        changed=copy.deepcopy(options);changed[1]['currentValue']='max'
        adapter.host=Mock();adapter.host.call.return_value={'configOptions':changed}
        adapter._select_model({'configOptions':options},'original',lambda:None,None)
        adapter.host.call.assert_called_once_with('session/set_config_option',{'sessionId':'original','configId':'thought_level','value':'max'})
        self.assertEqual(adapter.verified_model,'swe-2-max')
        changed[2]['currentValue']='fast'
        with self.assertRaises(CapabilityUnavailable):adapter._verify_model({'configOptions':changed})
        with self.assertRaises(CapabilityUnavailable):adapter._verify_model({'configOptions':changed[:2]})

    def test_only_explicit_native_response_before_prompt_is_start_rejection(self):
        native_error={'code':-12345,'message':'Unrelated startup refusal','data':{'retryable':False}}
        for received,prompt,expected in ((True,False,NativeStartRejected),(False,False,ACPError),(True,True,ACPError)):
            adapter=DevinAdapter(self.root,self.config);adapter.prompt_submitted=prompt
            error=ACPError('session/load',native_error,request_id=7,response_received=received)
            with patch.object(adapter,'_execute',side_effect=error),self.assertRaises(expected) as raised:
                adapter.execute('bounded',lambda *a:None,lambda:None,previous={'id':'original'})
            if expected is NativeStartRejected:
                self.assertEqual(raised.exception.observation['acp_request']['request_id'],7)
                self.assertEqual(raised.exception.observation['target_session'],'original')

    def test_pre_prompt_client_rpc_observed_before_callback_blocks_rejection_bridge(self):
        from types import SimpleNamespace
        adapter=DevinAdapter(self.root,self.config)
        adapter.host=SimpleNamespace(client_requests=[{'id':99,'method':'terminal/create'}])
        error=ACPError('session/load',{'code':-12345},request_id=7,response_received=True)
        with patch.object(adapter,'_execute',side_effect=error),self.assertRaises(ACPError):
            adapter.execute('bounded',lambda *a:None,lambda:None)

    def test_unavailable_exact_model_before_prompt_retains_ready_original_session(self):
        adapter=DevinAdapter(self.root,self.config)
        adapter.native=identity('devin_cli','session','original',self.root)
        with patch.object(adapter,'_execute',side_effect=CapabilityUnavailable('Exact Max not offered')),self.assertRaises(NativeStartRejected) as raised:
            adapter.execute('bounded',lambda *a:None,lambda:None)
        self.assertEqual(raised.exception.observation['phase'],'before_prompt')
        self.assertEqual(raised.exception.observation['target_session'],'original')
        self.assertEqual(raised.exception.ready_thread,adapter.native)

    def test_load_history_replay_does_not_become_a_current_tool_action(self):
        rows=[{'direction':'client','id':1,'method':'initialize'},
            {'direction':'agent','jsonrpc':'2.0','id':1,'result':{'protocolVersion':1}},
            {'direction':'client','id':2,'method':'session/load','params':{'sessionId':'original'}},
            {'direction':'agent','method':'session/update','params':{'update':{'sessionUpdate':'tool_call','toolCallId':'old-history'}}},
            {'direction':'agent','jsonrpc':'2.0','id':2,'error':{'code':-12345,'message':'Refused'}}]
        fact=pre_prompt_rejection(rows,session_id='original')
        self.assertEqual((fact['method'],fact['request_id'],fact['replayed_notifications']),('session/load',2,1))
        variants=[rows[:-1],rows+[{'direction':'client','id':3,'method':'session/prompt'}],
            rows+[{'direction':'agent','id':9,'method':'terminal/create'}],
            rows+[{'transport_error':'incomplete'}],rows+[rows[0]],
            [*rows[:-1],{**rows[-1],'id':99}]]
        for value in variants:self.assertIsNone(pre_prompt_rejection(value,session_id='original'))
        self.assertIsNone(pre_prompt_rejection(rows,session_id='other'))

    def test_acp_error_retains_original_method_id_and_native_response(self):
        script="import sys,json; r=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':-12345,'message':'refused'}}),flush=True);sys.stdin.read()"
        host=ACPHost([sys.executable,'-c',script],cwd=self.root,env=os.environ,on_event=lambda _:None,on_request=lambda *_:{},transcript=self.root/'protocol.jsonl')
        self.addCleanup(host.close)
        with self.assertRaises(ACPError) as raised:host.call('session/load',{'sessionId':'original'})
        self.assertEqual(raised.exception.observation['method'],'session/load')
        self.assertEqual(raised.exception.observation['request_id'],1)
        self.assertTrue(raised.exception.observation['response_received'])
        rows=[json.loads(line) for line in (self.root/'protocol.jsonl').read_text().splitlines()]
        self.assertEqual(rows[-1]['request_method'],'session/load')

    def client(self, write=True):
        policy = {**self.config['native_policy'], 'writable_roots': [str(self.root / 'candidate')] if write else []}
        client = ClientTools(self.root, policy, env=os.environ, directory=self.root / 'temporary')
        self.addCleanup(client.close)
        return client

    def test_read_write_and_symlink_boundary(self):
        client = self.client()
        target = self.root / 'candidate/result.txt'
        client.request('fs/write_text_file', {'path': str(target), 'content': 'complete evidence\nlimits retained\n'})
        self.assertEqual(client.request('fs/read_text_file', {'path': str(target)})['content'], target.read_text())
        self.assertEqual(client.request('fs/read_text_file', {'path': str(target), 'line': 2, 'limit': 1})['content'], 'limits retained\n')
        with self.assertRaises(ValueError):
            client.request('fs/write_text_file', {'path': str(self.root / 'outside.txt'), 'content': 'bad'})
        (self.root / 'candidate/link').symlink_to(self.root)
        with self.assertRaises(ValueError):
            client.request('fs/write_text_file', {'path': str(self.root / 'candidate/link/outside.txt'), 'content': 'bad'})
        (self.root / '.env').write_text('fixture, no credentials')
        with self.assertRaises(ValueError):
            client.request('fs/read_text_file', {'path': str(self.root / '.env')})
        with self.assertRaises(ValueError):
            self.client(False).request('fs/write_text_file', {'path': str(target), 'content': 'bad'})

    def shell(self, client, command):
        key = client.request('terminal/create', {'command': command, 'cwd': str(self.root)})['terminalId']
        client.request('terminal/wait_for_exit', {'terminalId': key})
        return client.request('terminal/output', {'terminalId': key})

    def test_os_sandbox_prevents_candidate_changes_for_reviewer(self):
        target = self.root / 'candidate/original.txt'
        target.write_text('original')
        result = self.shell(self.client(False), 'printf changed > ' + str(target))
        self.assertNotEqual(result['exitStatus']['exitCode'], 0)
        self.assertEqual(target.read_text(), 'original')

    def test_os_sandbox_allows_only_assigned_author_writes(self):
        client = self.client()
        target = self.root / 'candidate/allowed.txt'
        self.assertEqual(self.shell(client, 'printf allowed > ' + str(target))['exitStatus']['exitCode'], 0)
        self.assertEqual(target.read_text(), 'allowed')
        outside = self.root / 'outside.txt'
        self.assertNotEqual(self.shell(client, 'printf bad > ' + str(outside))['exitStatus']['exitCode'], 0)
        self.assertFalse(outside.exists())

    def test_os_sandbox_denies_private_reads_and_network(self):
        client = self.client()
        (self.root / '.env').write_text('fixture only')
        self.assertNotEqual(self.shell(client, 'cat ' + str(self.root / '.env'))['exitStatus']['exitCode'], 0)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            code = 'import socket; socket.create_connection(' + repr(listener.getsockname()) + ',timeout=1)'
            result = self.shell(client, shlex.join([sys.executable, '-c', code]))
            self.assertNotEqual(result['exitStatus']['exitCode'], 0)
            self.assertIn('PermissionError', result['output'])

    def test_output_limit_explicitly_reports_incomplete_read(self):
        client = self.client()
        key = client.request('terminal/create', {'command': 'printf abcdef', 'outputByteLimit': 3})['terminalId']
        client.request('terminal/wait_for_exit', {'terminalId': key})
        result = client.request('terminal/output', {'terminalId': key})
        self.assertTrue(result['truncated'])
        self.assertEqual(result['output'], 'abc')
        self.assertEqual(client.terminals[key][1].read_text(), 'abcdef')

    def test_closed_client_cannot_admit_late_writes_or_commands(self):
        client = self.client()
        client.close()
        target = self.root / 'candidate/late.txt'
        with self.assertRaisesRegex(ValueError, 'admission is closed'):
            client.request('terminal/create', {'command': 'printf bad > ' + str(target)})
        with self.assertRaisesRegex(ValueError, 'admission is closed'):
            client.request('fs/write_text_file', {'path': str(target), 'content': 'bad'})
        self.assertFalse(target.exists())

    def test_no_parent_or_provider_secret_environment_in_tools(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'parent', 'DEVIN_REFUSAL_FALLBACK': 'paid-model', 'FAKE_API_KEY': 'secret'}):
            adapter = DevinAdapter(self.root, self.config)
            self.assertNotIn('CODEX_THREAD_ID', adapter.env)
            self.assertNotIn('DEVIN_REFUSAL_FALLBACK', adapter.env)
            client = ClientTools(self.root, self.config['native_policy'], env=adapter.env, directory=self.root / 'temporary')
            self.assertNotIn('FAKE_API_KEY', client.env)
            self.assertNotIn('DEVIN_REFUSAL_FALLBACK', client.env)

    def test_terminal_spelling_never_bypasses_private_reads(self):
        client = self.client()
        (self.root / '.control').mkdir()
        (self.root / '.control/runtime.json').write_text('fixture only')
        (self.root / 'bin').mkdir()
        script = self.root / 'bin/launch.py'
        script.write_text('from pathlib import Path;print(Path('+repr(str(self.root/'.control/runtime.json'))+').read_text())')
        for command in (shlex.join([sys.executable, str(script)]),
                        'cd '+shlex.quote(str(self.root))+' && '+shlex.join([sys.executable,str(script)])):
            result=self.shell(client,command)
            self.assertNotEqual(result['exitStatus']['exitCode'],0)
            self.assertIn('Operation not permitted',result['output'])
        key=client.request('terminal/create',{'command':sys.executable,'args':[str(script)]})['terminalId']
        client.request('terminal/wait_for_exit',{'terminalId':key})
        self.assertNotEqual(client.request('terminal/output',{'terminalId':key})['exitStatus']['exitCode'],0)

    def test_acp_retains_exact_client_reply_and_delivery_phase(self):
        host=object.__new__(ACPHost);rows=[];sent=[]
        host.on_request=lambda method,args:{'terminalId':'original-terminal'}
        host.retain=rows.append;host.send=sent.append
        host._request({'id':'actual-request','method':'terminal/create','params':{}})
        self.assertEqual(rows[0]['result'],sent[0]['result'])
        self.assertTrue(rows[0]['response_write_completed'])
        self.assertEqual(rows[0]['request_method'],'terminal/create')
        def failed_send(reply):raise ACPError('terminal/create','closed')
        host.send=failed_send;host._request({'id':'actual-request-2','method':'terminal/create','params':{}})
        self.assertFalse(rows[-1]['response_write_completed'])

    def test_permission_never_writes_persistent_bypass(self):
        adapter = DevinAdapter(self.root, self.config)
        adapter.native = identity('devin_cli', 'session', 'actual-native', self.root)
        adapter.prompt_submitted=True
        adapter.tool_calls['tool'] = {'kind': 'execute'}
        params = {'sessionId': 'actual-native', 'toolCall': {'toolCallId': 'tool'}, 'options': [
            {'kind': 'allow_always', 'optionId': 'bypass'}, {'kind': 'allow_once', 'optionId': 'one'}]}
        result = adapter.request('session/request_permission', params)
        self.assertEqual(result['outcome']['optionId'], 'one')
        params['sessionId'] = 'different-session'
        with self.assertRaises(ValueError):
            adapter.request('session/request_permission', params)

    def test_retained_result_read_and_missing_response_never_start_host(self):
        adapter = DevinAdapter(self.root, self.config)
        native = identity('devin_cli', 'session', 'actual-native', self.root)
        result = adapter.readresult(native)
        self.assertEqual(result['native_status'], 'unknown')
        self.assertFalse(result['replay_allowed'])
        self.assertIsNone(adapter.process)
        adapter.directory.mkdir(parents=True)
        body = {'native_identity': native, 'native_status': 'completed', 'result': 'full original\nwith conditions'}
        (adapter.directory / 'acp-result.json').write_text(json.dumps(body))
        self.assertEqual(adapter.readresult(native), body)
        self.assertIsNone(adapter.process)

    def test_acp_disconnect_reports_unknown_without_resubmitting(self):
        script = "import sys,json; v=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':v['id'],'result':{'ready':True}}),flush=True); sys.stdin.readline()"
        host = ACPHost([sys.executable, '-c', script], cwd=self.root, env=os.environ,
            on_event=lambda _: None, on_request=lambda *_: {}, transcript=self.root / 'protocol.jsonl')
        self.addCleanup(host.close)
        self.assertEqual(host.call('initialize', {}), {'ready': True})
        with self.assertRaises(ACPError):
            host.call('session/prompt', {'sessionId': 'original'}, timeout=2)
        rows = [json.loads(line) for line in (self.root / 'protocol.jsonl').read_text().splitlines()]
        self.assertEqual(sum(row.get('direction') == 'client' and row.get('method') == 'session/prompt' for row in rows), 1)

    def test_native_exit_is_observed_while_descendant_keeps_stdout(self):
        from execution_lease import process_identity
        pid_path = self.root / 'descendant.pid'
        script = ("import sys,subprocess; sys.stdin.readline(); "
                  "p=subprocess.Popen(['/bin/sleep','3']); "
                  "open(sys.argv[1],'w').write(str(p.pid))")
        host = ACPHost([sys.executable, '-c', script, str(pid_path)], cwd=self.root, env=os.environ,
            on_event=lambda _: None, on_request=lambda *_: {})
        self.addCleanup(host.close)
        with self.assertRaises(ACPError):
            host.call('session/prompt', {'sessionId': 'original'}, timeout=2)
        self.assertTrue(process_identity(int(pid_path.read_text()))['exists'])

    def test_mcp_transport_calls_same_shared_review_implementation(self):
        calls = []
        class Review:
            def request_review(self, args):
                calls.append(args)
                return {'review_id': 'original-review', 'state': 'requested'}
        controlled = ControlledTools(self.root, self.root)
        controlled.review_service = Review()
        sock = self.root / 'tools.sock'
        bridge = Bridge(sock, controlled)
        self.addCleanup(bridge.close)
        def transport(message):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(sock))
                connection.sendall((json.dumps(message) + '\n').encode())
                return json.loads(connection.makefile('rb').readline())
        listing = transport({'method': 'tools/list'})['result']
        self.assertEqual({row['name'] for row in listing['tools']}, {'request_review', 'review_result', 'report_finding'})
        result = transport({'method': 'tools/call', 'params': {'name': 'request_review', 'arguments': {'artifacts': ['original.txt'], 'question': 'Exact use?'}}})
        envelope = json.loads(result['result']['content'][0]['text'])
        self.assertTrue(envelope['accepted'])
        self.assertEqual(envelope['diagnostic']['review_id'], 'original-review')
        self.assertEqual(calls, [{'artifacts': ['original.txt'], 'question': 'Exact use?'}])


if __name__ == '__main__':
    unittest.main()
