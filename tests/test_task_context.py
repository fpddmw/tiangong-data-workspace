"""Read-only original-record lookup: no inferred instructions or synthetic context."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import harness_snapshot
import harness_tasks
import test_harness_tasks as fixtures

BIN = Path(__file__).resolve().parents[1] / 'bin'


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskCoreTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.root = self.f.root

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-B', str(BIN / 'context.py'), '--root', str(self.root), *args],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_query_preserves_original_brief_and_has_no_inferred_step(self):
        request = self.f.request()
        returned = self.f.returned()
        self.f.dispose()
        before = (self.root / '.control/harness.sqlite').read_bytes()
        self.root.joinpath('brief.json').unlink()  # Missing evidence affects reading it, not querying records.
        value = self.cli('--request', request['id'])['current']
        work = value['work']
        self.assertEqual(work['requests'][0]['input_ref'], request['input_ref'])
        self.assertEqual(work['returns'][0]['result_ref'], returned['result_ref'])
        self.assertEqual(len(work['decisions']), 1)
        self.assertTrue({'task','requests','returns','runs','decisions','pagination','scope','history','detail_command'}<=set(work))
        self.assertEqual((self.root / '.control/harness.sqlite').read_bytes(), before)
        self.assertFalse((self.root / 'assignment.md').exists())

    def test_tasks_and_requests_page_without_auto_selecting_or_truncating(self):
        for n in range(4):
            self.f.request('r' + str(n))
        initial = self.cli()['current']
        self.assertNotIn('work', initial)
        self.assertEqual(initial['tasks'][0]['id'], 'task')
        first = self.cli('--task', 'task', '--limit', '2')['current']['work']
        second = self.cli('--task', 'task', '--limit', '2', '--offset', '2')['current']['work']
        self.assertEqual([r['id'] for r in first['requests'] + second['requests']], ['r0', 'r1', 'r2', 'r3'])
        self.assertEqual(first['pagination']['next_offset'], 2)
        self.assertIsNone(second['pagination']['next_offset'])

    def test_reader_label_does_not_change_actual_role_or_task(self):
        before = self.cli()['current']
        after = self.cli('--role', 'maintainer')['current']
        self.assertEqual(after, before)
        self.assertNotIn('maintainer', after['roles'])

    def test_missing_store_does_not_initialize_or_demand_registration(self):
        root = self.root / 'missing'
        value = harness_snapshot.read(root)
        self.assertEqual(value, {'status': 'UNCONFIGURED', 'tasks': []})
        self.assertFalse(root.exists())

    def test_explicit_request_cannot_be_queried_under_an_unrelated_task(self):
        self.f.request()
        with self.assertRaisesRegex(ValueError, 'different task'):
            harness_snapshot.read(self.root, task_id='other', request_id='request')

    def test_failure_hook_stays_separate_from_the_new_context_hook(self):
        for event in ('SessionStart', 'UserPromptSubmit'):
            result = subprocess.run([sys.executable, '-B', str(BIN / 'harness_hook.py'), '--root', str(self.root)],
                input=json.dumps({'hook_event_name': event, 'session_id': self.f.main['id'], 'cwd': str(self.root)}),
                capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout or '{}').get('hookSpecificOutput', {}).get('additionalContext', ''), '')
        self.assertFalse(list(self.root.rglob('*.cursor')))
