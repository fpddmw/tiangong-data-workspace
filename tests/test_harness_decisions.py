"""Scoped decision recovery and context navigation over isolated original records."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import context
from task_ledger import decisions
import harness_tasks as tasks
import test_harness_tasks as fixtures


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskCoreTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.root = self.f.root
        self.source = {**self.f.evidence, 'decided_by': 'Original user', 'locator': '/offline_test_only'}

    def document(self, ident, scope, statement=None, **extra):
        return {'id': ident, 'subject': 'endpoint', 'scope': scope,
                'statement': statement or ident, 'source_ref': self.source, **extra}

    def add(self, ident, scope, statement=None, **extra):
        return decisions.add(self.root, self.document(ident, scope, statement, **extra))

    def topics(self, **scope):
        return {item['subject']: item for item in decisions.resolve(self.root, scope)['subjects']}

    def test_global_defaults_and_exact_exception_select_by_range(self):
        exact = {'task': 'task', 'package': 'p', 'object': 'o@v1', 'action': 'submit', 'account': 'a'}
        self.assertEqual(decisions.resolve(self.root)['status'], 'unconfigured')
        with tasks._connect(self.root, True) as c:
            self.assertFalse(tasks._exists(c, 'work_decisions'))
        self.add('default', {})
        self.add('task-baseline', {'task': 'task'})
        self.add('one-exception', exact)
        self.assertEqual(self.topics()['endpoint']['decisions'][0]['id'], 'default')
        self.assertEqual(self.topics(task='task')['endpoint']['decisions'][0]['id'], 'task-baseline')
        self.assertEqual(self.topics(**exact)['endpoint']['decisions'][0]['id'], 'one-exception')
        for field, value in exact.items():
            self.assertNotEqual(self.topics(**(exact | {field: value + '-other'}))['endpoint']['decisions'][0]['id'], 'one-exception')
        self.assertIn(exact, [r['scope'] for r in decisions.resolve(self.root, {'task': 'task'})['narrower_scopes']])


    def test_conflict_and_withdrawal_are_local(self):
        exact = {'task': 'task', 'object': 'o', 'action': 'submit', 'account': 'a'}
        self.add('first', exact)
        self.add('second', exact)
        self.add('default', {})
        self.add('other-subject', {'task': 'task'}, subject='different-action')
        topics = self.topics(**exact)
        self.assertEqual(topics['endpoint']['status'], 'conflict')
        self.assertEqual(topics['different-action']['status'], 'applicable')
        self.assertEqual({d['id'] for d in topics['endpoint']['decisions']}, {'first', 'second'})
        self.assertEqual(self.topics(**(exact | {'account': 'b'}))['endpoint']['status'], 'applicable')
        withdrawal = {**self.f.save('withdrawal', {'text': 'End second decision'}), 'decided_by': 'Original user', 'locator': '/text'}
        record = decisions.withdraw(self.root, 'second', withdrawal)
        self.assertEqual(record['status'], 'withdrawn')
        self.assertEqual(record['withdrawal_ref'], withdrawal)
        self.assertEqual(decisions.withdraw(self.root, 'second', withdrawal), record)
        self.assertEqual(self.topics(**exact)['endpoint']['decisions'][0]['id'], 'first')

    def test_partial_and_complete_supersession_preserve_other_scopes(self):
        self.add('original', {})
        self.add('replacement', {}, supersedes=[{'id': 'original', 'scope': {'task': 'task'}}])
        self.assertEqual(self.topics(task='task')['endpoint']['decisions'][0]['id'], 'replacement')
        self.assertEqual(self.topics(task='elsewhere')['endpoint']['status'], 'conflict')
        self.assertEqual(decisions.get(self.root, 'original')['status'], 'active')
        with self.assertRaisesRegex(ValueError, 'within both'):
            self.add('unsafe', {'task': 'task'}, supersedes=[{'id': 'original', 'scope': {}}])
        self.add('full', {}, supersedes=[{'id': 'original'}, {'id': 'replacement'}])
        self.assertEqual(decisions.get(self.root, 'original')['status'], 'superseded')
        self.assertEqual(self.topics()['endpoint']['decisions'][0]['id'], 'full')
        decisions.withdraw(self.root, 'full', self.source)
        self.assertEqual(self.topics(), {})

    def test_missing_source_wrong_recorder_and_recovery(self):
        with self.assertRaisesRegex(ValueError, 'original decision source'):
            self.add('no-source', {}, source_ref=None)
        with self.assertRaisesRegex(ValueError, 'does not resolve'):
            self.add('bad-locator', {}, source_ref=self.source | {'locator': '/missing'})
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'worker'}):
            self.add('worker', {'task': 'task'})
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'lead'}):
            self.add('lead', {})
            self.add('lead-task', {'task': 'task'})
        self.f.call(self.f.main, 'grant-role', {'id': 'maint', 'actor': self.f.other, 'org_role': 'maintainer', 'evidence_ref': self.f.evidence})
        recovered = {**self.f.save('original-decision', {'text': 'Exact original record'}), 'decided_by': 'Original user', 'locator': '/text'}
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'other'}):
            self.add('recovered', {}, source_ref=recovered)
        result = self.topics()['endpoint']
        self.assertEqual(next(d for d in result['decisions'] if d['id'] == 'recovered')['recorded_by']['id'], 'other')
        Path(recovered['path']).write_text('{}')
        result = self.topics()['endpoint']
        self.assertEqual(result['status'], 'source_unverified')
        self.assertIn('source_issue', next(d for d in result['decisions'] if d['id'] == 'recovered'))



    def test_markdown_request_follows_original_return_and_disposition(self):
        import hashlib
        for path, text in (('roles/data.md', '# 执行\n\n## 从已有工作体推进\nRepair the reviewed issue.'),
                           ('workflows/dataset-review/README.md', '## 按当前阶段读取\nRead the current step.')):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        brief = self.root / 'work-package.md'
        brief.write_text('# Work package\nUse the current exact candidate through the agreed local endpoint.')
        request = self.f.request('markdown-work', input_ref={'path': str(brief),
            'sha256': hashlib.sha256(brief.read_bytes()).hexdigest()}, finite=True)
        def read():
            with patch.dict(os.environ, CODEX_THREAD_ID='worker'):
                return context.build_daily_context(self.root, request=request['id'])['current']['work']
        first = read()
        self.assertEqual(first['requests'][0]['input_ref']['path'], str(brief))
        self.assertEqual(first['returns'], [])
        self.assertNotIn('request_navigation', first)
        returned = self.f.returned(request_id=request['id'])
        self.assertEqual(read()['returns'][0]['id'], returned['id'])
        self.f.dispose(returned=returned['id'], disposition='revise', remaining=['a', 'b'])
        current = read()
        self.assertEqual(current['decisions'][0]['disposition'], 'revise')
        self.assertEqual(current['requests'][0]['input_ref'], first['requests'][0]['input_ref'])
        self.assertNotIn('stage_guide', current)



if __name__ == '__main__':
    unittest.main()
