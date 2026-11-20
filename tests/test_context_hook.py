"""Read-only native identity, deduplication and context-epoch recovery."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import harness_context_hook as hook
import test_harness_tasks as fixtures
import test_agent_runtime as runtime_fixtures
import harness_tasks as t

BIN=Path(__file__).resolve().parents[1]/'bin'

class ContextHookTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.TaskCoreTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
        self.root=self.f.root
    def event(self,name='SessionStart',source='startup',**extra):return {'hook_event_name':name,'source':source,'session_id':'main','cwd':str(self.root),**extra}
    def context(self,event,env=None):return hook.emit_context(self.root,event,environment={} if env is None else env)
    def test_each_lifecycle_edge_restores_and_fifty_reads_deduplicate(self):
        for source in ('startup','resume','clear','compact','compact'):
            output=self.context(self.event(source=source))
            self.assertIn('additionalContext',output['hookSpecificOutput'])
            self.assertLessEqual(len(output['hookSpecificOutput']['additionalContext'].encode()),8192)
            for _ in range(50):self.assertEqual(self.context(self.event('PostToolUse')),{} )
            self.assertEqual(self.context(self.event('UserPromptSubmit')),{} )
    def test_stdin_identity_with_and_without_environment_is_read_only(self):
        original=(self.root/'.control/harness.sqlite').read_bytes()
        for env in ({},{'CODEX_THREAD_ID':'main'}):
            self.assertIn('task=task',self.context(self.event(source='resume'),env)['hookSpecificOutput']['additionalContext'])
        self.assertEqual((self.root/'.control/harness.sqlite').read_bytes(),original)
    def test_conflicting_cross_workspace_and_unbound_child_cannot_borrow_parent(self):
        self.assertEqual(self.context(self.event(),{'CODEX_THREAD_ID':'other'}),{})
        self.assertEqual(self.context(self.event(cwd=str(self.root.parent))),{})
        self.assertEqual(self.context(self.event(agent_id='unknown-child')),{})
        nav=self.context(self.event(session_id='unbound-native'))['hookSpecificOutput']['additionalContext']
        self.assertIn('work-list',nav);self.assertNotIn('task=task',nav);self.assertNotIn('--mine',nav)
        for _ in range(4):self.context(self.event(),{'CODEX_THREAD_ID':'other'})
        files=list((self.root/'.control/hook-cache/diagnostics').rglob('*.json'))
        self.assertEqual(len(files),3)
    def test_db_lock_missing_store_and_bad_json_fail_open(self):
        db=t._connect(self.root)
        db.execute('PRAGMA journal_mode=DELETE');db.execute('BEGIN EXCLUSIVE')
        try:
            started=time.monotonic()
            p=subprocess.run([sys.executable,str(BIN/'harness_context_hook.py'),'--root',str(self.root)],input=json.dumps(self.event()),
                env={k:v for k,v in os.environ.items() if k!='CODEX_THREAD_ID'},text=True,capture_output=True,timeout=2)
            self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout),{})
            self.assertLess(time.monotonic()-started,2)
        finally:db.rollback();db.close()
        missing=self.root/'missing';missing.mkdir()
        output=hook.emit_context(missing,{'hook_event_name':'SessionStart','source':'startup','session_id':'main','cwd':str(missing)},environment={})
        self.assertEqual(output,{});self.assertFalse((missing/'.control/harness.sqlite').exists())
    def test_real_project_handlers_use_whole_output_and_only_failure_hook_observes_errors(self):
        config=json.loads((BIN.parent/'.codex/hooks.json').read_bytes())
        for name in ('SessionStart','UserPromptSubmit'):
            handler=config['hooks'][name][0]['hooks'][0]
            self.assertEqual(handler['additionalContextLimit'],0);self.assertEqual(handler['timeout'],2)
            self.assertIn('harness_context_hook.py',handler['command'])
        self.assertNotIn('PostCompact',config['hooks'])
        self.assertIn('harness_hook.py',config['hooks']['PostToolUse'][0]['hooks'][0]['command'])
    def test_non_object_and_malformed_stdin_fail_open_without_task_writes(self):
        original=(self.root/'.control/harness.sqlite').read_bytes()
        for source in ('[]','null','true','"literal"','{bad'):
            with self.subTest(source=source):
                result=subprocess.run([sys.executable,str(BIN/'harness_context_hook.py'),'--root',str(self.root)],
                    input=source,text=True,capture_output=True,timeout=2)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout),{})
                self.assertNotIn('Traceback',result.stderr)
        self.assertEqual((self.root/'.control/harness.sqlite').read_bytes(),original)
    def test_native_worker_role_comes_from_exact_observed_run_without_role_registration(self):
        f=runtime_fixtures.RuntimeTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        result=f.execute();native=t.read_run(f.root,result['run_id'])['native_identity']
        before=(f.root/'.control/harness.sqlite').read_bytes()
        event={'hook_event_name':'SessionStart','source':'startup','session_id':native['id'],'cwd':str(f.root)}
        value=hook.emit_context(f.root,event,environment={})
        self.assertIn('当前职责：executor',value['hookSpecificOutput']['additionalContext'])
        with t._connect(f.root,True) as c:self.assertEqual(t._roles(c,native),set())
        self.assertEqual((f.root/'.control/harness.sqlite').read_bytes(),before)
