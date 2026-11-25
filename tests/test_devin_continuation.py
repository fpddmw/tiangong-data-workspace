"""Public CLI recovery over a local synthetic ACP executable; no model or data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin'))
import agent_runtime as runtime
import execution_lease as execution
import harness_tasks as tasks
from python_executable_fixture import python_executable
import test_devin_runtime as fixture

REPO=Path(__file__).resolve().parents[1]


class DevinContinuationTests(unittest.TestCase):
    setUp=fixture.DevinRuntimeTests.setUp
    create_request=fixture.DevinRuntimeTests.create_request
    ref=fixture.DevinRuntimeTests.ref
    execute=fixture.DevinRuntimeTests.execute
    select_devin=fixture.DevinRuntimeTests.select_devin
    legacy_load_refusal=fixture.DevinRuntimeTests.legacy_load_refusal

    def prepare(self,delay=0,split_catalog=False):
        self.first=self.legacy_load_refusal()
        self.log=self.home/'synthetic-prompts.jsonl'
        native=self.home/'synthetic-acp'
        python_executable(native, '''
import json,os,sys,time
from pathlib import Path
level='high'
def options():
 models=[{'value':'swe-2-high'}]
 if SPLIT_CATALOG:models.append({'value':'swe-2-medium-lightning'})
 config=[{'id':'model','currentValue':'swe-2-high','options':models},
  {'id':'thought_level','currentValue':level,'options':[{'value':'high'},{'value':'max'}]}]
 if not SPLIT_CATALOG:config.append({'id':'speed','currentValue':'standard','options':[{'value':'standard'}]})
 return {'configOptions':config}
def emit(value):print(json.dumps({'jsonrpc':'2.0',**value}),flush=True)
for line in sys.stdin:
 r=json.loads(line);method=r.get('method');p=r.get('params',{})
 if not method:continue
 if method=='initialize':body={'protocolVersion':1,'agentCapabilities':{'loadSession':True}}
 elif method=='session/load':body=options()
 elif method=='session/set_config_option':level=p['value'] if p['configId']=='thought_level' else level;body=options()
 elif method=='session/set_mode':body={}
 elif method=='session/prompt':
  with Path(LOG).open('a') as f:f.write(json.dumps({'sessionId':p['sessionId'],'level':level,'pid':os.getpid()})+'\\n')
  time.sleep(DELAY)
  emit({'method':'session/update','params':{'sessionId':p['sessionId'],'update':{'sessionUpdate':'tool_call','toolCallId':'fixture-read','title':'Read original input','kind':'read','status':'completed'}}})
  emit({'method':'session/update','params':{'sessionId':p['sessionId'],'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Synthetic original Session recovery complete.'}}}})
  body={'stopReason':'end_turn'}
 else:raise AssertionError(method)
 emit({'id':r['id'],'result':body})
'''.replace('SPLIT_CATALOG',repr(split_catalog)).replace('LOG',repr(str(self.log))).replace('DELAY',repr(delay)))
        config=json.loads(self.config_path.read_bytes())
        config['hosts']['devin']['binary']=str(native)
        config['roles']['executor']['model']='swe-2-max'
        self.config_path.write_text(json.dumps(config))
        self.worker=runtime.Runtime(self.root)

    def command(self,*extra,continued=True,original='rejected'):
        return [sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),
            'work-dispatch','--request','request','--execute','--config',str(self.config_path),
            *(['--continue','--run-id',original] if continued else []),*extra]

    def cli(self,*extra,continued=True):
        return subprocess.run(self.command(*extra,continued=continued),cwd=self.root,env=os.environ.copy(),
            capture_output=True,text=True,timeout=30)

    def prompts(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_public_continue_cold_verifies_original_and_selects_same_session_max(self):
        self.prepare(split_catalog=True)
        old=tasks.read_run(self.root,'rejected')['latest_receipt']
        result=self.cli()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        new=tasks.read_run(self.root,json.loads(result.stdout)['run_id'])
        self.assertEqual(new['native_identity']['id'],'fixture-devin')
        self.assertEqual(self.prompts()[0]['level'],'max')
        self.assertEqual(len(self.prompts()),1)
        failed=tasks.read_run(self.root,'rejected')
        self.assertEqual(failed['outcome'],'failed')
        self.assertTrue(any(e.get('receipt')==old for e in failed['events']))
        self.assertEqual(self.cli(continued=False).returncode,0)
        self.assertEqual(len(self.prompts()),1)

    def test_concurrent_public_continues_only_send_one_prompt_after_refusal(self):
        self.prepare(delay=.3)
        processes=[subprocess.Popen(self.command(),cwd=self.root,env=os.environ.copy(),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        output=[p.communicate(timeout=30) for p in processes]
        self.assertTrue(any(p.returncode==0 for p in processes),output)
        self.assertEqual(len(self.prompts()),1,output)
        self.assertEqual(self.prompts()[0]['sessionId'],'fixture-devin')

    def test_public_continue_releases_reused_tool_pid_but_keeps_live_or_unreadable_tool(self):
        self.prepare()
        path=execution.metadata_path(self.home,'rejected')
        doc=json.loads(path.read_bytes())
        replacement=execution.process_identity(os.getpid())
        self.assertTrue(replacement['start'] and replacement['boot'],replacement)
        tracked={**replacement,'pgid':os.getpid()}
        for identity in (tracked,{**tracked,'start':None}):
            doc.update(phase='manager_released',tools={'old':{'phase':'spawned','process':identity}})
            execution.atomic_json(path,doc)
            result=self.cli()
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertEqual(self.prompts(),[])
        doc['tools']['old']['process']={**tracked,'start':'Thu Jan 01 00:00:00 1970'}
        execution.atomic_json(path,doc)
        result=self.cli()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual([p['sessionId'] for p in self.prompts()],['fixture-devin'])
        observed=json.loads(result.stdout)
        acp=path.parent.parent/observed['run_id']/'acp.jsonl'
        self.assertIn('fixture-read',acp.read_text())

    def test_stop_is_visible_after_read_and_only_explicit_owner_instruction_can_resume(self):
        self.prepare()
        tasks.mutate(self.root,'run-stop',self.actor,{'id':'rejected','summary':'Isolated explicit stop'})
        value=self.worker.readresult(run_id='rejected',config_path=None,role='executor',evidence_home=str(self.home))
        self.assertEqual(value['execution_state'],'not_started');self.assertTrue(value['stop_requested'])
        result=self.cli();self.assertNotEqual(result.returncode,0);self.assertEqual(self.prompts(),[])
        note=self.home/'owner-resume.txt';note.write_text('Explicitly resume this stopped isolated original Session. No data action.')
        args=self.command(continued=False)+['--run-id','rejected','--instructions',str(note),'--resume-stopped']
        resumed=subprocess.run(args,cwd=self.root,env=os.environ.copy(),capture_output=True,text=True,timeout=30)
        self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
        self.assertEqual([p['sessionId'] for p in self.prompts()],['fixture-devin'])
        self.assertTrue(tasks.read_run(self.root,'rejected')['stop_requested'])

    def test_live_or_unverifiable_original_execution_cannot_be_cold_reclassified(self):
        self.prepare()
        for state in ('active','unknown'):
            with patch('execution_lease.observe_run',return_value={'state':state,'reason':'Exact isolated counterexample'}):
                value=self.worker.readresult(run_id='rejected',config_path=None,role='executor',evidence_home=str(self.home))
            self.assertEqual(value['outcome'],'unknown')
        self.assertEqual(self.prompts(),[])

    def test_manager_crash_after_prompt_cannot_cause_a_second_dispatch(self):
        self.prepare(delay=2)
        manager=subprocess.Popen(self.command(),cwd=self.root,env=os.environ.copy(),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            deadline=time.monotonic()+10
            while not self.prompts() and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(len(self.prompts()),1)
            manager.kill();manager.communicate(timeout=5)
            # Native fake has already accepted the prompt. Its execution stays
            # tracked until natural exit; repeat dispatch only observes it.
            observed=self.cli(continued=False)
            self.assertEqual(len(self.prompts()),1,observed.stdout+observed.stderr)
            time.sleep(2.1)
            again=self.cli(continued=False)
            self.assertEqual(len(self.prompts()),1,again.stdout+again.stderr)
            self.assertIn('unknown',again.stdout+again.stderr)
        finally:
            if manager.poll() is None:manager.kill();manager.communicate(timeout=5)


if __name__=='__main__':unittest.main()
