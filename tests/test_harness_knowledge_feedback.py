"""Isolated original-reference discovery and atomic feedback behavior; no live qualification."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'bin'))
import harness_tasks as tasks
import harness_knowledge as knowledge
import harness_feedback as feedback


class KnowledgeFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID': 'owner'}); self.env.start(); self.addCleanup(self.env.stop)
        self.actor = tasks.verified_actor(self.root)
        self.authority = self.write('original/approval.json', {'scope': 'isolated test fixture only'})
        tasks.initialize(self.root, self.actor, self.authority, 'isolated')
        tasks.mutate(self.root, 'grant-role', self.actor,
                     {'id': 'owner-role', 'actor': self.actor, 'org_role': 'task_lead', 'evidence_ref': self.authority})
        self.method = self.write('methods/method.md', 'Original method, read its actual professional contract.')
        self.counter = self.write('original/counterexample.json', {'observed': 'new process fails'})
        self.plan = self.write('original/plan.json', {'goal': 'A complete original task', 'knowledge_links': []})
        self.create_task()
        self.mapping = {'review_id': '/review', 'dataset_id': '/dataset/id', 'dataset_version': '/dataset/version',
                        'items': '/opinions', 'opinion_id': '/id', 'opinion_version': '/version',
                        'retracted': '/withdrawn', 'cursor': '/cursor', 'next_cursor': '/next',
                        'complete': '/complete', 'observed_at': '/time'}

    def write(self, name, obj):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        data = obj.encode() if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode()
        path.write_bytes(data)
        return {'path': name, 'sha256': hashlib.sha256(data).hexdigest()}

    def create_task(self):
        return tasks.mutate(self.root, 'create', self.actor, {'id': 'T', 'owner_actor': self.actor,
            'original_ref': self.plan, 'plan_ref': self.plan, 'summary': 'Original purpose', 'required_scope': ['process']})

    def page(self, name='page', opinions=None, cursor=None, next_cursor=None, complete=True, time='2026-10-29T09:00:00Z'):
        native = {'review': 'review-1', 'dataset': {'id': 'dataset-1', 'version': '01.00.000'},
                  'opinions': opinions if opinions is not None else [self.opinion()],
                  'cursor': cursor, 'next': next_cursor, 'complete': complete, 'time': time}
        ref = self.write(f'original/{name}.json', native)
        return {'task_id': 'T', 'source': 'supported-cli-original-receipt', 'source_ref': ref,
                'mapping': self.mapping}

    def opinion(self, ident='opinion-1', version='1', content='Check this quantity', withdrawn=False):
        return {'id': ident, 'version': version, 'content': content, 'withdrawn': withdrawn}

    def scalar(self, sql, params=()):
        with knowledge.read_connection(self.root) as c:
            return c.execute(sql, params).fetchone()[0]

    def revise_plan(self, doc):
        # The task original is immutable; a genuinely changed plan remains native.
        self.plan = self.write('original/current-plan.json', doc)
        snapshot = tasks.read_task(self.root, 'T')
        return tasks.mutate(self.root, 'revise', self.actor, {'id': 'revision-1', 'task_id': 'T',
            'expected_task_revision': snapshot['task']['revision'], 'plan_ref': self.plan,
            'evidence_ref': self.authority, 'summary': 'Evidence changed', 'affected_scope': ['process']})


    def test_relation_index_accepts_explicit_originals_and_preserves_index_identity(self):
        self.revise_plan({'knowledge_links': [{'method_ref': self.method, 'version': 'v1', 'relation': 'effect',
            'target_ref': self.counter, 'conditions': ['observed failure in original use'], 'required': True}]})
        knowledge.record_links(self.root, self.actor, 'T', self.plan)
        knowledge.record_links(self.root, self.actor, 'T', self.plan)
        self.assertEqual(self.scalar('SELECT count(*) FROM work_knowledge_links'), 1)
        with sqlite3.connect(self.root / '.control/harness.sqlite') as raw:
            with self.assertRaises(sqlite3.OperationalError):
                raw.execute("UPDATE work_knowledge_links SET version='invented'")
        bad = self.write('unrelated.json', {'knowledge_links': []})
        self.assertEqual(knowledge.record_links(self.root, self.actor, 'T', bad)['indexed'], 0)
        text = self.root / 'ordinary-method.md'; text.write_text('Read the original method directly.')
        ref = {'path': str(text), 'sha256': hashlib.sha256(text.read_bytes()).hexdigest()}
        self.assertEqual(knowledge.record_links(self.root, self.actor, 'T', ref)['indexed'], 0)
        malformed = self.write('optional.json', {'knowledge_links': [{}]})
        self.assertTrue(knowledge.record_links(self.root, self.actor, 'T', malformed)['warnings'])


    def test_complete_pages_are_atomic_and_replay_does_not_duplicate_requests(self):
        first = self.page(next_cursor='next-1')
        result = feedback.ingest_page(self.root, self.actor, first)
        again = feedback.ingest_page(self.root, self.actor, first)
        self.assertEqual(again['status'], 'already_ingested')
        self.assertEqual(self.scalar('SELECT count(*) FROM work_requests'), 1)
        incomplete = self.page('incomplete', cursor='next-1', complete=False)
        partial = feedback.ingest_page(self.root, self.actor, incomplete)
        self.assertEqual(partial['created_requests'], [])
        self.assertIsNone(partial['next_cursor'])
        self.assertEqual(feedback.current_view(self.root, 'T')['reliable_cursors'][0]['next_cursor'], 'next-1')
        second = self.page('second', cursor='next-1', opinions=[self.opinion('new-1'), self.opinion('new-2')])
        original_apply = tasks.apply
        def fail_second(c, root, action, actor, payload):
            if 'new-2' in payload['summary']:
                raise ValueError('simulated second-item storage failure')
            return original_apply(c, root, action, actor, payload)
        with patch.object(tasks, 'apply', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, 'second-item'):
                feedback.ingest_page(self.root, self.actor, second)
        self.assertEqual(self.scalar('SELECT count(*) FROM work_feedback_pages'), 2)
        self.assertEqual(self.scalar('SELECT count(*) FROM work_requests'), 1)
        self.assertEqual(feedback.current_view(self.root, 'T')['reliable_cursors'][0]['next_cursor'], 'next-1')
        feedback.ingest_page(self.root, self.actor, second)
        self.assertEqual(self.scalar('SELECT count(*) FROM work_requests'), 3)
        self.assertEqual(result['next_cursor'], 'next-1')

    def test_modified_and_retracted_opinions_keep_history_same_task_and_paused_state(self):
        first = feedback.ingest_page(self.root, self.actor, self.page())
        tasks.mutate(self.root, 'pause', self.actor, {'id': 'pause', 'task_id': 'T', 'expected_task_revision': 1,
            'evidence_ref': self.authority, 'summary': 'Explicit user pause'})
        second = feedback.ingest_page(self.root, self.actor,
            self.page('modified', [self.opinion(version='2', content='Changed opinion')], time='2026-10-29T10:00:00Z'))
        third = feedback.ingest_page(self.root, self.actor,
            self.page('withdrawn', [self.opinion(version='3', withdrawn=True)], time='2026-10-29T11:00:00Z'))
        snap = tasks.read_task(self.root, 'T')
        self.assertEqual(snap['task']['state'], 'paused')
        self.assertEqual(snap['pending_request_ids'], third['created_requests'])
        self.assertEqual(next(r for r in snap['requests'] if r['id'] == second['created_requests'][0])['supersedes_request_id'], first['created_requests'][0])
        view = feedback.current_view(self.root, 'T')
        self.assertEqual(len(view['observations']), 1)
        self.assertTrue(view['observations'][0]['retracted'])
        self.assertEqual(view['observations'][0]['supersedes_request_id'], second['created_requests'][0])

        original, = feedback.current_view(self.root, 'T', request_id=first['created_requests'][0])['observations']
        self.assertEqual(original['request_id'], first['created_requests'][0])
        self.assertEqual(original['request_state'], 'superseded')
        self.assertFalse(original['retracted'])
        self.assertEqual(self.scalar('SELECT count(*) FROM work_feedback_observations'), 3)
        self.assertEqual(view['collector']['status'], 'unavailable_live_collector')

    def test_late_historical_snapshot_keeps_current_opinion_and_cursor(self):
        latest = feedback.ingest_page(self.root, self.actor,
            self.page('current', [self.opinion(version='2', content='Current opinion')], time='2026-10-29T10:00:00Z'))
        original = feedback.ingest_page(self.root, self.actor,
            self.page('historical', [self.opinion(version='1', content='Earlier opinion')], time='2026-10-29T09:00:00Z'))
        view = feedback.current_view(self.root, 'T')
        self.assertEqual(view['observations'][0]['request_id'], latest['created_requests'][0])
        self.assertEqual(view['reliable_cursors'][0]['observed_at'], '2026-10-29T10:00:00Z')
        self.assertTrue(original['collection_issues'])
        self.assertEqual(self.scalar('SELECT count(*) FROM work_feedback_observations'), 2)
        historical, = feedback.current_view(self.root, 'T', request_id=original['created_requests'][0])['observations']
        self.assertIsNone(historical['supersedes_request_id'])

    def test_partial_native_capture_retains_three_opinions_without_advancing_cursor(self):
        from test_review_adapter import fixture
        capture=fixture()
        capture['responses'][2].update(http_status=403,error={'message':'membership unavailable'},data=[])
        ref=self.write('original/partial-capture.json',capture)
        result=feedback.ingest_capture(self.root,self.actor,{'task_id':'T','capture_ref':ref,
            'expected_project':'project-1','expected_account_user_id':'admin','offline':True})
        self.assertIsNone(result['collection'])
        self.assertEqual(sum(len(page['created_requests']) for page in result['pages']),3)
        self.assertEqual(self.scalar('SELECT count(*) FROM work_feedback_cursors'),0)
        self.assertEqual(self.scalar('SELECT count(*) FROM work_feedback_observations'),3)
        repeated=feedback.ingest_capture(self.root,self.actor,{'task_id':'T','capture_ref':ref,
            'expected_project':'project-1','expected_account_user_id':'admin','offline':True})
        self.assertEqual(sum(len(page['created_requests']) for page in repeated['pages']),0)

    def test_package_description_and_targets_evolve_without_new_package_or_plan(self):
        import harness_business as business
        first=self.write('original/package-a.json',{'package_id':'package','note':'First selection',
            'subject_refs':['process:f83f7f6c-cd88-5b4a-8263-a3d177b2f663@00.00.001']})
        original=business.bind_package(self.root,self.actor,'T',first['path'])
        tasks.mutate(self.root,'pause',self.actor,{'id':'pause','task_id':'T','expected_task_revision':1,
            'evidence_ref':self.authority,'summary':'Production paused; facts remain editable.'})
        second=self.write('original/package-b.json',{'package_id':'package','note':'Added evidence object',
            'subject_refs':['process:f83f7f6c-cd88-5b4a-8263-a3d177b2f663@00.00.001',
                            'flow:c6dddd18-b089-41c7-b881-1fd6d546f0db@00.00.001']})
        changed=business.bind_package(self.root,self.actor,'T',second['path'])
        self.assertEqual(changed['id'],original['id'])
        self.assertEqual(changed['original_id'],'package')
        self.assertEqual(self.scalar('SELECT count(*) FROM work_original_links'),1)
        self.assertEqual(self.scalar("SELECT count(*) FROM work_task_events WHERE kind='business-binding-updated'"),1)
        self.assertEqual(tasks.read_task(self.root,'T')['task']['state'],'paused')

    def test_plain_response_records_only_the_selected_opinion_and_preserves_history(self):
        feedback.ingest_page(self.root, self.actor, self.page())
        obs = feedback.current_view(self.root, 'T')['observations'][0]
        note = self.write('original/decision.md', 'Which unit does the source use?')
        first = feedback.dispose(self.root, self.actor, obs['request_id'], 'needs_clarification', note['path'])
        self.assertEqual(tasks.read_task(self.root, 'T')['pending_request_ids'], [obs['request_id']])
        resolved = self.write('original/response.md', 'The original table reports kilograms. The existing value is correct.')
        second = feedback.dispose(self.root, self.actor, obs['request_id'], 'not_adopted_with_evidence', resolved['path'])
        again = feedback.dispose(self.root, self.actor, obs['request_id'], 'not_adopted_with_evidence', resolved['path'])
        self.assertEqual(again['id'], second['id'])
        self.assertEqual(second['supersedes_id'], first['id'])
        snap = tasks.read_task(self.root, 'T')
        self.assertEqual(snap['pending_request_ids'], [])
        self.assertEqual(snap['dispositions'], [])
        self.assertEqual(snap['task']['state'], 'active')
        self.assertEqual(self.scalar('SELECT count(*) FROM work_feedback_dispositions'), 2)
        updated = feedback.ingest_page(self.root, self.actor, self.page('updated', [self.opinion(version='2')], time='2026-10-29T10:00:00Z'))
        self.assertEqual(tasks.read_task(self.root, 'T')['pending_request_ids'], updated['created_requests'])
        other = {**self.actor, 'id': 'other', 'instance': 'other'}
        with self.assertRaises(ValueError):
            feedback.dispose(self.root, other, obs['request_id'], 'clarify_only', note['path'])

    def test_revision_response_needs_no_repair_child_or_adoption_envelope(self):
        feedback.ingest_page(self.root, self.actor, self.page())
        obs = feedback.current_view(self.root, 'T')['observations'][0]
        note = self.write('original/repair.md', 'I will correct the quantity in the existing author Session.')
        feedback.dispose(self.root, self.actor, obs['request_id'], 'revise_data', note['path'])
        snap = tasks.read_task(self.root, 'T')
        self.assertEqual(len(snap['requests']), 1)
        self.assertEqual(snap['returns'], [])
        self.assertEqual(snap['dispositions'], [])
        self.assertEqual(snap['task']['state'], 'active')
        response = feedback.current_view(self.root, 'T')['observations'][0]['disposition']
        self.assertTrue(response['response_recorded'])
        self.assertNotIn('fulfilled', response)
        self.revise_plan({'goal': 'Changed method for the same process'})
        with knowledge.read_connection(self.root) as c:
            self.assertTrue(feedback.request_is_disposed(c, obs['request_id']))

    def test_missing_old_capture_does_not_prevent_recording_a_clarification(self):
        feedback.ingest_page(self.root, self.actor, self.page())
        obs = feedback.current_view(self.root, 'T')['observations'][0]
        (self.root / obs['source_ref']['path']).unlink()
        note = self.write('original/missing.md', 'The original capture is unavailable; requesting it from its owner.')
        feedback.dispose(self.root, self.actor, obs['request_id'], 'needs_clarification', note['path'])
        view = feedback.current_view(self.root, 'T')['observations'][0]
        self.assertEqual(view['disposition']['decision'], 'needs_clarification')
        self.assertIn(obs['request_id'], tasks.read_task(self.root, 'T')['pending_request_ids'])

    def test_missing_database_read_never_initializes_and_actor_label_cannot_grant_import(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ValueError):
                feedback.current_view(Path(empty), 'T')
            self.assertEqual(list(Path(empty).iterdir()), [])
        other = {**self.actor, 'id': 'other', 'instance': 'other'}
        with self.assertRaises(ValueError):
            feedback.ingest_page(self.root, other, self.page())
        self.assertEqual(self.scalar('SELECT count(*) FROM work_feedback_pages'), 0)
        credential = self.write('.env', 'OFFLINE_FIXTURE=not-a-real-secret')
        with self.assertRaisesRegex(ValueError, 'Credentials'):
            knowledge.source(self.root, credential)

    def test_malformed_optional_method_and_skill_refs_are_visible_gaps_not_task_wide_blocks(self):
        changed = self.revise_plan({'knowledge_links': [{'method_ref': {}, 'version': 'v1', 'relation': 'condition',
            'target_ref': self.counter, 'conditions': ['must read original condition'], 'required': True}],
            'skill_uses': [{'skill_ref': {}, 'version': 'v1', 'output_refs': [], 'remaining_branches': ['not completed']}]})
        self.assertTrue(changed['knowledge_warnings'])
        view = knowledge.discover(self.root, 'T')
        self.assertIn('originals', view)
        self.assertNotIn('required_context', view)
        self.assertEqual(tasks.read_task(self.root, 'T')['task']['state'], 'active')



if __name__ == '__main__':
    unittest.main()
