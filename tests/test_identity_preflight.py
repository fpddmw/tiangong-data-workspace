"""Public identity-read branches; synthetic credentials and no real CLI/network."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import collaboration_tools as tools


class IdentityPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        (self.root/'bin').mkdir();(self.root/'bin/launch.py').write_text('# isolated entry')
        (self.root/'.control').mkdir()
        (self.root/'.control/accounts.json').write_text(json.dumps({'project_ref':'fixture-project','editor':{'user_id':'fixture-owner'}}))
        self.session=self.root/'original-owner-session.json'
        self.session.write_text('fixture-session-bytes-must-never-be-read')
        self.session.chmod(0o600)
        (self.root/'.env').write_text('DATA_EDITOR_SESSION_FILE='+str(self.session)+'\n')
        (self.root/'.env').chmod(0o600)
        self.config={'role':'executor'}
        self.receipt={'schema':'tiangong-lca.auth-identity-receipt.v1','status':'passed',
            'captured_at_utc':'fixture-check-time','assertions':{'passed':True,'mode':'intent-bound'},
            'project':{'ref':'fixture-project'},'identity':{'id':'fixture-owner'},
            'session':{'expires_at_utc':'fixture-expiry-must-not-be-task-authority',
                'access_token':'fixture-secret-must-not-be-copied'},'unexpected':'must-not-copy'}

    def result(self,body,code=0):
        return subprocess.CompletedProcess(['fixture'],code,json.dumps(body),'')

    def passed(self):return self.result({'data':{'receipt':self.receipt}})
    def run_preflight(self):return tools.editor_preflight(self.root,self.config,{'native_cli'})

    def test_valid_refresh_is_account_bound_safe_and_does_not_become_task_expiry(self):
        before=self.session.read_bytes()
        with patch.object(tools.subprocess,'run',return_value=self.passed()) as called:
            result=self.run_preflight()
        argv=called.call_args.args[0]
        self.assertEqual(argv[argv.index('--expected-project-ref')+1],'fixture-project')
        self.assertEqual(argv[argv.index('--expected-user-id')+1],'fixture-owner')
        self.assertEqual(result['identity_state'],'身份已核')
        self.assertEqual(result['session']['access_token'],'[REDACTED]')
        self.assertNotIn('unexpected',result)
        self.assertNotIn('fixture-expiry',tools.identity_note(result))
        self.assertIn('身份有效期不代表任务授权期限',tools.identity_note(result))
        self.assertEqual(self.session.read_bytes(),before)

    def test_concurrent_refreshes_each_use_native_cli_without_local_token_cache(self):
        with patch.object(tools.subprocess,'run',return_value=self.passed()) as called:
            with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.run_preflight(),range(2)))
        self.assertEqual(called.call_count,2)
        self.assertTrue(all(value['status']=='passed' for value in results))
        self.assertEqual(sorted(path.name for path in (self.root/'.control').iterdir()),['accounts.json'])
        self.assertTrue(all('identity-receipt' in call.args[0] for call in called.call_args_list))
        # Actual concurrent OAuth rotation/locking is the native CLI's contract,
        # not something this mock or the harness claims to have qualified.

    def test_transient_network_and_timeout_retry_only_the_identity_read(self):
        network=self.result({'error':{'code':'AUTH_IDENTITY_SESSION_FAILED',
            'cause':{'code':'ECONNRESET','message':'Transient network failure'}}},1)
        timeout=subprocess.TimeoutExpired(['fixture-auth'],30)
        with patch.object(tools.subprocess,'run',side_effect=[network,timeout,self.passed()]) as called, \
             patch.object(tools.time,'sleep') as slept:
            result=self.run_preflight()
        self.assertEqual(result['status'],'passed')
        self.assertEqual([call.args[0] for call in slept.call_args_list],[1,3])
        self.assertEqual(called.call_count,3)
        self.assertTrue(all('identity-receipt' in call.args[0] for call in called.call_args_list))

    def test_exhausted_timeouts_keep_all_safe_attempts_and_bounded_delays(self):
        with patch.object(tools.subprocess,'run',side_effect=subprocess.TimeoutExpired(['fixture-auth'],30)) as called, \
             patch.object(tools.time,'sleep') as slept:
            result=self.run_preflight()
        self.assertEqual(called.call_count,4)
        self.assertEqual([call.args[0] for call in slept.call_args_list],[1,3,10])
        self.assertEqual(result['identity_state'],'服务不可达')
        self.assertEqual(len(result['safe_errors']),4)
        self.assertEqual(result['code'],'IDENTITY_COMMAND_TIMEOUT')

    def test_missing_invalid_grant_keeps_original_cause_and_requires_owner_login(self):
        self.session.unlink()
        failed=self.result({'error':{'code':'AUTH_IDENTITY_SESSION_FAILED','message':'invalid_grant',
            'details':{'cause_code':'invalid_grant','body':'fixture-private-auth-response'}}},1)
        status=self.result({'data':{'sessionState':'missing','status':'login-required'}})
        with patch.object(tools.subprocess,'run',side_effect=[failed,status]) as called, \
             patch.object(tools.time,'sleep') as slept:
            result=self.run_preflight()
        self.assertEqual(called.call_count,2);slept.assert_not_called()
        self.assertIn('status',called.call_args.args[0])
        self.assertEqual(result['identity_state'],'需登录')
        self.assertFalse(result['session_path_before']['exists'])
        self.assertIn('unknown',result['cause'])
        self.assertNotIn('fixture-private-auth-response',json.dumps(result))
        self.assertIn('original account owner',result['recovery'])

    def test_sandbox_denial_preserves_trusted_bridge_without_copying_session_bytes(self):
        failed=self.result({'error':{'code':'AUTH_SESSION_ACCESS','message':'Permission denied',
            'details':{'cause_code':'EACCES'}}},1)
        status=self.result({'data':{'sessionState':'authenticated'}})
        original=Path.read_bytes
        def guard(path):
            if path==self.session:raise AssertionError('Harness must not read the owner token file')
            return original(path)
        with patch.object(Path,'read_bytes',guard),patch.object(tools.subprocess,'run',side_effect=[failed,status]), \
             patch.object(tools.time,'sleep') as slept:
            result=self.run_preflight()
        slept.assert_not_called()
        self.assertEqual(result['identity_state'],'需刷新')
        self.assertIn('trusted refresh_editor_identity bridge',result['cause'])
        self.assertTrue(result['session_path_after']['exists'])

    def test_status_probe_failure_does_not_replace_original_identity_error(self):
        failed=self.result({'error':{'code':'AUTH_IDENTITY_SESSION_FAILED','message':'missing original session'}},1)
        with patch.object(tools.subprocess,'run',side_effect=[failed,subprocess.TimeoutExpired(['status'],30)]):
            result=self.run_preflight()
        self.assertEqual(result['code'],'AUTH_IDENTITY_SESSION_FAILED')
        self.assertEqual(result['public_status']['state'],'unavailable')
        self.assertEqual(result['identity_state'],'需登录')

    def test_reviewer_does_not_refresh_the_author_account(self):
        self.config={'role':'scientific_reviewer'}
        with patch.object(tools.subprocess,'run') as called:
            self.assertIsNone(self.run_preflight())
        called.assert_not_called()


if __name__=='__main__':unittest.main()
