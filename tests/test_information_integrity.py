"""Complete source handling precedes previews and error classification."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import safe_diagnostics as safe
import execution_lease as lease
from claude_adapter import ClaudeAdapter
from codex_host import CodexHost

ROOT=Path(__file__).resolve().parents[1]

class DiagnosticTests(unittest.TestCase):
    def test_shared_redaction_fixtures_and_tail_cause(self):
        rows=json.loads((ROOT/'tests/fixtures/safe-diagnostics.json').read_bytes())
        for row in rows:
            with self.subTest(row=row):self.assertEqual(safe.safe_text(row['input']),row['expected'])
        tail='安全解释 '*250+'authorization denied; 最后原因：project mismatch'
        self.assertEqual(safe.safe_text(tail),tail)
        with tempfile.TemporaryDirectory() as home:
            ref=safe.retain({'code':'E','message':tail,'cause':{'password':'pretend','message':'root cause'}},directory=home)
            raw=Path(ref['diagnostic_ref']['path']).read_text()
            self.assertIn('最后原因',raw);self.assertNotIn('pretend',raw)
            self.assertEqual(safe.retain({'code':'E','message':tail,'cause':{'password':'pretend','message':'root cause'}},directory=home),ref)
    def test_more_than_eight_stderr_events_retained_once(self):
        with tempfile.TemporaryDirectory() as home:
            host=object.__new__(CodexHost)
            from collections import deque
            host.diagnostics=deque(maxlen=8);host.diagnostic_directory=home
            host.process=Mock(stderr=iter(['ERROR 中文 '+str(i)+' '+('context '*100)+'tail-'+str(i)+'\n' for i in range(12)]))
            host._stderr()
            self.assertEqual(len(list(Path(home).glob('*.json'))),12)
            self.assertEqual(len(host.diagnostics),8)
            text=''.join(p.read_text() for p in Path(home).glob('*.json'))
            self.assertIn('tail-0',text);self.assertIn('tail-11',text)
    def test_classification_reads_the_entire_safe_source(self):
        reply={'tool_use_id':'original','is_error':True,'content':'context '*900+'permission denied'}
        self.assertEqual(ClaudeAdapter.tool_failures([], [reply])[0]['error_class'],'permission_denied')
        reply['error']={'code':'EXACT_CODE'}
        self.assertEqual(ClaudeAdapter.tool_failures([], [reply])[0]['error_class'],'EXACT_CODE')
        reply['truncated']=True
        self.assertEqual(ClaudeAdapter.tool_failures([], [reply])[0]['error_class'],'unknown_incomplete_evidence')
    def test_unwritable_diagnostic_is_explicit(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'file';path.write_text('keep')
            self.assertFalse(safe.retain('safe cause',directory=path)['diagnostic_available'])

class ExecutionOwnershipTests(unittest.TestCase):
    def test_noninheritance_and_manager_exit_do_not_release_live_execution(self):
        with tempfile.TemporaryDirectory() as home:
            script='''import os,subprocess,sys,json
from pathlib import Path
from execution_lease import FileLock,atomic_json,process_identity
root=Path(sys.argv[1])
with FileLock(root,['request','original']) as lock:
 child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True,close_fds=True)
 atomic_json(root/'execution.json',{'phase':'spawned','manager':process_identity(os.getpid()),'child':process_identity(child.pid),'lock_inheritable':os.get_inheritable(lock.fd)})
 os._exit(0)
'''
            env={**os.environ,'PYTHONPATH':str(ROOT/'bin')}
            subprocess.run([sys.executable,'-c',script,home],env=env,check=True)
            doc=json.loads((Path(home)/'execution.json').read_bytes())
            try:
                self.assertFalse(doc['lock_inheritable'])
                for _ in range(2):
                    with lease.FileLock(home,['request','original']):
                        self.assertIn(lease.execution_observation(Path(home)/'execution.json')['state'],('active','unknown'))
            finally:
                os.killpg(doc['child']['pid'],15)
    def test_startup_gap_pid_reuse_and_eperm_stay_unknown(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'execution.json'
            lease.atomic_json(path,{'phase':'launch_intent','manager':{'pid':111,'start':'old','boot':'boot'}})
            with patch.object(lease,'matches',return_value=False):
                self.assertEqual(lease.execution_observation(path)['state'],'unknown')
            doc={'phase':'spawned','manager':{'pid':111},'child':{'pid':222,'pgid':222,'start':'old','boot':'boot'}}
            lease.atomic_json(path,doc)
            with patch.object(lease,'matches',return_value=False),patch.object(lease,'process_identity',return_value={'exists':None}):
                self.assertEqual(lease.execution_observation(path)['state'],'unknown')
            with patch.object(lease,'matches',return_value=False),patch.object(lease,'process_identity',return_value={'pid':222,'exists':True,'start':'new'}):
                self.assertEqual(lease.execution_observation(path)['state'],'unknown')
            with patch.object(lease,'matches',return_value=False),patch.object(lease,'process_identity',return_value={'pid':222,'exists':False}),patch.object(lease.os,'killpg',side_effect=PermissionError):
                self.assertEqual(lease.execution_observation(path)['state'],'unknown')
    def test_readable_replacement_leader_proves_old_group_ended_without_group_signal(self):
        record = {'pid':222,'pgid':222,'start':'Wed Nov 18 13:09:05 2026','boot':'boot'}
        observed = {**record,'exists':True,'start':'Wed Nov 18 13:27:06 2026'}
        with patch.object(lease, 'process_identity', return_value=observed), patch.object(lease.os, 'killpg') as probe:
            self.assertEqual(lease.tools_observation({'cli':{'phase':'spawned','process':record}})['state'], 'exited')
            self.assertEqual(lease.original_group({**record,'pgid':333})['state'], 'unknown')
            self.assertEqual(lease.original_group({**record,'boot':'different'})['state'], 'unknown')
            self.assertEqual(lease.original_group(observed)['state'], 'active')
            probe.assert_not_called()
        with patch.object(lease.os,'kill',side_effect=PermissionError), patch.object(lease,'boot_identity',return_value='boot'), \
             patch.object(lease.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'Wed Nov 18 13:27:06 2026 222 S','')):
            self.assertEqual(lease.original_group(record)['state'],'exited')

    def test_descendant_group_keeps_execution_ownership(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'execution.json'
            lease.atomic_json(path,{'phase':'spawned','manager':{'pid':111},'child':{'pid':222,'pgid':222}})
            with patch.object(lease,'matches',return_value=False),patch.object(lease,'process_identity',return_value={'exists':False}),patch.object(lease.os,'killpg',return_value=None):
                self.assertEqual(lease.execution_observation(path)['state'],'active')
            with patch.object(lease,'matches',return_value=False),patch.object(lease,'process_identity',return_value={'exists':False}),patch.object(lease.os,'killpg',side_effect=ProcessLookupError):
                self.assertEqual(lease.execution_observation(path)['state'],'exited')
