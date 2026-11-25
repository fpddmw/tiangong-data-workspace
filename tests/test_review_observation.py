"""Task-scoped native observations never mutate board or platform state."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from test_review_adapter import TARGET, fixture, response

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'bin'))
from platform_review import observation as board
SUBJECT = 'process:' + TARGET['id'] + '@' + TARGET['version']
ACCOUNT_ID = '551c89e9-c4e3-463b-8fc7-68fbe1ec8603'


def native_fixture():
    capture = fixture()
    capture['profile'] = 'native'
    capture['source'].update(account_user_id=ACCOUNT_ID, identity={'id': ACCOUNT_ID})
    capture['responses'][2]['data'][0]['user_id'] = ACCOUNT_ID
    return capture


class ReviewObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = 'materials/board.json'
        self.capture = native_fixture()
        self.capture['responses'][0].update(http_status=403, data=[],
            error={'code': '42501', 'message': 'REVIEW_ADMIN_REQUIRED'}, completeness={'complete': False})
        self.capture['responses'][2] = response('qry_membership_get_mine', [
            {'user_id': self.capture['source']['account_user_id'], 'role': 'data_product_manager'},
        ])
        self.ref = self.write('materials/original-brief.md', 'Original single plan; no duplicate body.')
        self.subjects = [SUBJECT]
        self.write(self.path, {'original_record': 'retained'})
        self.capture_ref = self.write('materials/capture.json', self.capture)

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value if isinstance(value, str) else json.dumps(value))
        return {'path': path, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}

    def observe(self, **kwargs):
        return board.observe_package(self.root, 'observation-task', self.subjects, self.capture_ref['path'],
                                     offline=True, **kwargs)

    def test_permission_failure_is_observed_without_any_state_change(self):
        original = (self.root / self.path).read_bytes()
        metadata = (self.root / self.path).stat().st_mtime_ns
        result = self.observe()
        self.assertNotIn('proposed_business_status',result['observation'])
        self.assertIsNone(result['observation']['cursor'])
        self.assertEqual(result['observation']['facts']['account_roles'], ['data_product_manager'])
        self.assertEqual(result['board_writes'], 0)
        self.assertEqual(result['platform_writes'], 0)
        self.assertEqual((self.root / self.path).read_bytes(), original)
        self.assertEqual((self.root / self.path).stat().st_mtime_ns, metadata)

    def test_malformed_unrelated_response_keeps_readable_original_opinions(self):
        import review_adapter
        from test_review_adapter import fixture
        capture = fixture()
        before = review_adapter.assess_capture(capture)['facts']['opinions']
        capture['schema'] = 'historical-observation-envelope'
        capture['responses'].append({'rpc': 'unrecognized-historical-read', 'data': None})
        observed = review_adapter.assess_capture(capture)
        self.assertEqual(observed['facts']['opinions'], before)
        self.assertIsNone(observed['cursor'])
        self.assertTrue(observed['blocked_conditions'])
        self.assertFalse(observed['live_transition_authority'])

    def test_malformed_nested_review_and_comment_do_not_erase_other_opinions(self):
        import review_adapter
        from test_review_adapter import fixture
        capture = fixture()
        before = review_adapter.assess_capture(capture)['facts']['opinions']
        for response in capture['responses']:
            if response['rpc'] == 'qry_review_get_comment_items':
                response['data'].append({'review_id': [], 'reviewer_id': {}, 'state_code': 1})
            if response['rpc'] in review_adapter.REVIEW_RPCS:
                for row in response['data']:
                    row['json'] = {'user': [], 'data': []}
        observed = review_adapter.assess_capture(capture)
        self.assertEqual(observed['facts']['opinions'], before)
        self.assertIsNone(observed['cursor'])
        self.assertTrue(observed['collection_errors'])

    def test_partial_or_different_scope_keeps_observed_facts_with_gaps(self):
        self.subjects.append('process:another-object@01.01.001')
        result = self.observe()
        self.assertIn('target_not_read:process:another-object@01.01.001',result['observation']['evidence_gaps'])
        self.assertEqual(result['platform_writes'],0)

    def test_same_workspace_alias_reads_original_capture(self):
        alias = self.root / 'materials/alias.json'
        alias.symlink_to(self.root / self.capture_ref['path'])
        result = board.observe_package(self.root,'observation-task',self.subjects,alias,offline=True)
        self.assertEqual(result['capture_ref'],self.observe()['capture_ref'])
        self.assertEqual(self.observe(previous_path=alias)['platform_writes'],0)
        outside=self.root.parent/'outside-capture.json'
        alias.unlink();alias.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'inside this workspace'):
            board.observe_package(self.root,'observation-task',self.subjects,alias,offline=True)

    def test_reading_saved_capture_retains_profile_without_native_actions(self):
        for profile in ('native','synthetic'):
            self.capture['profile']=profile
            self.write(self.capture_ref['path'],self.capture)
            result=board.observe_package(self.root,'observation-task',self.subjects,self.capture_ref['path'])
            self.assertEqual(result['observation']['profile'],profile)
            self.assertEqual(result['platform_writes'],0)

    def test_saved_account_provenance_is_independent_of_current_installation(self):
        self.write('.control/accounts.json', {'project_ref':'different-project','editor':{'user_id':'editor'},'manager':{'user_id':'manager'}})
        result=board.observe_package(self.root,'observation-task',self.subjects,self.capture_ref['path'])
        self.assertEqual(result['observation']['facts']['account_roles'],['data_product_manager'])
        self.assertEqual(result['platform_writes'],0)

    def test_explicit_existing_review_account_expectation_is_read_only(self):
        self.write('.control/accounts.json', {'project_ref': self.capture['source']['project'],
                    'editor': {'user_id': 'editor'}, 'manager': {'user_id': 'different-manager'}})
        board.observe_package(self.root, 'observation-task', self.subjects, self.capture_ref['path'])
        before = (self.root / self.path).read_bytes()
        result = board.observe_package(self.root, 'observation-task', self.subjects, self.capture_ref['path'],
                                   expected_account_user_id=self.capture['source']['account_user_id'])
        self.assertEqual(result['board_writes'], 0)
        self.assertEqual((self.root / self.path).read_bytes(), before)
        with self.assertRaisesRegex(board.BoardError, 'expectation'):
            board.observe_package(self.root, 'observation-task', self.subjects, self.capture_ref['path'],
                               expected_account_user_id='00000000-0000-0000-0000-000000000000')


    def test_frozen_native_read_keeps_reviewer_facts_without_certifying_delivery(self):
        capture = native_fixture()
        capture['responses'][-1]['completeness']['complete'] = False
        self.write(self.capture_ref['path'], capture)
        before = (self.root / self.path).read_bytes()
        result = self.observe(expected_account_user_id=capture['source']['account_user_id'])
        observation = result['observation']
        self.assertEqual(observation['profile'], 'native')
        self.assertEqual(observation['facts']['account_roles'], ['review-admin'])
        review = observation['facts']['reviews'][0]
        self.assertEqual(review['review_id'], 'review-1')
        self.assertEqual(review['state_code'], 1)
        self.assertEqual(len(set(review['active_reviewer_ids'])), 3)
        self.assertEqual([o['state_code'] for o in observation['facts']['opinions']], [0, 0, 0])
        self.assertNotIn('qualified_reviewer_ids',review)
        self.assertNotIn('proposed_business_status',observation)
        self.assertIsNone(observation['cursor'])
        self.assertEqual((self.root / self.path).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
