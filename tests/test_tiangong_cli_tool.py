"""Real argv/process/archiving boundaries, without remote writes or models."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_base import StopRequested
from collaboration_tools import ControlledTools
from collaboration_mcp import Bridge
import execution_lease as execution

REPO=Path(__file__).resolve().parents[1]


class TiangongCliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'原目录 with spaces';self.root.mkdir()
        for name in ('bin','.venv/bin','work','nested folder'): (self.root/name).mkdir(parents=True)
        (self.root/'.venv/bin/python').symlink_to(sys.executable)
        self.native=self.root/'bin/native.py'
        self.native.write_text('''import json,os,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[1];args=sys.argv[1:]
with (root/'calls').open('a') as out:out.write('once\\n')
if '--hold' in args:
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
 (root/'child.pid').write_text(str(child.pid))
 time.sleep(30)
doc={'argv':args,'cwd':str(Path.cwd()),'run_id':os.environ.get('HTAM_DELIVERY_RUN_ID')}
if '--output' in args:Path(args[args.index('--output')+1]).write_text(json.dumps(doc,ensure_ascii=False))
if '--large' in args:print('大'*150000)
else:print(json.dumps(doc,ensure_ascii=False))
print('original stderr',file=sys.stderr)
if '--fail' in args:raise SystemExit(7)
''')
        (self.root/'bin/launch.py').write_text(
            'import os,sys\nfrom pathlib import Path\n'
            'sys.path.insert(0,'+repr(str(REPO/'bin'))+')\n'
            'from delivery_evidence import execute_native\n'
            'root=Path(__file__).resolve().parents[1]\n'
            'assert sys.argv[1:2]==["--role"] and sys.argv[4]=="--"\n'
            'raise SystemExit(execute_native(root,[sys.executable,str(root/"bin/native.py")],sys.argv[5:],env=os.environ.copy(),cwd=root/"work"))\n')
        self.home=self.root/'work/original/runs/original-run';self.home.mkdir(parents=True)
        self.started=[];self.not_started=[]
        self.lease=SimpleNamespace(path=self.home/'execution.json',value={'run_id':'original-run','request_id':'request'},
            tool_intent=lambda key:None,tool_not_started=self.not_started.append,
            tool_spawned=lambda key,process:self.started.append(execution.process_identity(process.pid)))

    def tool(self,cwd=None,available=True):
        return ControlledTools(self.root,cwd or self.root,cli_available=available,execution_lease=self.lease,
                               write_roots=(self.root,))

    def call(self,args,**kwargs):
        return self.tool().dispatch('tiangong_cli',json.dumps(args),**kwargs)

    def test_root_and_nested_use_one_launcher_with_literal_argv_and_unicode_output(self):
        marker=self.root/'must-not-exist'
        for cwd in (self.root,self.root/'nested folder'):
            output=self.root/('输出 '+cwd.name+'.json')
            args={'role':'editor','surface':'cli','argv':['--version',';touch '+str(marker),'$(touch '+str(marker)+')','--output',str(output)]}
            value=self.tool(cwd).dispatch('tiangong_cli',json.dumps(args))
            self.assertTrue(value['accepted']);d=value['diagnostic'];self.assertEqual(d['exit_code'],0,d)
            saved=json.loads(output.read_text());self.assertEqual(saved['argv'],args['argv'])
            self.assertEqual(saved['cwd'],str(self.root/'work'));self.assertEqual(saved['run_id'],'original-run')
            self.assertFalse(marker.exists())
        self.assertEqual((self.root/'calls').read_text(),'once\nonce\n')

    def test_full_outputs_nonzero_and_native_archive_are_separate_from_tool_acceptance(self):
        output=self.root/'canonical 证据.json';source=self.root/'input.json';source.write_text('{"fixture":true}')
        args={'role':'editor','surface':'cli','argv':['dataset','process','validate','--input',str(source),'--output',str(output),'--large','--fail']}
        value=self.call(args);self.assertTrue(value['accepted']);d=value['diagnostic']
        self.assertEqual(d['exit_code'],7);self.assertTrue(d['stdout_truncated'])
        self.assertGreater(d['stdout_bytes'],400000)
        self.assertEqual(Path(d['stdout_ref']['path']).read_text(),'大'*150000+'\n')
        record=list((self.home/'delivery').glob('operation-*/record.json'));self.assertEqual(len(record),1)
        native=json.loads(record[0].read_text());self.assertEqual(native['exit_code'],7)
        self.assertEqual(Path(native['input_ref']['path']).read_bytes(),source.read_bytes())
        self.assertEqual((self.root/'calls').read_text(),'once\n')

    def test_only_editor_result_group_can_create_its_local_recovery_file(self):
        fake_home = Path(self.temp.name).resolve() / 'account'
        state = fake_home / '.local/state/tiangong-lca'
        state.mkdir(parents=True)
        journal = state / 'model-result-groups'
        self.native.write_text("""import os,sys
from pathlib import Path
journal = Path.home()/'.local/state/tiangong-lca/model-result-groups'
journal.mkdir(exist_ok=True)
output = journal/'operation.json'
output.write_text('recovery')
assert output.read_text() == 'recovery'
""")
        args = ['dataset','model','result-group']
        with patch.dict(os.environ, {'HOME': str(fake_home)}):
            for role, command in [('research',args),('editor',['dataset','model','get'])]:
                diagnostic = self.call({'role':role,'surface':'cli','argv':command})['diagnostic']
                self.assertNotEqual(diagnostic['exit_code'], 0)
                self.assertFalse(journal.exists())
            diagnostic = self.call({'role':'editor','surface':'cli','argv':args})['diagnostic']
            self.assertEqual(diagnostic['exit_code'],0,diagnostic['stderr'])
            self.assertEqual((journal/'operation.json').read_text(),'recovery')

    def test_roles_and_overrides_are_rejected_before_process_creation(self):
        valid={'role':'editor','surface':'cli','argv':['--version']}
        for args in ({**valid,'role':'manager'},{**valid,'surface':'ai'},
                     {**valid,'env':{'TOKEN':'bad'}},{**valid,'cwd':'/'},{**valid,'argv':'--version'},
                     {**valid,'argv':['bad\0arg']},{'argv':['--version']}):
            self.assertFalse(self.call(args)['accepted'],args)
        self.assertFalse((self.root/'calls').exists())
        for surface in ('cli','ai'):
            self.assertEqual(self.call({**valid,'role':'research','surface':surface})['diagnostic']['exit_code'],0)

    def test_readonly_profile_has_no_online_tool_even_when_called_directly(self):
        tools=self.tool(available=False)
        self.assertNotIn('tiangong_cli',tools.offered())
        self.assertFalse(tools.dispatch('tiangong_cli',json.dumps({'role':'editor','surface':'cli','argv':['--version']}))['accepted'])
        self.assertFalse((self.root/'calls').exists())

    def test_cancel_retains_original_tool_process_and_unknown_effect_without_replay(self):
        with self.assertRaises(StopRequested):
            self.call({'role':'editor','surface':'cli','argv':['dataset','process','validate','--hold']},
                      cancelled=lambda:(self.root/'child.pid').exists())
        self.assertEqual(len(self.started),1);self.assertEqual(self.not_started,[])
        self.assertFalse(execution.process_identity(self.started[0]['pid'])['exists'])
        record=json.loads(next((self.home/'tool-output').glob('tiangong-cli-*/record.json')).read_text())
        self.assertEqual(record['phase'],'interrupted');self.assertIn('unknown',record['data_effect'])
        self.assertEqual((self.root/'calls').read_text(),'once\n')

    def test_cli_output_cannot_enlarge_the_original_write_grant(self):
        private=self.root/'.control';private.mkdir();target=private/'runtime.json';target.write_text('original metadata')
        tools=ControlledTools(self.root,self.root,cli_available=True,execution_lease=self.lease,
                              write_roots=(self.root/'work',))
        result=tools.dispatch('tiangong_cli',json.dumps({'role':'editor','surface':'cli','argv':['--output',str(target)]}))
        self.assertTrue(result['accepted']);self.assertNotEqual(result['diagnostic']['exit_code'],0)
        self.assertEqual(target.read_text(),'original metadata')

    def test_mcp_uses_the_same_declaration_and_execution(self):
        bridge=Bridge(self.root/'bridge.sock',self.tool());self.addCleanup(bridge.close)
        def request(message):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(bridge.path));connection.sendall((json.dumps(message)+'\n').encode())
                with connection.makefile('rb') as reader:return json.loads(reader.readline())
        listed=request({'method':'tools/list'})['result']['tools'];self.assertEqual(listed[0]['name'],'tiangong_cli')
        result=request({'method':'tools/call','params':{'name':'tiangong_cli','arguments':{'role':'research','surface':'cli','argv':['--version']}}})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(json.loads(result['result']['content'][0]['text'])['diagnostic']['exit_code'],0)


if __name__=='__main__':unittest.main()
