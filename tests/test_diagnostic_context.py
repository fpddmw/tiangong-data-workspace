"""Business bodies remain whole; diagnostic-only transport payloads stay omitted."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from safe_diagnostics import retain, safe_error, safe_value

ROOT = Path(__file__).resolve().parents[1]


class DiagnosticContextTests(unittest.TestCase):
    def test_business_contract_body_and_tail_condition_are_preserved(self):
        body = {'purpose': 'Original scope ' * 2500 + '允许保存草稿，但尚未授权正式送审。',
                'endpoint': 'saved_draft', 'authority_refs': []}
        original = {'contract': {'body': body}, 'access_token': 'fixture-not-a-real-token',
                    'refreshToken':'fixture-camel-secret'}
        safe = safe_value(original)
        self.assertEqual(safe['contract']['body'], body)
        self.assertEqual(safe['access_token'], '[REDACTED]')
        self.assertEqual(safe['refreshToken'],'[REDACTED]')

    def test_native_error_details_and_retained_diagnostics_omit_transport_payloads(self):
        original = {'code': 'AUTH_CHECK_FAILED', 'message': 'authorization denied',
                    'details': {'body': 'fixture-complete-auth-response',
                                'headers': {'cookie': 'fixture-cookie'}, 'attempt': 2},
                    'cause': {'message': 'A complete safe root cause', 'response': 'fixture-private-response'}}
        safe = safe_error(original)
        self.assertEqual(safe['message'], 'authorization denied')
        self.assertEqual(safe['details']['body'], '[OMITTED transport/auth payload]')
        self.assertEqual(safe['details']['attempt'], 2)
        self.assertEqual(safe['cause']['message'], 'A complete safe root cause')
        self.assertEqual(safe['cause']['omitted_fields'], ['response'])
        with tempfile.TemporaryDirectory() as temp:
            result = retain(original, directory=temp)
            saved = json.loads(Path(result['diagnostic_ref']['path']).read_bytes())
        text = json.dumps(saved)
        self.assertNotIn('fixture-complete-auth-response', text)
        self.assertNotIn('fixture-private-response', text)
        self.assertIn('A complete safe root cause', text)

    def test_js_runtime_uses_the_same_context_boundary(self):
        binary = shutil.which('node')
        if not binary:
            self.skipTest('Requires Node.js to run the JavaScript diagnostic test')
        module = (ROOT / 'bin/safe_diagnostics.mjs').as_uri()
        source = ('import {safeValue,safeError} from ' + json.dumps(module) + ';'
                  'console.log(JSON.stringify({'
                  'business:safeValue({contract:{body:{purpose:"完整用途及限制"}},accessToken:"fixture-camel-secret"}),'
                  'error:safeError({message:"authorization denied",details:{body:"fixture-private-response",attempt:2}})'
                  '}));')
        result = subprocess.run([binary, '--input-type=module', '-e', source],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['business']['contract']['body'], {'purpose': '完整用途及限制'})
        self.assertEqual(value['business']['accessToken'],'[REDACTED]')
        self.assertEqual(value['error']['details']['body'], '[OMITTED transport/auth payload]')
        self.assertEqual(value['error']['details']['attempt'], 2)


if __name__ == '__main__':
    unittest.main()
