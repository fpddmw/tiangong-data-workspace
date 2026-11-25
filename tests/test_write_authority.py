"""Exact managed writes through the normal hosts, with a local receiver."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collaboration_mcp import Bridge
from devin_adapter import DevinAdapter
from dsh_headless import DeepSeekAdapter
from env_run import cli_write_directories
import delivery_evidence
import harness_tasks as tasks
from task_ledger import decisions
import test_agent_runtime as fixture
from review_fixtures import FLOW_ID, PROCESS_ID, VERSION, flow


HERE = Path(__file__).resolve().parent
ACTOR = '0037f465-3686-42a4-a419-b0ac5bec7c7e'
ENVIRONMENT = 'https://isolated.invalid'
PROJECT = 'isolated-project'


def flow_type(category, version=VERSION):
    body = flow(version)
    body['flowDataSet']['modellingAndValidation'] = {'LCIMethod':{'typeOfDataSet':category}}
    return body


class WriteAuthorityTests(unittest.TestCase):
    ref = fixture.RuntimeTests.ref
    endpoint = 'saved_draft'

    def create_request(self, **changes):
        return fixture.RuntimeTests.create_request(self,delivery_contract={
            'purpose':'Authorized isolated write receiver fixture','endpoint':self.endpoint,
            'acceptance':{'text':'Verify the fixture endpoint','refs':[self.evidence]}},**changes)

    def setUp(self):
        fixture.RuntimeTests.setUp(self)
        self.account_home=self.root/'account'
        self.journals=self.account_home/'.local/state/tiangong-lca/dataset-operations'
        self.journals.mkdir(parents=True)
        self.environment={'HOME':str(self.account_home),'TIANGONG_LCA_EXPECTED_USER_ID':ACTOR,
            'TIANGONG_LCA_EXPECTED_PROJECT_REF':PROJECT,'TIANGONG_LCA_API_BASE_URL':ENVIRONMENT}
        auth_env=patch.dict(os.environ, HOME=str(self.account_home)); auth_env.start(); self.addCleanup(auth_env.stop)
        for path in cli_write_directories(self.root):
            path.mkdir(parents=True,exist_ok=True)
        shutil.copytree(HERE.parent/'bin',self.root/'bin',ignore=shutil.ignore_patterns('__pycache__'))
        self.receiver=self.home/'receiver.py'
        self.receiver.write_text('''import json,sys,time
from pathlib import Path
argv = sys.argv[1:]
action = argv[2]
if action in ('create','save','delete','result-group') and '--dry-run' not in argv:
    path=argv[argv.index('--input')+1]
    value=json.loads(sys.stdin.buffer.read() if path=='-' else Path(path).read_bytes())
    with Path(__file__).with_name('sent.jsonl').open('a') as log:
        log.write(json.dumps({'action':action,'intent':value})+'\\n')
    if value.get('requestId')=='hold-first':
        time.sleep(.45)
print(json.dumps({'action':action,'outcome':'local_receiver','dryRun':'--dry-run' in argv}))
''')
        (self.root/'.control/runtime.json').write_text(json.dumps({'node':sys.executable,'cli':str(self.receiver)}))
        (self.root/'.control/accounts.json').write_text(json.dumps({'project_ref':PROJECT,'editor':{'user_id':ACTOR}}))
        envfile=self.root/'.env'
        envfile.write_text('DATA_EDITOR_SESSION_FILE=isolated-no-credentials\nTIANGONG_LCA_API_BASE_URL='+ENVIRONMENT+'\n')
        envfile.chmod(0o600)
        self.run_id='authority-run'
        self.run_home=self.home/'runs'/self.run_id
        tasks.mutate(self.root,'run-intent',self.actor,{'id':self.run_id,'request_id':'request',
            'config_ref':self.ref(self.config_path),'input_refs':[self.input_ref],
            'target_host':'codex','operation':'start'})
        self.lease=SimpleNamespace(path=self.run_home/'execution.json',value={'run_id':self.run_id,'request_id':'request'},
            tool_intent=lambda _:None,tool_spawned=lambda *_:None,tool_not_started=lambda _:None)

    def request(self, body, operation_id, *, expected=None):
        kind,ident,version=delivery_evidence.body_identity(body)
        raw={'id':ident,'version':version,'expectedActor':ACTOR,'expectedEnvironment':ENVIRONMENT,
             'requestId':operation_id,'content':body}
        if expected is not None:
            raw['expectedContent']=expected
        return raw

    def decision(self, ident, action, statement, *, kind='flow', target=FLOW_ID, version=VERSION, relates=(), account=ACTOR):
        document={'id':ident,'scope':{'task':'task','package':'request','object':f'{kind}/{target}/{version}',
            'action':action,'account':account,'project':PROJECT},
            'subject':decisions.DATASET_WRITE_GRANT if statement=='allow' else decisions.DATASET_WRITE,'statement':statement,
            'source_ref':{**self.evidence,'locator':'file','decided_by':self.actor['id']},
            'supersedes':[{'id':row} for row in relates]}
        return decisions.add(self.root,document)

    def arguments(self, raw, verb='create', kind='flow', *, dry_run=False):
        input_path=self.home/('input-'+hashlib.sha256(json.dumps(raw).encode()).hexdigest()+'.json')
        input_path.write_text(json.dumps(raw))
        return {'role':'editor','surface':'cli','argv':['dataset',kind,verb,'--input',str(input_path),'--json',
            *(['--dry-run'] if dry_run else [])]}

    def sent(self):
        path=self.home/'sent.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def public(self, raw, verb='create', kind='flow', *, dry_run=False):
        argv=self.arguments(raw,verb,kind,dry_run=dry_run)['argv']
        env={**os.environ,**delivery_evidence.context_environment(self.root,self.lease)}
        return subprocess.run([sys.executable,str(self.root/'bin/launch.py'),'--role','editor','cli','--',*argv],
                              env=env,capture_output=True,timeout=12)

    def test_exact_authorization_and_release_are_independent(self):
        basic=flow_type('Elementary flow')
        one=self.request(basic,'basic'); new_version=self.request(flow_type('Elementary flow','00.00.002'),'wrong-version')
        self.assertEqual(json.loads(self.public(one).stderr)['status'],'authorization_required')
        self.decision('wrong-account','create','allow',account='some-other-editor')
        self.decision('stop','create','stop')
        self.decision('release','create','release',relates=['stop'])
        self.assertEqual(json.loads(self.public(one).stderr)['status'],'authorization_required')
        self.decision('exact','create','allow')
        self.assertEqual(json.loads(self.public(new_version).stderr)['status'],'authorization_required')
        self.assertEqual(self.sent(),[])
        self.assertEqual(self.public(one).returncode,0)
        self.assertEqual([row['intent']['requestId'] for row in self.sent()],['basic'])
        self.decision('stop-again','create','stop',relates=['release'])
        self.assertEqual(json.loads(self.public(one).stderr)['status'],'stopped')
        self.assertEqual(len(self.sent()),1)

    def test_save_and_delete_check_both_exact_bodies(self):
        basic=flow_type('Elementary flow'); product=flow_type('Product flow')
        for before, after in ((product,basic),(basic,product),(basic,basic)):
            value=self.request(after,'class-or-name-change',expected=before)
            self.assertEqual(json.loads(self.public(value,'save').stderr)['status'],'authorization_required')
        raw=self.request(product,'delete',expected=product);raw.pop('content')
        self.assertEqual(json.loads(self.public(raw,'delete').stderr)['status'],'authorization_required')
        self.assertEqual(self.sent(),[])
        self.decision('delete-ok','delete','allow')
        self.assertEqual(self.public(raw,'delete').returncode,0)
        wrong=self.request(product,'wrong-compare',expected=flow_type('Product flow','00.00.002'))
        self.assertNotEqual(self.public(wrong,'save').returncode,0)
        self.assertEqual(len(self.sent()),1)

    def model_host(self, host):
        common={'role':'executor','profile':'native-workspace','_execution_lease':self.lease,
                'native_policy':{'writable_roots':[str(self.home),*map(str,cli_write_directories(self.root))],
                    'sandbox':'workspace-write','network_access':True},'native_cwd':str(self.root)}
        if host=='dsh':
            return DeepSeekAdapter(self.root,{**common,'host':{'binary':sys.executable,'node':sys.executable,
                'dsh_home':str(self.account_home)},'provider':{}})
        return DevinAdapter(self.root,{**common,'host':{'binary':sys.executable},'provider':{}})

    def test_hosts_share_qualified_tool_and_local_receiver(self):
        basic=self.request(flow_type('Elementary flow'),'protected')
        product=self.request(flow_type('Product flow'),'normal-product')
        waste=self.request(flow_type('Waste flow','00.00.002'),'normal-waste')
        dsh=self.model_host('dsh')
        devin=self.model_host('devin')
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as sockroot:
            bridge=Bridge(Path(sockroot)/'mcp.sock',devin.controlled)
            try:
                def use_dsh(value,**options):
                    result=dsh.controlled.dispatch('tiangong_cli',json.dumps(self.arguments(value,**options)))
                    self.assertTrue(result['accepted'],result)
                    return result['diagnostic']

                def use_devin(value,**options):
                    arguments=self.arguments(value,**options)
                    request={'method':'tools/call','params':{'name':'tiangong_cli','arguments':arguments}}
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(str(bridge.path))
                        client.sendall((json.dumps(request)+'\n').encode())
                        with client.makefile('rb') as reply:
                            result=json.loads(reply.readline())['result']
                    self.assertFalse(result['isError'],result)
                    return json.loads(result['content'][0]['text'])['diagnostic']

                for invoke, ordinary in ((use_dsh,product),(use_devin,waste)):
                    diagnostic=invoke(basic,dry_run=True)
                    self.assertEqual(diagnostic['exit_code'],0,diagnostic)
                    diagnostic=invoke(basic)
                    self.assertEqual(diagnostic['exit_code'],1,diagnostic)
                    expected='read_only_assignment' if self.endpoint=='evidence' else 'authorization_required'
                    self.assertIn(expected,diagnostic['stderr'])
                    diagnostic=invoke(ordinary)
                    self.assertEqual(diagnostic['exit_code'],int(self.endpoint=='evidence'),diagnostic)
                self.decision('grant','create','allow')
                diagnostic=use_devin(basic)
                self.assertEqual(diagnostic['exit_code'],int(self.endpoint=='evidence'),diagnostic)
            finally:
                bridge.close()
        expected=[] if self.endpoint=='evidence' else ['normal-product','normal-waste','protected']
        self.assertEqual([row['intent']['requestId'] for row in self.sent()],expected)

    def native_journal(self, raw, state, *, kind='flow'):
        native_key=hashlib.sha256(json.dumps(raw['requestId']).encode()).hexdigest()
        file=self.journals/(native_key+'.json')
        file.write_text(json.dumps({'schemaVersion':1,'binding':'native-fixture','action':'create','type':kind,
            'requestId':raw['requestId'],'id':raw['id'],'version':raw['version'],'actor':raw['expectedActor'],
            'environment':raw['expectedEnvironment'],'state':state,'contentDigest':'native-fixture','intent':raw,'response':None}))
        file.chmod(0o600)

    def test_unknown_blocks_only_exact_target_across_request_ids_until_terminal(self):
        product=flow_type('Product flow')
        first=self.request(product,'first'); second=self.request(product,'second'); other=self.request(product,'replacement')
        self.native_journal(first,'dispatched')
        self.native_journal(second,'unknown')
        reply=json.loads(self.public(other).stderr)
        self.assertEqual(reply['status'],'original_effect_unknown')
        self.assertEqual({a['request_id'] for a in reply['attempts']},{'first','second'})
        self.assertEqual(self.sent(),[])
        unrelated=self.request(flow_type('Waste flow','00.00.002'),'unrelated')
        self.assertEqual(self.public(unrelated).returncode,0)
        self.native_journal(first,'completed')
        self.assertEqual(json.loads(self.public(other).stderr)['attempts'][0]['request_id'],'second')
        self.native_journal(second,'rejected')
        after=self.request(product,'correction',expected=product)
        self.assertEqual(self.public(after,'save').returncode,0)
        self.assertEqual([row['intent']['requestId'] for row in self.sent()],['unrelated','correction'])

    def test_new_stop_after_input_snapshot_prevents_native_start(self):
        value=self.request(flow_type('Product flow'),'stop-between-input-and-dispatch')
        import production_review
        save_fixed=production_review._save_fixed
        def save_and_stop(root,path,raw):
            ref=save_fixed(root,path,raw)
            if Path(path).name=='dispatch.json':
                self.decision('before-dispatch','create','stop')
            return ref
        args=self.arguments(value)['argv']
        with patch.dict(os.environ,delivery_evidence.context_environment(self.root,self.lease)), \
             patch('production_review._save_fixed',side_effect=save_and_stop):
            self.assertEqual(delivery_evidence.execute_native(self.root,[sys.executable,str(self.receiver)],args,
                env=self.environment,cwd=self.root),1)
        self.assertEqual(self.sent(),[])

    def test_competing_target_is_serialized_and_unrelated_target_proceeds(self):
        origin=self.request(flow_type('Product flow'),'hold-first')
        other=self.request(flow_type('Product flow'),'competing-id')
        next_object=self.request(flow_type('Waste flow','00.00.002'),'independent')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(1) as executor:
            original=executor.submit(self.public,origin)
            deadline=time.monotonic()+3
            while not self.sent() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(self.sent()[0]['intent']['requestId'],'hold-first')
            refused=self.public(other)
            self.assertNotEqual(refused.returncode,0,refused.stdout)
            self.assertEqual(json.loads(refused.stderr)['reason'],'ExecutionBusy')
            self.assertEqual(self.public(next_object).returncode,0)
            self.assertEqual(original.result(timeout=3).returncode,0)
        self.assertEqual([row['intent']['requestId'] for row in self.sent()],['hold-first','independent'])

    def test_result_group_authorizes_compound_change_and_honors_member_stop(self):
        model_id='70bbfac4-b360-42f8-9d8c-e44177327c21'
        model=self.home/'model.json'; model.write_text('{}')
        baseline=self.home/'group-baseline.json'
        raw={'action':'save','modelId':model_id,'version':VERSION,'expectedActor':ACTOR,'expectedEnvironment':ENVIRONMENT,
             'baseline':baseline.name,'generation':{'source_model':{'json_ordered_path':model.name}}}
        view={key:raw[key] for key in ('modelId','version','expectedActor','expectedEnvironment')}
        baseline.write_text(json.dumps({**view,'snapshot':{'members':[{'id':PROCESS_ID,'version':VERSION}]}}))
        self.assertEqual(json.loads(self.public(raw,'result-group','model').stderr)['status'],'authorization_required')
        self.decision('group','result-group','allow',kind='model',target=model_id)
        self.decision('member-stop','delete','stop',kind='process',target=PROCESS_ID)
        self.assertEqual(json.loads(self.public(raw,'result-group','model').stderr)['status'],'stopped')
        self.assertEqual(self.sent(),[])
        self.decision('member-release','delete','release',kind='process',target=PROCESS_ID,relates=['member-stop'])
        self.assertEqual(self.public(raw,'result-group','model').returncode,0)
        value=self.sent()[0]['intent']
        self.assertEqual(value['generation']['source_model']['json_ordered_path'],str(model))
        self.assertEqual(json.loads(Path(value['baseline']).read_bytes()),json.loads(baseline.read_bytes()))
        fresh={**raw,'modelId':FLOW_ID,'baseline':None}
        self.assertEqual(self.public(fresh,'result-group','model').returncode,0)
        self.assertEqual(len(self.sent()),2)


class EvidenceAuthorityTests(unittest.TestCase):
    endpoint = 'evidence'
    setUp = WriteAuthorityTests.setUp
    ref = WriteAuthorityTests.ref
    def create_request(self):
        self.input_path = self.home/'investigation.md'
        self.input_path.write_text('Diagnose the input evidence and deliver the findings locally.\n')
        self.input_ref = self.ref(self.input_path)
        return WriteAuthorityTests.create_request(self)

    request = WriteAuthorityTests.request
    public = WriteAuthorityTests.public
    arguments = WriteAuthorityTests.arguments
    decision = WriteAuthorityTests.decision
    sent = WriteAuthorityTests.sent
    model_host = WriteAuthorityTests.model_host
    test_hosts_share_qualified_tool_and_local_receiver = WriteAuthorityTests.test_hosts_share_qualified_tool_and_local_receiver

    def test_evidence_delegation_denies_even_an_exact_grant_and_allows_dry_run(self):
        self.assertNotEqual(Path(self.input_ref['path']).suffix,'.json')
        value = self.request(flow_type('Elementary flow'),'evidence-only')
        self.decision('exact','create','allow')
        self.assertEqual(self.public(value,dry_run=True).returncode,0)
        self.assertEqual(json.loads(self.public(value).stderr)['status'],'read_only_assignment')
        self.assertEqual(self.sent(),[])
