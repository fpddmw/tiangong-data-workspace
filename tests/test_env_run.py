import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
SPEC = importlib.util.spec_from_file_location('tested_env_run', BIN / 'env_run.py')
env_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(env_run)
with patch.object(sys, 'path', [str(BIN), *sys.path]):
    import agent_runtime
    import harness_hook
    from harness_hosts import installed_program
    import lca_gate
    from workspace_queries.discovery import capabilities


class SkillCliEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / '.control').mkdir(); (self.root / 'work').mkdir()
        self.spec = {'node': '.toolchain/node/bin/node',
                     'cli': '.toolchain/lca/node_modules/@tiangong-lca/cli/bin/tiangong-lca.js',
                     'ai_cli': '.toolchain/ai/node_modules/@tiangong-ai/cli/bin/tiangong-ai.js'}
        for value in self.spec.values():
            path = self.root / value; path.parent.mkdir(parents=True, exist_ok=True); path.touch()
        (self.root / '.control/runtime.json').write_text(json.dumps(self.spec))
        (self.root / '.control/accounts.json').write_text(json.dumps({'project_ref':'fixture', 'editor':{'user_id':'editor'}}))
        self.script = self.root / '.agents/skills/process-automated-builder/scripts/run.mjs'
        self.script.parent.mkdir(parents=True); self.script.touch()
        self.env = {'PATH':'/usr/bin:/bin', 'HOME':str(self.root)}
        self.argv = ['node',str(self.script),'--help']

    def bind(self, argv=None):
        return env_run.skill_cli_environment(self.root, argv or self.argv, self.env)

    def test_direct_and_wrapped_calls_use_same_installed_cli(self):
        argv, env, cwd = self.bind()
        self.assertEqual(argv[0], str(self.root/self.spec['node']))
        self.assertEqual(env['TIANGONG_LCA_CLI_COMMAND'], str(self.root/self.spec['cli']))
        self.assertEqual(env['TIANGONG_AI_CLI_BIN'], str(self.root/self.spec['ai_cli']))
        self.assertEqual(cwd, self.root/'work')
        self.assertFalse(any('EXPECTATION' in key for key in env))
        for name in ('tiangong-lca','tiangong-ai'):
            _, direct, directory = self.bind([name,'--help'])
            self.assertEqual(direct, env); self.assertEqual(directory, cwd)
        self.assertNotIn('TIANGONG_LCA_CLI_COMMAND', self.env)

    def test_ai_shell_script_is_resolved_before_changing_cwd(self):
        script=self.root/'.agents/skills/tiangong-kb-sci-search/scripts/sci_search.sh'
        script.parent.mkdir(parents=True);script.touch()
        argv,env,cwd=self.bind(['bash',str(script.relative_to(self.root)),'{"query":"test","dry_run":true}'])
        self.assertEqual(argv[:2],['bash',str(script)])
        self.assertIn('TIANGONG_AI_CLI_BIN',env)
        self.assertEqual(cwd,self.root/'work')

    def test_original_foundry_and_unrelated_commands_are_not_rebound(self):
        for argv in [['node','.agents/skills/foundry-tidas-import/scripts/run-foundry.mjs','task','resume'],
                     ['python3','original-task.py'],['node','--eval','1']]:
            self.assertEqual(self.bind(argv),(argv,self.env,self.root))

    def test_external_symlink_does_not_receive_skill_environment(self):
        outside=self.root/'outside.mjs';outside.touch();self.script.unlink();self.script.symlink_to(outside)
        self.assertEqual(self.bind(),(self.argv,self.env,self.root))

    def test_dotenv_autoload_cannot_expose_other_roles(self):
        (self.root/'.env').write_text('CONN=private\n')
        probe=self.root/'probe.py';probe.write_text("from pathlib import Path\nprint((Path.cwd()/'.env').exists())\n")
        _,env,cwd=self.bind()
        self.assertEqual(subprocess.check_output([sys.executable,str(probe)],env=env,cwd=cwd,text=True).strip(),'False')
        (self.root/'work/.env').symlink_to(self.root/'missing.env')
        with self.assertRaisesRegex(ValueError,'nested .env'):self.bind()

    def test_only_selected_tool_must_be_installed(self):
        self.spec['node']=sys.executable
        (self.root/'.control/runtime.json').write_text(json.dumps(self.spec))
        for selected,missing,name in [('cli','ai_cli','tiangong-lca'),('ai_cli','cli','tiangong-ai')]:
            (self.root/self.spec[selected]).write_text("print('selected tool executed')\n")
            (self.root/self.spec[missing]).unlink(missing_ok=True)
            argv,env,cwd=self.bind([name,'--help'])
            self.assertEqual(subprocess.check_output(argv,env=env,cwd=cwd,text=True).strip(),'selected tool executed')

    def test_managed_cli_and_hook_use_preferred_binding_then_legacy(self):
        shutil.copytree(BIN, self.root / 'bin', ignore=shutil.ignore_patterns('__pycache__'))
        control = self.root / '.control'
        local = control / 'runtime.local.json'
        legacy = control / 'runtime.json'
        bindings = (local, legacy)
        for path in bindings:
            fake_cli = self.root / (path.stem + '-cli.py')
            fake_cli.write_text('import json, sys\nfrom pathlib import Path\nprint(json.dumps([Path(__file__).stem, *sys.argv[1:]]))\n')
            worker = control / (path.stem + '-worker.json')
            worker.write_text('{}')
            path.write_text(json.dumps({
                'node': sys.executable,
                'cli': fake_cli.name,
                'programmatic_worker': {'path': str(worker.relative_to(self.root))},
            }))

        # First reproduce a clean machine, then add the previous binding and remove the preferred one.
        previous = legacy.read_bytes()
        legacy.unlink()
        cases = ('local_only', 'both', 'legacy_only')
        for case in cases:
            with self.subTest(case=case):
                if case == 'both':
                    legacy.write_bytes(previous)
                if case == 'legacy_only':
                    local.unlink()
                selected = legacy if case == 'legacy_only' else local
                env = {**self.env, 'HTAM_DELIVERY_ROOT': str(self.root), 'HTAM_DELIVERY_RUN_ID': 'fixture-run'}
                _, tool_env, cwd = env_run.skill_cli_environment(self.root, self.argv, env)
                self.assertEqual(tool_env['TIANGONG_LCA_CLI_COMMAND'], str(BIN / 'native_cli_capture.py'))
                delegate = self.root / 'bin/native_cli_capture.py'
                result = subprocess.check_output(
                    [sys.executable, str(delegate), '--version', selected.stem],
                    env=tool_env, cwd=cwd, text=True, timeout=10)
                self.assertEqual(json.loads(result), [selected.stem + '-cli', '--version', selected.stem])
                self.assertEqual(harness_hook.runtime_binding(self.root, {})['config_sha256'],
                                 hashlib.sha256(selected.read_bytes()).hexdigest())
                self.assertEqual(installed_program(self.root)['path'],
                                 str(control / (selected.stem + '-worker.json')))
                native_cli = next(row for row in capabilities(self.root, manifest={}, limit=20)['items']
                                  if row['id'] == 'native-cli')
                self.assertEqual(native_cli['runtime_binding'], str(selected.relative_to(self.root)))

    def test_invalid_preferred_binding_does_not_silently_use_legacy(self):
        (self.root / '.control/runtime.local.json').write_text('{')
        with self.assertRaises(json.JSONDecodeError):
            self.bind(['tiangong-lca', '--version'])
        with self.assertRaises(json.JSONDecodeError):
            installed_program(self.root)

    def test_cached_doctor_detects_change_from_legacy_to_local(self):
        receipt = self.root / lca_gate.DOCTOR_RECEIPT
        receipt.parent.mkdir(parents=True)
        old = self.root / '.control/runtime.json'
        receipt.write_text(json.dumps({
            'schema': lca_gate.DOCTOR_SCHEMA,
            'candidate_entry_sha256': lca_gate.fingerprint([None, None]),
            'runtime': {'config_sha256': hashlib.sha256(old.read_bytes()).hexdigest()},
        }))
        self.assertEqual(lca_gate.local_doctor(self.root, refresh=False)['cache_status'], 'last_observed')
        (self.root / '.control/runtime.local.json').write_text(json.dumps({'cli': 'new-cli'}))
        self.assertEqual(lca_gate.local_doctor(self.root, refresh=False)['cache_status'], 'binding_changed')

    def test_copied_worker_template_loads_both_roles_without_enabling_host(self):
        control = self.root / '.control'
        worker = control / 'programmatic-worker.local.json'
        template = BIN.parent / '.control/programmatic-worker.template.json'
        shutil.copyfile(template, worker)
        local = self.spec | {'programmatic_worker': {'path': str(worker.relative_to(self.root))}}
        (control / 'runtime.local.json').write_text(json.dumps(local))
        path = installed_program(self.root)['path']
        for role in ('executor', 'scientific_reviewer'):
            with self.subTest(role=role), patch.object(sys, 'path', [str(BIN), *sys.path]):
                config = agent_runtime.load_config(self.root, path, role)
                self.assertTrue(config['programmatic'])
                self.assertEqual(config['profile'], 'read-only-evidence')
                with self.assertRaisesRegex(agent_runtime.CapabilityUnavailable, 'disabled'):
                    agent_runtime.adapter_for(self.root, config)

    def test_credential_filter_preserves_only_selected_role(self):
        values={'DATA_EDITOR_SESSION_FILE':'editor-session','DATA_MANAGER_SESSION_FILE':'manager-session',
                'TIANGONG_SCI_APIKEY':'sci-private','TIANGONG_REPORT_APIKEY':'report-private','CONN':'db-private'}
        with patch.dict(os.environ,{'PATH':'/bin','HOME':str(self.root),'AWS_SECRET_ACCESS_KEY':'inherited','TIANGONG_AI_CLI':'foreign'},clear=True):
            research=env_run.role_env('research',values,self.root)
            editor=env_run.role_env('editor',values,self.root)
        self.assertEqual(research['TIANGONG_REPORT_APIKEY'],'report-private')
        self.assertNotIn('TIANGONG_LCA_SESSION_FILE',research)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',research)
        self.assertNotIn('TIANGONG_AI_CLI',research)
        self.assertEqual(editor['TIANGONG_LCA_SESSION_FILE'],'editor-session')
        self.assertNotIn('TIANGONG_SCI_APIKEY',editor)
        self.assertNotIn('CONN',editor)

    def test_offline_main_uses_isolated_home_and_no_credentials(self):
        with patch.object(env_run,'ROOT',self.root), patch.object(sys,'argv',['env_run.py','research','--without-project-env','--',*self.argv]), patch.object(env_run.subprocess,'call',return_value=0) as execute:
            with self.assertRaises(SystemExit) as result:env_run.main()
        self.assertEqual(result.exception.code,0)
        self.assertNotEqual(execute.call_args.kwargs['env']['HOME'],os.environ.get('HOME'))
        self.assertNotIn('TIANGONG_LCA_SESSION_FILE',execute.call_args.kwargs['env'])

if __name__=='__main__': unittest.main()
