"""Public workspace entry forwards native commands and selected identity unchanged."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
with patch.object(sys, 'path', [str(BIN), *sys.path]):
    SPEC = importlib.util.spec_from_file_location('public_launch', BIN / 'launch.py')
    launch = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(launch)


class PublicLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / '.control').mkdir()
        (self.root / 'work').mkdir()
        manifest = b'original immutable manifest'
        (self.root / 'manifest.json').write_bytes(manifest)
        self.spec = {'manifest':'manifest.json', 'manifest_sha256':hashlib.sha256(manifest).hexdigest(),
                     'node':'installed/node', 'cli':'installed/cli.js', 'ai_cli':'installed/ai.js', 'foundry_runtime_cli':'original/cli.js', 'cache':'cache', 'foundry_workspace':'work'}
        for key in ('node', 'cli', 'ai_cli', 'foundry_runtime_cli'):
            path=self.root/self.spec[key];path.parent.mkdir(parents=True,exist_ok=True);path.touch()
        (self.root / '.control/runtime.json').write_text(json.dumps(self.spec))
        (self.root / '.control/accounts.json').write_text(json.dumps({'project_ref':'project',
            'editor':{'user_id':'editor'},'manager':{'user_id':'manager'}}))
        env = self.root / '.env'
        env.write_text('DATA_EDITOR_SESSION_FILE=editor-session\nDATA_MANAGER_SESSION_FILE=manager-session\n')
        env.chmod(0o600)
        for name, value in [('ROOT',self.root)]:
            p=patch.object(launch,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(launch.subprocess,'call',return_value=0)
        self.execute=p.start();self.addCleanup(p.stop)
        p=patch.object(launch.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'native help',''))
        self.probe=p.start();self.addCleanup(p.stop)

    def invoke(self,*args,code=0):
        with patch.object(sys,'argv',['launch.py',*args]), self.assertRaises(SystemExit) as result:
            launch.main()
        self.assertEqual(result.exception.code,code)

    def test_native_grammar_and_role_environment_are_not_reinterpreted(self):
        commands=[['dataset','flow','get','--id','original'],
                  ['dataset','process','statistics','--state','20'],
                  ['dataset','flow','save','--input','/original/request'],
                  ['auth','identity-receipt','--expected-user-id','conflicting-native-expectation'],
                  ['future-native-command','--future-native-option','same value']]
        for role in ['editor','manager']:
            for command in commands:
                self.invoke('--role',role,'cli',*command)
                self.assertEqual(self.execute.call_args.args[0][2:],command)
                env=self.execute.call_args.kwargs['env']
                self.assertEqual(env['TIANGONG_LCA_EXPECTED_USER_ID'],role)
                self.assertEqual(env['TIANGONG_LCA_EXPECTED_PROJECT_REF'],'project')
                self.assertEqual(env['TIANGONG_LCA_SESSION_FILE'],role+'-session')
                self.assertEqual(self.execute.call_args.kwargs['cwd'],self.root/'work')
        self.probe.assert_not_called()

    def test_offline_role_needs_no_credentials_and_native_exit_is_preserved(self):
        (self.root/'.env').unlink();(self.root/'.control/accounts.json').unlink()
        self.execute.return_value=17
        self.invoke('--role','research','cli','dataset','process','validate','--input','/original/rows',code=17)
        self.assertNotIn('TIANGONG_LCA_SESSION_FILE',self.execute.call_args.kwargs['env'])
        self.assertNotIn('TIANGONG_LCA_EXPECTED_USER_ID',self.execute.call_args.kwargs['env'])
        self.probe.assert_not_called()

    def test_nested_env_cannot_replace_selected_identity(self):
        (self.root/'work/.env').write_text('unrelated')
        with patch.object(sys,'argv',['launch.py','cli','--help']), self.assertRaisesRegex(ValueError,'nested .env'):
            launch.main()
        self.execute.assert_not_called()

    def test_foundry_uses_original_manifest_and_unchanged_task_arguments(self):
        self.invoke('foundry','task','resume','--task','original','--actor','owner')
        argv=self.execute.call_args.args[0]
        self.assertEqual(argv[-6:],['task','resume','--task','original','--actor','owner'])
        self.assertIn(self.spec['manifest_sha256'],argv)
        (self.root/'manifest.json').write_text('changed')
        with patch.object(sys,'argv',['launch.py','foundry','doctor']), self.assertRaisesRegex(ValueError,'manifest changed'):
            launch.main()

    def test_removed_command_is_forwarded_once_without_private_fallback(self):
        self.execute.return_value=2
        self.invoke('cli','dataset','candidate','init','--input','/original/rows',code=2)
        self.assertEqual(self.execute.call_count,1)
        self.assertEqual(self.execute.call_args.args[0][1],str(self.root/'installed/cli.js'))
        self.probe.assert_not_called()

    def test_ai_and_foundry_have_distinct_executables(self):
        self.invoke('--role','research','ai','--version')
        self.assertEqual(self.execute.call_args.args[0][1],str(self.root/'installed/ai.js'))
        self.invoke('foundry','task','status','--task','original')
        self.assertEqual(self.execute.call_args.args[0][1],str(self.root/'original/cli.js'))

if __name__=='__main__': unittest.main()
