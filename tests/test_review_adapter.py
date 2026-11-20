import copy
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'bin' / 'review_adapter.py'
SPEC = importlib.util.spec_from_file_location('review_adapter', MODULE)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

NOW = '2026-10-29T07:00:00+00:00'
BEFORE = '2026-10-29T06:00:00+00:00'
TARGET = {'table': 'processes', 'id': 'dataset-1', 'version': '01.00.000'}
BODY = {'processDataSet': {'name': '实际格式测试', 'amount': '1.0'}}


def response(rpc, rows, params=None, paged=False):
    completeness = {'complete': True}
    params = params or {}
    if paged:
        params = {**params, 'p_page': 1, 'p_page_size': 100}
        completeness['pagination'] = {'page': 1, 'page_size': 100, 'total_count': len(rows)}
    return {'rpc': rpc, 'params': params, 'data': rows, 'http_status': 200,
            'error': None, 'completeness': completeness}


def fixture(state=1):
    checksum = adapter.native_body_checksum(BODY)
    review = {'id': 'review-1', 'data_id': TARGET['id'], 'data_version': TARGET['version'],
              'target_table': 'processes', 'review_kind': 'root', 'state_code': state,
              'reviewer_id': ['expert-1', 'expert-2', 'expert-3'],
              'submitted_revision_checksum': checksum, 'modified_at': BEFORE,
              'json': {'data': TARGET, 'user': {'id': 'author'}, 'logs': []}}
    if state == 2:
        review['json']['logs'] = [{'action': 'approved', 'time': BEFORE,
                                  'user': {'id': 'admin'}, 'approved_revision_checksum': checksum}]
    comments = [{'review_id': 'review-1', 'reviewer_id': member, 'state_code': 2 if state == 2 else 0,
                 'json': {}, 'created_at': BEFORE, 'modified_at': BEFORE} for member in review['reviewer_id']]
    return {'schema': adapter.SCHEMA, 'profile': 'synthetic', 'contract_profile': 'source_v2',
            'source': {'origin': 'browser_network_export', 'project': 'project-1', 'account_user_id': 'admin',
                       'identity': {'id': 'admin'}, 'observed_at': NOW, 'identity_observed_at': BEFORE},
            'scope': {'targets': [TARGET], 'review_ids': ['review-1'], 'closure_targets': [TARGET]},
            'responses': [response('qry_review_admin_queue_items_v2', [review], paged=True),
                          response('qry_review_get_comment_items', comments, {'p_review_id': 'review-1', 'p_scope': 'all'}),
                          response('qry_membership_get_mine', [{'user_id': 'admin', 'team_id': adapter.ZERO_TEAM, 'role': 'review-admin'}]),
                          response('qry_review_get_member_list', [
                              {'user_id': m, 'team_id': adapter.ZERO_TEAM, 'role': 'review-member'} for m in review['reviewer_id']], paged=True),
                          response('qry_root_review_reference_progress_v2', [], {'p_root_review_id': 'review-1'})],
            'datasets': [{**TARGET, 'state_code': 100 if state == 2 else 20, 'json_ordered': BODY, 'observed_at': NOW}]}






class ReviewAdapterTests(unittest.TestCase):
    def test_submitted_checksum_observation_distinguishes_absent_null_and_value(self):
        for mode in ('field_absent', 'reported_null', 'reported_value'):
            with self.subTest(mode=mode):
                capture = fixture()
                review = capture['responses'][0]['data'][0]
                if mode == 'field_absent':
                    review.pop('submitted_revision_checksum')
                elif mode == 'reported_null':
                    review['submitted_revision_checksum'] = None
                facts = adapter.assess_capture(capture)['facts']['reviews'][0]
                self.assertEqual(facts['submitted_checksum_observation'], mode)
        capture = fixture()
        capture['responses'][0]['data'][0]['submitted_revision_checksum'] = 'invalid-checksum'
        result = adapter.assess_capture(capture)
        self.assertEqual(result['facts']['reviews'][0]['submitted_checksum_observation'], 'reported_value')
        self.assertIn('native_submitted_checksum_missing:review-1', result['evidence_gaps'])

    def test_collection_reads_distinguish_local_range_validation_from_native_http_error(self):
        capture = fixture()
        read = capture['responses'][1]
        reason = 'native_http_range_not_proven_complete'
        read['completeness'].update(complete=False, range_present=True, reason=reason)
        read['error'] = {'code': 'HAR_READ_INCOMPLETE', 'message': reason}
        result = adapter.assess_capture(capture)
        facts = result['facts']['collection_reads'][1]
        self.assertEqual(facts, {'rpc': read['rpc'], 'http_status': 200, 'collector_complete': False,
                                 'error_code': 'HAR_READ_INCOMPLETE', 'error_origin': 'local_capture_validation'})
        self.assertIn('read_failed:' + read['rpc'], result['blocked_conditions'])
        self.assertIn('read_incomplete:' + read['rpc'], result['blocked_conditions'])
        self.assertIsNone(result['cursor'])
        read.update(http_status=403, error={'code': '42501', 'message': 'REVIEW_ADMIN_REQUIRED'})
        result = adapter.assess_capture(capture)
        self.assertEqual(result['facts']['collection_reads'][1]['error_origin'], 'native_response')
        self.assertEqual(result['facts']['collection_reads'][1]['http_status'], 403)
        self.assertIsNone(result['cursor'])

    def test_missing_submitted_checksum_is_unknown_not_body_mismatch(self):
        capture = fixture()
        capture['responses'][0]['data'][0].pop('submitted_revision_checksum')
        result = adapter.assess_capture(capture)
        self.assertIn('native_submitted_checksum_missing:review-1', result['evidence_gaps'])
        self.assertNotIn('submitted_body_or_state_changed:review-1', result['evidence_gaps'])
        self.assertEqual(result['facts']['reviews'][0]['submitted_body_comparison'], 'missing_submitted_checksum')
        self.assertNotIn('proposed_business_status', result)
        # Complete opinions can return to their task while scientific use
        # remains blocked by the missing native submitted checksum.
        self.assertEqual(result['collection_errors'], [])
        self.assertIsNotNone(result['cursor'])

    def test_json_only_digest_does_not_establish_native_fingerprint_basis(self):
        capture = fixture()
        capture['datasets'][0]['json'] = capture['datasets'][0].pop('json_ordered')
        result = adapter.assess_capture(capture)
        facts = result['facts']['reviews'][0]
        self.assertEqual(facts['current_body_sha256'], adapter.native_body_checksum(BODY))
        self.assertEqual(facts['current_body_basis'], 'json')
        self.assertIsNone(facts['native_body_checksum'])
        self.assertEqual(facts['submitted_body_comparison'], 'current_native_checksum_unverified')
        self.assertIn('native_body_checksum_unverified:review-1', result['evidence_gaps'])
        self.assertNotIn('submitted_body_or_state_changed:review-1', result['evidence_gaps'])
        self.assertNotIn('proposed_business_status', result)

    def test_proven_native_fingerprint_mismatch_remains_blocked(self):
        capture = fixture()
        capture['responses'][0]['data'][0]['submitted_revision_checksum'] = 'a' * 64
        result = adapter.assess_capture(capture)
        self.assertEqual(result['facts']['reviews'][0]['submitted_body_comparison'], 'mismatch')
        self.assertIn('submitted_body_or_state_changed:review-1', result['evidence_gaps'])

    def test_workload_roles_and_counts_do_not_qualify_experts(self):
        capture = fixture()
        capture['responses'][3]['rpc'] = 'qry_review_get_member_workload'
        for row in capture['responses'][3]['data']:
            row.update(pending_count=0, reviewed_count=200)
        result = adapter.assess_capture(capture)
        self.assertEqual(result['facts']['reviews'][0]['reviewer_roles']['expert-1'], ['review-member'])
        self.assertNotIn('qualified_reviewer_ids',result['facts']['reviews'][0])

    def test_native_rows_never_imply_scientific_qualification(self):
        result = adapter.assess_capture(fixture())
        self.assertNotIn('proposed_business_status', result)
        self.assertEqual(len(result['facts']['reviews'][0]['active_reviewer_ids']), 3)
        self.assertNotIn('qualified_reviewer_ids',result['facts']['reviews'][0])
        self.assertFalse(result['live_transition_authority'])
        self.assertIsNotNone(result['cursor'])





    def test_approved_and_published_need_approved_body_and_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = fixture(2)
            self.assertEqual(adapter.assess_capture(capture)['facts']['reviews'][0]['state_code'],2)
            capture['datasets'][0]['state_code'] = 20
            result = adapter.assess_capture(capture)
            self.assertNotIn('proposed_business_status', result)
            self.assertEqual(result['facts']['reviews'][0]['state_code'], 2)
            self.assertIn('approved_publication_body_not_verified:review-1', result['evidence_gaps'])

    def test_native_approval_metadata_change_uses_approved_not_submitted_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = fixture(2)
            approved_body = {'processDataSet': {'name': 'New native approved metadata'}}
            capture['datasets'][0]['json_ordered'] = approved_body
            capture['responses'][0]['data'][0]['json']['logs'][0]['approved_revision_checksum'] = adapter.native_body_checksum(approved_body)
            self.assertEqual(adapter.assess_capture(capture)['facts']['reviews'][0]['state_code'],2)

    def test_closed_comment_does_not_reconstruct_prior_vote(self):
        result = adapter.assess_capture(fixture(2))
        self.assertEqual(result['facts']['opinions'][0]['meaning'], 'admin_approved_closed')
        self.assertIsNone(result['facts']['opinions'][0]['prior_reviewer_vote'])

    def test_revocation_and_draft_assignments_do_not_count(self):
        capture = fixture()
        capture['responses'][1]['data'][0]['state_code'] = -2
        result = adapter.assess_capture(capture)
        self.assertEqual(len(result['facts']['reviews'][0]['active_reviewer_ids']), 2)
        self.assertEqual(result['facts']['opinions'][0]['meaning'], 'revoked')
        capture['responses'][0]['data'][0]['state_code'] = 0
        self.assertEqual(adapter.assess_capture(capture)['facts']['reviews'][0]['active_reviewer_ids'], [])

    def test_reviewer_must_have_current_native_membership_even_with_adjudication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = fixture()
            capture['responses'][3]['data'][0]['role'] = 'data_product_manager'
            result = adapter.assess_capture(capture)
            self.assertEqual(result['facts']['reviews'][0]['reviewer_roles']['expert-1'],[])
            self.assertIsNotNone(result['cursor'])

    def test_permission_negative_capture_valid_but_not_a_successful_empty_review(self):
        capture = fixture()
        capture['profile'] = 'native'
        capture['scope']['review_ids'] = []
        capture['datasets'] = []
        capture['responses'] = [response('qry_membership_get_mine', [
            {'user_id': 'admin', 'team_id': adapter.ZERO_TEAM, 'role': 'data_product_manager'}])]
        self.assertEqual(adapter.validate_capture(capture), [])
        result = adapter.assess_capture(capture)
        self.assertIsNone(result['cursor'])
        self.assertNotIn('proposed_business_status', result)
        self.assertEqual(result['facts']['account_roles'], ['data_product_manager'])

    def test_empty_rls_rows_do_not_mean_no_review(self):
        capture = fixture()
        capture['responses'][0]['data'] = []
        capture['responses'][0]['completeness']['pagination']['total_count'] = 0
        result = adapter.assess_capture(capture)
        self.assertIn('review_missing_or_rls_hidden:review-1', result['blocked_conditions'])
        self.assertIsNone(result['cursor'])

    def test_mine_or_unknown_actor_cannot_claim_all_comments(self):
        for mutation in ('mine', 'MINE', 'SELF', 'role'):
            capture = fixture()
            if mutation != 'role':
                capture['responses'][1]['params']['p_scope'] = mutation
            else:
                capture['responses'][2]['data'][0]['role'] = 'data_product_manager'
            result = adapter.assess_capture(capture)
            self.assertIn('all_review_comments_not_proven_visible:review-1', result['blocked_conditions'])
            self.assertIsNone(result['cursor'])

    def test_closed_comment_on_active_review_is_inconsistent_and_not_active(self):
        for state in (-1, 2):
            capture = fixture()
            capture['responses'][1]['data'][0]['state_code'] = state
            result = adapter.assess_capture(capture)
            self.assertEqual(len(result['facts']['reviews'][0]['active_reviewer_ids']), 2)
            self.assertIsNone(result['cursor'])
            self.assertIn('native_state_inconsistent_closed_comment_on_active_review:review-1', result['blocked_conditions'])

    def test_wrong_target_and_invalid_states_do_not_advance_cursor(self):
        for field, value in [('target_table', 'flows'), ('state_code', 999), ('state_code', True), ('reviewer_id', 'fake'), ('review_kind', None)]:
            capture = fixture()
            capture['responses'][0]['data'][0][field] = value
            result = adapter.assess_capture(capture)
            self.assertIsNone(result['cursor'])
            self.assertNotIn('proposed_business_status', result)
        capture = fixture()
        capture['responses'][1]['data'][0]['state_code'] = True
        self.assertIsNone(adapter.assess_capture(capture)['cursor'])

    def test_reference_progress_must_bind_actual_child_review(self):
        capture = fixture()
        capture['responses'][4]['data'] = [{'reference_review_id': 'uncaptured', 'target_table': 'processes',
                                          'data_id': TARGET['id'], 'data_version': TARGET['version'],
                                          'submitted_revision_checksum': adapter.native_body_checksum(BODY), 'state_code': 2}]
        result = adapter.assess_capture(capture)
        self.assertIn('reference_review_binding_mismatch:review-1', result['evidence_gaps'])
        self.assertIsNotNone(result['cursor'])  # Opinions were completely read; business use remains blocked.
        self.assertNotIn('proposed_business_status', result)
        capture['responses'].append(response('qry_root_review_reference_progress_v2', [], {'p_root_review_id': 'review-1'}))
        result = adapter.assess_capture(capture)
        self.assertIn('unstable_reference_read:review-1', result['blocked_conditions'])
        self.assertIsNone(result['cursor'])  # Conflicting reads do not establish a stable capture.
        self.assertNotIn('proposed_business_status', result)

    def test_rejected_target_zero_needs_full_exact_target_review_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = fixture(-1)
            capture['datasets'][0]['state_code'] = 0
            for comment in capture['responses'][1]['data']:
                comment['state_code'] = -1
            result = adapter.assess_capture(capture)
            self.assertIn('other_active_reviews_not_enumerated:review-1', result['evidence_gaps'])
            capture['responses'].append(response('qry_review_get_items', [copy.deepcopy(capture['responses'][0]['data'][0])],
                                                 {'p_review_ids': None, 'p_data_id': TARGET['id'], 'p_data_version': TARGET['version'], 'p_state_codes': None}))
            self.assertEqual(adapter.assess_capture(capture)['facts']['reviews'][0]['state_code'],-1)
            for field in ('p_review_ids', 'p_state_codes'):
                with self.subTest(field=field):
                    bad = copy.deepcopy(capture)
                    bad['responses'][-1]['params'][field] = []
                    self.assertIn('other_active_reviews_not_enumerated:review-1', adapter.assess_capture(bad)['evidence_gaps'])

    def test_conflicting_state_aliases_are_rejected_between_and_within_reads(self):
        capture = fixture()
        extra = copy.deepcopy(capture['responses'][0])
        extra['rpc'] = 'qry_review_get_member_queue_items_v4'
        del extra['data'][0]['state_code']
        extra['data'][0]['review_state_code'] = 2
        capture['responses'].append(extra)
        result = adapter.assess_capture(capture)
        self.assertIn('unstable_review_read:review-1', result['blocked_conditions'])
        self.assertIsNone(result['cursor'])
        capture['responses'][0]['data'][0]['review_state_code'] = 2
        self.assertTrue(adapter.validate_capture(capture))

    def test_failed_partial_and_incomplete_pages_never_advance_cursor(self):
        for mutate in (lambda c: c['responses'][1].update(http_status=403),
                       lambda c: c['responses'][1].update(error={'message': 'failed'}),
                       lambda c: c['responses'][1]['completeness'].update(complete=False),
                       lambda c: c['responses'][0]['completeness']['pagination'].update(total_count=101),
                       lambda c: c['responses'][0]['completeness'].pop('pagination')):
            capture = fixture()
            mutate(capture)
            result = adapter.assess_capture(capture)
            self.assertIsNone(result['cursor'])
            self.assertNotIn('proposed_business_status', result)

    def test_duplicate_or_unstable_native_rows_are_rejected(self):
        capture = fixture()
        capture['responses'][1]['data'].append(copy.deepcopy(capture['responses'][1]['data'][0]))
        self.assertTrue(adapter.validate_capture(capture))
        capture = fixture()
        duplicate = copy.deepcopy(capture['responses'][0])
        duplicate['data'][0]['modified_at'] = NOW
        capture['responses'].append(duplicate)
        result = adapter.assess_capture(capture)
        self.assertIn('unstable_review_read:review-1', result['blocked_conditions'])
        self.assertIsNone(result['cursor'])

    def test_out_of_scope_and_changed_body_block(self):
        capture = fixture()
        capture['datasets'][0]['json_ordered'] = {'changed': True}
        self.assertIn('submitted_body_or_state_changed:review-1', adapter.assess_capture(capture)['evidence_gaps'])
        capture['responses'][1]['data'][0]['review_id'] = 'other'
        self.assertIn('invalid_comment_binding', adapter.assess_capture(capture)['blocked_conditions'])

    def test_changed_or_disappeared_opinion_is_not_silently_processed(self):
        previous = fixture()
        previous['source']['observed_at'] = BEFORE
        current = fixture()
        current['responses'][1]['data'][0]['json'] = {'comment': {'message': 'new reason'}}
        current['responses'][1]['data'][0]['modified_at'] = NOW
        result = adapter.assess_capture(current, previous)
        self.assertEqual(result['opinion_changes'][0]['change'], 'modified')
        current['responses'][1]['data'].pop(0)
        result = adapter.assess_capture(current, previous)
        self.assertEqual(result['opinion_changes'][0]['change'], 'removed_uncertain')
        self.assertIsNone(result['cursor'])

    def test_profile_account_project_and_scope_cannot_cross_cursor(self):
        previous = fixture()
        previous['source']['observed_at'] = BEFORE
        current = fixture()
        current['profile'] = 'native'
        self.assertIn('previous_capture_binding_mismatch', adapter.assess_capture(current, previous)['blocked_conditions'])
        self.assertTrue(adapter.validate_capture(current, expected_project='wrong'))
        self.assertTrue(adapter.validate_capture(current, expected_account_user_id='other'))

    def test_v6_requires_distinct_observed_profile(self):
        capture = fixture()
        capture['responses'][0]['rpc'] = 'qry_review_get_admin_queue_items_v6'
        self.assertTrue(adapter.validate_capture(capture))
        capture['contract_profile'] = 'observed_v6'
        self.assertEqual(adapter.validate_capture(capture), [])

    def test_unknown_rpc_including_mutation_is_not_accepted(self):
        capture = fixture()
        capture['responses'][0]['rpc'] = 'cmd_review_finalize_approve'
        self.assertTrue(adapter.validate_capture(capture))

    def test_lossless_decimal_and_unicode_native_hashing(self):
        self.assertNotEqual(adapter.native_body_checksum({'x': Decimal('1.0')}), adapter.native_body_checksum({'x': 1}))
        self.assertEqual(adapter.canonical_json({'x': Decimal('0.10')}), '{"x":0.10}')
        self.assertEqual(adapter.canonical_json({'x': Decimal('1e-6')}), '{"x":0.000001}')
        self.assertEqual(adapter.canonical_json({'x': Decimal('-0.00')}), '{"x":0.00}')
        self.assertEqual(adapter.canonical_json({'中': 1, 'é': 2, 'A': 3}), '{"A":3,"é":2,"中":1}')
        self.assertIsNone(adapter.native_body_checksum({'x': 1.0}))

    def test_raw_loader_preserves_scale_and_rejects_nonfinite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.json'
            path.write_text('{"value":0.10}')
            self.assertEqual(adapter.canonical_json(adapter.load_capture(path)), '{"value":0.10}')
            path.write_text('{"value":NaN}')
            with self.assertRaises(ValueError):
                adapter.load_capture(path)
            path.write_text('{"value":1,"value":2}')
            with self.assertRaises(ValueError):
                adapter.load_capture(path)
            with path.open('wb') as stream:
                stream.truncate(adapter.MAX_CAPTURE_BYTES + 1)
            with self.assertRaises(ValueError):
                adapter.load_capture(path)

    def test_malformed_arbitrary_nesting_reports_without_crash(self):
        values = [None, [], {}, {'schema': adapter.SCHEMA, 'source': {'identity': None}}]
        for mutator in (lambda c: c['responses'][0]['data'][0].update(json=None),
                        lambda c: c['responses'][0]['data'][0].update(json={'user': []}),
                        lambda c: c['responses'][0]['data'][0].update(id=[]),
                        lambda c: c['scope'].update(targets=[{'table': 'flows', 'id': []}])):
            value = fixture()
            mutator(value)
            values.append(value)
        for value in values:
            with self.subTest(value=repr(value)[:100]):
                result = adapter.assess_capture(value)
                self.assertIsNone(result['cursor'])
                self.assertNotIn('proposed_business_status', result)
                self.assertTrue(result['blocked_conditions'])


if __name__ == '__main__':
    unittest.main()
