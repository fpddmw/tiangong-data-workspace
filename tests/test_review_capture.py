import base64
import contextlib
import copy
from decimal import Decimal
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
capture_module = importlib.import_module('review_capture')
adapter = importlib.import_module('review_adapter')

PROJECT = 'qgzvkongdjqiiamzbbts'
HOST = 'https://' + PROJECT + '.supabase.co'
TARGET = {'table': 'processes', 'id': 'process-1', 'version': '01.01.001'}
SCOPE = {'targets': [TARGET], 'review_ids': ['review-1'], 'closure_targets': [TARGET]}
REVIEW = {'id': 'review-1', 'data_id': 'process-1', 'data_version': '01.01.001', 'state_code': 1,
          'target_table': 'processes', 'review_kind': 'root', 'reviewer_id': ['expert-1'],
          'json': {'data': TARGET, 'user': {'id': 'owner'}, 'logs': []},
          'modified_at': '2026-10-29T07:00:00Z', 'total_count': 1}
PARAMS = {'p_status': 'in-progress', 'p_query': 'exact feed', 'p_page': 1, 'p_page_size': 50}


def identity():
    return {'project': PROJECT, 'account_user_id': 'admin', 'identity': {'id': 'admin'},
            'observed_at': '2026-10-29T08:00:00Z', 'identity_observed_at': '2026-10-29T07:30:00Z',
            'membership_response': {'rpc': 'qry_membership_get_mine', 'params': {},
                                    'data': [{'user_id': 'admin', 'team_id': adapter.ZERO_TEAM, 'role': 'review-admin'}],
                                    'http_status': 200, 'error': None, 'completeness': {'complete': True}}}


def entry(rpc='qry_review_get_admin_queue_items_v6', params=None, data=None, method='POST', status=200):
    raw = json.dumps([copy.deepcopy(REVIEW)] if data is None else data, ensure_ascii=False)
    return {'request': {'method': method, 'url': HOST + '/rest/v1/rpc/' + rpc,
                        'headers': [{'name': 'authorization', 'value': 'Bearer SECRET_TOKEN_DO_NOT_COPY'}],
                        'cookies': [{'name': 'session', 'value': 'SECRET_COOKIE'}],
                        'postData': {'text': json.dumps(PARAMS if params is None else params)}},
            'response': {'status': status, 'headers': [{'name': 'set-cookie', 'value': 'SECRET_RESPONSE_COOKIE'}],
                         'cookies': [{'name': 'session', 'value': 'SECRET_RESPONSE_COOKIE'}],
                         'content': {'mimeType': 'application/json', 'size': len(raw.encode()), 'text': raw}}}


def write_har(root, entries, name='capture.har'):
    path = root / name
    path.write_text(json.dumps({'log': {'entries': entries}}))
    return path


def process_export():
    return {'schemaVersion': 1, 'operation': 'dataset process get', 'status': 'completed',
            'outcome': 'dataset_read', 'error': None,
            'context': {'environment': HOST, 'observation': {'source': 'remote', 'time': '2026-10-29T08:00:00Z'},
                        'effectiveInput': {'type': 'process', 'action': 'get', 'id': TARGET['id'], 'version': TARGET['version']}},
            'completeness': {'scope': 'one_dataset_version', 'returned': 1, 'hasMore': False, 'truncated': False},
            'data': {'subject': {'type': 'process', 'id': TARGET['id'], 'version': TARGET['version'],
                                 'stateCode': 20, 'modifiedAt': '2026-10-29T07:00:00Z'},
                     'content': {'processDataSet': {'processInformation': {'dataSetInformation': {'common:UUID': TARGET['id']}},
                                'administrativeInformation': {'publicationAndOwnership': {'common:dataSetVersion': TARGET['version']}}}}}}



class ReviewCaptureTests(unittest.TestCase):
    def test_exact_cli_process_body_is_current_json_evidence_only(self):
        capture = self.import_entries([entry()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'process-get.json'
            path.write_text(json.dumps(process_export()))
            attached = capture_module.attach_process_exports(capture, [path])
        self.assertEqual(capture['datasets'], [])
        self.assertEqual(attached['datasets'][0]['json'], process_export()['data']['content'])
        self.assertNotIn('json_ordered', attached['datasets'][0])
        self.assertFalse(attached['datasets'][0]['read_evidence']['native_fingerprint_basis_verified'])
        result = adapter.assess_capture(attached)
        self.assertEqual(result['facts']['reviews'][0]['current_body_basis'], 'json')
        self.assertIsNone(result['facts']['reviews'][0]['native_body_checksum'])
        self.assertNotIn('submitted_body_or_state_changed:review-1', result['evidence_gaps'])
        self.assertIn('native_body_checksum_unverified:review-1', result['evidence_gaps'])

    def test_unsupported_export_retains_original_without_claiming_current_body(self):
        mutations = [lambda r: r.update(status='partial'),
                     lambda r: r.update(schemaVersion=True),
                     lambda r: r.update(operation='dataset process list'),
                     lambda r: r['data']['subject'].update(version='01.01.002'),
                     lambda r: r['context']['effectiveInput'].update(version='latest'),
                     lambda r: r['context'].update(environment='https://otherproject.supabase.co'),
                     lambda r: r['context']['observation'].update(source='local'),
                     lambda r: r['context']['effectiveInput'].update(fields='exchanges'),
                     lambda r: r['completeness'].update(truncated=True),
                     lambda r: r['completeness'].update(hasMore=True),
                     lambda r: r['completeness'].update(completeContent=False),
                     lambda r: r['data']['subject'].update(stateCode=None),
                     lambda r: r['data'].update(content={}),
                     lambda r: r['data']['content']['processDataSet']['processInformation']['dataSetInformation'].update({'common:UUID': 'other'}),
                     lambda r: r['data']['content']['processDataSet']['administrativeInformation']['publicationAndOwnership'].update({'common:dataSetVersion': '01.01.002'})]
        capture = self.import_entries([entry()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'process-get.json'
            for i, mutation in enumerate(mutations):
                with self.subTest(case=i):
                    record = process_export()
                    mutation(record)
                    path.write_text(json.dumps(record))
                    result = capture_module.attach_process_exports(capture, [path])
                    self.assertEqual(result['datasets'], [])
                    original, = result['source']['export_provenance']
                    self.assertEqual(original['path'], str(path))
                    self.assertEqual(original['interpretation'], 'unavailable')
                    self.assertEqual(json.loads(path.read_bytes()), record)

    def test_native_workload_roster_retains_counts_without_implying_eligibility(self):
        params = {'p_page': 1, 'p_page_size': 10, 'p_sort_by': 'created_at', 'p_sort_order': 'desc', 'p_role': None}
        row = {'user_id': 'unassigned-expert', 'team_id': adapter.ZERO_TEAM, 'role': 'review-member',
               'pending_count': 3, 'reviewed_count': '7', 'total_count': 1}
        capture = self.import_entries([entry(), entry('qry_review_get_member_workload', params, [row])])
        self.assertEqual(capture['responses'][-1]['data'], [row])
        self.assertEqual(capture['responses'][-1]['completeness']['pagination']['page_size'], 10)
        self.assertNotIn('qualification_evidence', capture)
        params['p_page_size'] = 1
        row['total_count'] = 2
        partial = self.import_entries([entry(), entry('qry_review_get_member_workload', params, [row])])
        self.assertTrue(partial['responses'][-1]['completeness']['complete'])
        self.assertIn('pagination_not_closed:qry_review_get_member_workload', adapter.assess_capture(partial)['blocked_conditions'])
        for key, value in [('role', 'data_product_manager'), ('team_id', 'foreign-team'), ('pending_count', -1)]:
            invalid = {**row, key: value}
            with self.assertRaises(capture_module.CaptureError):
                self.import_entries([entry(), entry('qry_review_get_member_workload', params, [invalid])])

    def import_entries(self, entries, identity_record=None, scope=None):
        with tempfile.TemporaryDirectory() as directory:
            path = write_har(Path(directory).resolve(), entries)
            return capture_module.import_capture(identity_record or identity(), scope or SCOPE, [path])

    def test_imports_raw_params_rows_and_actual_v6_without_tokens(self):
        original = entry()
        capture = self.import_entries([original])
        self.assertEqual(capture['profile'], 'native')
        self.assertEqual(capture['contract_profile'], 'observed_v6')
        self.assertEqual(capture['responses'][1]['params'], PARAMS)
        self.assertEqual(capture['responses'][1]['data'], [REVIEW])
        self.assertEqual(capture['datasets'], [])
        self.assertEqual(capture['responses'][1]['completeness']['pagination'], {'page': 1, 'page_size': 50, 'total_count': 1})
        serialized = adapter.canonical_json(capture)
        for secret in ['SECRET_TOKEN_DO_NOT_COPY', 'SECRET_COOKIE', 'SECRET_RESPONSE_COOKIE', 'authorization', 'headers', 'cookies']:
            self.assertNotIn(secret, serialized)
        self.assertEqual(capture['source']['har_provenance'][0]['entry_count'], 1)
        self.assertNotIn('proposed_business_status', adapter.assess_capture(capture))

    def test_options_is_ignored_for_exact_read_endpoint(self):
        option = entry(method='OPTIONS')
        option['response']['content'] = {'size': 0}
        capture = self.import_entries([entry(), option])
        self.assertEqual(len(capture['responses']), 2)
        self.assertEqual(capture['source']['har_provenance'][0]['imported_read_count'], 1)

    def test_range_presence_is_incomplete_even_with_http_200(self):
        for side, header in [('request', 'rAnGe'), ('response', 'Content-Range')]:
            with self.subTest(side=side):
                value = entry('qry_review_get_comment_items', {'p_review_id': 'review-1', 'p_scope': 'all'}, [])
                value[side]['headers'].append({'name': header, 'value': 'RANGE_VALUE_NOT_COPIED'})
                capture = self.import_entries([value])
                response = capture['responses'][1]
                self.assertFalse(response['completeness']['complete'])
                self.assertTrue(response['completeness']['range_present'])
                self.assertEqual(response['completeness']['reason'], 'native_http_range_not_proven_complete')
                self.assertNotIn('RANGE_VALUE_NOT_COPIED', adapter.canonical_json(capture))
                self.assertIsNone(adapter.assess_capture(capture)['cursor'])

    def test_alias_resolves_original_and_credential_target_stays_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            native = root / 'native.sqlite'
            native.write_bytes(b'FAKE NATIVE STATE IN TEMP DIRECTORY ONLY')
            alias = root / 'alias.har'
            alias.symlink_to(native)
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read')):
                with self.assertRaises(capture_module.CaptureError):
                    capture_module._read(alias)
            source_dir = root / 'source'
            source_dir.mkdir()
            actual = write_har(source_dir, [entry()])
            link_dir = root / 'directory-alias'
            link_dir.symlink_to(source_dir, target_is_directory=True)
            self.assertEqual(capture_module._read(link_dir / actual.name), actual.read_bytes())

    def test_comments_exact_native_tuple_preserved(self):
        comment = {'review_id': 'review-1', 'reviewer_id': 'expert-1', 'state_code': 0,
                   'json': None, 'created_at': '2026-10-29T07:00:00Z', 'modified_at': '2026-10-29T07:00:00Z'}
        capture = self.import_entries([entry(), entry('qry_review_get_comment_items', {'p_review_id': 'review-1', 'p_scope': 'all'}, [comment])])
        self.assertEqual(capture['responses'][2]['data'], [comment])
        self.assertTrue(capture['responses'][2]['completeness']['complete'])

    def test_unknown_or_write_origins_endpoints_and_methods_rejected(self):
        changes = [('url', 'https://example.com/rest/v1/rpc/qry_review_get_items'),
                   ('url', 'http://' + PROJECT + '.supabase.co/rest/v1/rpc/qry_review_get_items'),
                   ('url', HOST + '/rest/v1/rpc/cmd_review_finalize_approve'),
                   ('url', HOST + '/rest/v1/processes'),
                   ('url', HOST + '/rest/v1/rpc/qry_unknown'),
                   ('url', HOST + '/rest/v1/rpc/qry_review_get_items?apikey=SECRET'),
                   ('url', 'https://user:secret@' + PROJECT + '.supabase.co/rest/v1/rpc/qry_review_get_items'),
                   ('method', 'PATCH')]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                value_entry = entry()
                value_entry['request'][field] = value
                with self.assertRaises(capture_module.CaptureError):
                    self.import_entries([value_entry])

    def test_unknown_params_and_credential_json_rejected(self):
        for params in [{**PARAMS, 'access_token': 'secret'}, {**PARAMS, 'unexpected': True}]:
            with self.assertRaises(capture_module.CaptureError):
                self.import_entries([entry(params=params)])

    def test_response_credential_fields_never_copied(self):
        row = {**REVIEW, 'access_token': 'RESPONSE_SECRET'}
        capture = self.import_entries([entry(data=[row])])
        self.assertNotIn('RESPONSE_SECRET', adapter.canonical_json(capture))
        self.assertFalse(capture['responses'][1]['completeness']['complete'])

    def test_get_parameters_parse_without_network(self):
        value = entry('qry_review_get_items', method='GET')
        value['request']['url'] += '?p_data_id=process-1&p_data_version=01.01.001'
        capture = self.import_entries([value])
        self.assertEqual(capture['responses'][1]['params'], {'p_data_id': 'process-1', 'p_data_version': '01.01.001'})
        value = entry(method='GET')
        value['request']['url'] += '?p_page=1&p_page_size=50&p_query=exact'
        self.assertEqual(self.import_entries([value])['responses'][1]['params']['p_page'], 1)

    def test_out_of_scope_native_rows_and_requests_rejected(self):
        row = {**REVIEW, 'id': 'other-review'}
        with self.assertRaises(capture_module.CaptureError):
            self.import_entries([entry(data=[row])])
        row = {**REVIEW, 'data_version': '01.01.002'}
        with self.assertRaises(capture_module.CaptureError):
            self.import_entries([entry(data=[row])])
        with self.assertRaises(capture_module.CaptureError):
            self.import_entries([entry('qry_review_get_comment_items', {'p_review_id': 'other-review', 'p_scope': 'all'}, [])])
        with self.assertRaises(capture_module.CaptureError):
            self.import_entries([entry('qry_review_get_items', {}, [])])

    def test_http_error_preserved_not_successful_empty_list(self):
        error = {'code': '42501', 'message': 'REVIEW_ADMIN_REQUIRED'}
        capture = self.import_entries([entry(data=error, status=403)])
        response = capture['responses'][1]
        self.assertEqual(response['error'], error)
        self.assertEqual(response['data'], [])
        self.assertFalse(response['completeness']['complete'])
        self.assertIsNone(adapter.assess_capture(capture)['cursor'])

    def test_missing_invalid_truncated_and_size_mismatch_bodies_incomplete(self):
        for mutation in (lambda v: v['response']['content'].pop('text'),
                         lambda v: v['response']['content'].update(text='[{'),
                         lambda v: v['response']['content'].update(_truncated=True),
                         lambda v: v['response']['content'].update(size=999999),
                         lambda v: v['response']['content'].update(encoding='gzip')):
            value = entry()
            mutation(value)
            response = self.import_entries([value])['responses'][1]
            self.assertFalse(response['completeness']['complete'])
            self.assertEqual(response['data'], [])

    def test_base64_native_json_decodes_losslessly(self):
        value = entry()
        raw = '[{"id":"review-1","data_id":"process-1","data_version":"01.01.001","target_table":"processes","state_code":1,"total_count":1,"json":{"amount":0.10}}]'.encode()
        value['response']['content'].update(encoding='base64', size=len(raw), text=base64.b64encode(raw).decode())
        capture = self.import_entries([value])
        self.assertEqual(capture['responses'][1]['data'][0]['json']['amount'], Decimal('0.10'))
        self.assertIn('0.10', adapter.canonical_json(capture))

    def test_pagination_missing_total_and_incomplete_page_cannot_claim_completion(self):
        for rows in [[], [{k: v for k, v in REVIEW.items() if k != 'total_count'}], [{**REVIEW, 'total_count': 51}]]:
            response = self.import_entries([entry(data=rows)])['responses'][1]
            self.assertFalse(response['completeness']['complete'])

    def test_valid_partial_page_still_does_not_advance_collection_cursor(self):
        rows = [{**REVIEW, 'id': 'review-' + str(i), 'total_count': 51} for i in range(50)]
        scope = copy.deepcopy(SCOPE)
        scope['review_ids'] = [r['id'] for r in rows]
        capture = self.import_entries([entry(data=rows)], scope=scope)
        self.assertTrue(capture['responses'][1]['completeness']['complete'])
        self.assertIsNone(adapter.assess_capture(capture)['cursor'])

    def test_membership_must_be_native_own_account(self):
        source = identity()
        source['membership_response']['data'][0]['user_id'] = 'other'
        with self.assertRaises(capture_module.CaptureError):
            self.import_entries([entry()], identity_record=source)

    def test_scope_extra_fields_and_identity_metadata_are_not_copied(self):
        source = identity()
        source['identity']['access_token'] = 'IDENTITY_SECRET'
        scope = copy.deepcopy(SCOPE)
        scope['headers'] = {'authorization': 'SCOPE_SECRET'}
        scope['targets'][0]['access_token'] = 'TARGET_SECRET'
        capture = self.import_entries([entry()], identity_record=source, scope=scope)
        for value in ('IDENTITY_SECRET', 'SCOPE_SECRET', 'TARGET_SECRET'):
            self.assertNotIn(value, adapter.canonical_json(capture))

    def test_malformed_page_count_is_incomplete_without_crash(self):
        capture = self.import_entries([entry(data=[{**REVIEW, 'total_count': []}])])
        self.assertFalse(capture['responses'][1]['completeness']['complete'])

    def test_malformed_har_log_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'bad.har'
            path.write_text('{"log":[]}')
            with self.assertRaises(capture_module.CaptureError):
                capture_module.import_capture(identity(), SCOPE, [path])
        source = identity()
        source['identity']['id'] = 'other'
        with self.assertRaises(capture_module.CaptureError):
            self.import_entries([entry()], identity_record=source)

    def test_duplicate_keys_and_nonfinite_params_rejected(self):
        for text in ['{"p_page":1,"p_page":2}', '{"p_page":NaN}']:
            value = entry()
            value['request']['postData']['text'] = text
            with self.assertRaises(ValueError):
                self.import_entries([value])

    def test_cli_writes_once_and_preserves_output_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            har = write_har(root, [entry()])
            (root / 'identity.json').write_text(json.dumps(identity()))
            (root / 'scope.json').write_text(json.dumps(SCOPE))
            (root / 'process-get.json').write_text(json.dumps(process_export()))
            output = root / 'new-capture.json'
            argv = ['review_capture.py', '--identity', str(root / 'identity.json'), '--scope', str(root / 'scope.json'),
                    '--har', str(har), '--process-get', str(root / 'process-get.json'), '--output', str(output)]
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
                capture_module.main()
            original = output.read_bytes()
            with patch.object(sys, 'argv', argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                capture_module.main()
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(adapter.load_capture(output)['profile'], 'native')
            self.assertEqual(adapter.load_capture(output)['datasets'][0]['json'], process_export()['data']['content'])


if __name__ == '__main__':
    unittest.main()
