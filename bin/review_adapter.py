#!/usr/bin/env python3
"""Assess frozen native review reads; never connects, writes a board, or runs commands.

Capture provenance is descriptive evidence, not an authorization or authenticity proof.
Native current rows remain authoritative. This module deliberately cannot submit,
assign, decide, publish, or turn a synthetic capture into a live transition receipt.
"""
import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 'data-workspace.native-review-capture.v1'
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
TABLES = {'contacts', 'sources', 'unitgroups', 'flowproperties', 'flows', 'processes', 'lifecyclemodels'}
REVIEW_RPCS = {'qry_review_get_items', 'qry_review_admin_queue_items_v2',
               'qry_review_member_queue_items_v2', 'qry_review_get_admin_queue_items_v4',
               'qry_review_get_member_queue_items_v4', 'qry_review_get_admin_queue_items_v6',
               'qry_review_get_member_queue_items_v6'}
RPCLIST = REVIEW_RPCS | {'qry_review_get_comment_items', 'qry_membership_get_mine',
                       'qry_review_get_member_list', 'qry_review_get_member_workload',
                       'qry_root_review_reference_progress_v2'}
COMMENT_STATES = {-3: 'reviewer_rejected', -2: 'revoked', -1: 'admin_rejected_closed',
                  0: 'pending', 1: 'reviewer_approved', 2: 'admin_approved_closed'}
REVIEW_STATES = {-1: 'admin_rejected', 0: 'unassigned', 1: 'assigned', 2: 'admin_approved'}
ZERO_TEAM = '00000000-0000-0000-0000-000000000000'


def canonical_json(value):
    """Source review_canonical_json_text_v1 format; Decimal retains JSONB scale."""
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValueError('JSON object keys must be strings')
        return '{' + ','.join(canonical_json(k) + ':' + canonical_json(value[k]) for k in sorted(value)) + '}'
    if isinstance(value, list):
        return '[' + ','.join(canonical_json(v) for v in value) + ']'
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Nonfinite JSON number')
        # PostgreSQL numeric suppresses the negative sign of zero.
        return format(abs(value) if value.is_zero() else value, 'f')
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def load_capture(path):
    """Load a raw exported capture without losing decimal scale or accepting NaN."""
    def invalid(value):
        raise ValueError('Nonfinite JSON value: ' + value)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError('Capture is missing or exceeds 16 MiB')
    return json.loads(path.read_bytes(), parse_float=Decimal, parse_constant=invalid,
                      object_pairs_hook=unique_object)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _key(value):
    if not isinstance(value, dict):
        return (None, None, None)
    return tuple(v if isinstance(v, str) else None for v in
                 (value.get('table'), value.get('id'), value.get('version')))


def _target(row):
    snapshot = row.get('json', {}).get('data', {}) if isinstance(row.get('json'), dict) else {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    return _key({'table': row.get('target_table') or snapshot.get('table'),
                 'id': row.get('data_id') or snapshot.get('id'),
                 'version': row.get('data_version') or snapshot.get('version')})


def native_body_checksum(body):
    """Return source-compatible checksum only where JSON number spelling is lossless.

    Python float parsing loses JSONB decimal scale and exponent notation. Use
    load_capture for exported JSON; already parsed float bodies stay unverified.
    """
    def lossless(value):
        if isinstance(value, float):
            return False
        if isinstance(value, Decimal):
            return value.is_finite()
        if isinstance(value, dict):
            return all(isinstance(k, str) and lossless(v) for k, v in value.items())
        if isinstance(value, list):
            return all(lossless(v) for v in value)
        return value is None or type(value) in (str, int, bool)
    return digest(body) if lossless(body) else None


def _state(row):
    return row.get('state_code', row.get('review_state_code'))


def _is_sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _validate_capture(capture):
    """Return structural errors. Valid structure alone does not prove completeness."""
    errors = []
    if not isinstance(capture, dict):
        return ['capture must be an object']
    if capture.get('schema') != SCHEMA:
        errors.append('unknown capture schema')
    if capture.get('profile') not in ('native', 'synthetic'):
        errors.append('profile must be native or synthetic')
    if capture.get('contract_profile') not in ('source_v2', 'observed_v6'):
        errors.append('unknown native contract profile')
    source = capture.get('source')
    if not isinstance(source, dict):
        errors.append('source must be an object')
        source = {}
    for field in ('project', 'account_user_id'):
        if not isinstance(source.get(field), str) or not source[field].strip():
            errors.append('source.' + field + ' required')
    if source.get('origin') not in ('browser_network_export', 'official_cli_export'):
        errors.append('unsupported capture origin')
    if not isinstance(source.get('identity'), dict) or source['identity'].get('id') != source.get('account_user_id'):
        errors.append('identity does not match captured account')
    for field in ('observed_at', 'identity_observed_at'):
        if not _time(source.get(field)):
            errors.append('source.' + field + ' needs timezone')
    scope = capture.get('scope', {})
    if not isinstance(scope, dict):
        errors.append('scope must be an object')
        scope = {}
    for name in ('targets', 'closure_targets'):
        rows = scope.get(name, [])
        if not isinstance(rows, list) or (name == 'targets' and not rows):
            errors.append('scope.' + name + ' must be a nonempty target list' if name == 'targets' else 'invalid closure list')
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get('table') not in TABLES or not row.get('id') or not row.get('version'):
                errors.append('invalid exact target in ' + name)
        keys = [_key(row) for row in rows if isinstance(row, dict)]
        if len(keys) != len(set(keys)):
            errors.append('duplicate exact target in ' + name)
    ids = scope.get('review_ids')
    if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        errors.append('scope.review_ids must be distinct explicit IDs')
    responses = capture.get('responses')
    if not isinstance(responses, list):
        errors.append('responses must be a list')
        responses = []
    for response in responses:
        if not isinstance(response, dict):
            errors.append('response must be an object')
            continue
        rpc = response.get('rpc')
        if rpc not in RPCLIST:
            errors.append('RPC is not a qualified read: ' + str(rpc))
        if isinstance(rpc, str) and rpc.endswith('_v6') and capture.get('contract_profile') != 'observed_v6':
            errors.append('v6 response needs observed_v6 profile')
        if not isinstance(response.get('params'), dict):
            errors.append('RPC params required')
        if not isinstance(response.get('data'), list):
            errors.append('native RPC data must be an array')
        elif any(not isinstance(row, dict) for row in response['data']):
            errors.append('native RPC rows must be objects')
        else:
            rows = response['data']
            if rpc in REVIEW_RPCS:
                ids_in_response = [row.get('id') for row in rows]
                if len(ids_in_response) != len(set(ids_in_response)):
                    errors.append('duplicate review row within one response')
                for row in rows:
                    if not isinstance(row.get('id'), str) or type(_state(row)) is not int:
                        errors.append('invalid native review identity/state')
                    if 'state_code' in row and 'review_state_code' in row and row['state_code'] != row['review_state_code']:
                        errors.append('contradictory native review state aliases')
                    if 'json' in row and not isinstance(row['json'], dict):
                        errors.append('native review json must be an object')
                    elif isinstance(row.get('json'), dict) and any(k in row['json'] and not isinstance(row['json'][k], dict) for k in ('data', 'user')):
                        errors.append('invalid review data/user snapshot')
            elif rpc == 'qry_review_get_comment_items':
                keys = [(row.get('review_id'), row.get('reviewer_id')) for row in rows]
                if len(keys) != len(set(keys)):
                    errors.append('duplicate comment key within one response')
            elif rpc == 'qry_membership_get_mine':
                if any(not isinstance(row.get('role'), str) for row in rows):
                    errors.append('invalid membership row')
        if not isinstance(response.get('completeness'), dict):
            errors.append('response completeness envelope required')
        if type(response.get('http_status')) is not int:
            errors.append('response HTTP status required')
    datasets = capture.get('datasets', [])
    if not isinstance(datasets, list):
        errors.append('datasets must be a list')
    else:
        for row in datasets:
            if not isinstance(row, dict) or row.get('table') not in TABLES or not row.get('id') or not row.get('version'):
                errors.append('invalid exact dataset read')
            elif type(row.get('state_code')) is not int or not _time(row.get('observed_at')):
                errors.append('dataset state/time required')
            elif not isinstance(row.get('json_ordered', row.get('json')), dict):
                errors.append('complete dataset body required')
    return errors


def validate_capture(capture, *, expected_project=None, expected_account_user_id=None):
    """Fail closed for malformed input and optionally assert caller's current scope."""
    try:
        errors = _validate_capture(capture)
        if isinstance(capture, dict) and isinstance(capture.get('source'), dict):
            for field, expected in (('project', expected_project), ('account_user_id', expected_account_user_id)):
                if expected is not None and capture['source'].get(field) != expected:
                    errors.append('unexpected source.' + field)
        return errors
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return ['malformed capture structure']


def _complete(responses, blocked):
    """Require successful collection, including every declared page, before cursors."""
    groups = {}
    for response in responses:
        rpc = response['rpc']
        if response.get('error') or response.get('http_status') != 200:
            blocked.append('read_failed:' + rpc)
        completeness = response['completeness']
        if completeness.get('complete') is not True:
            blocked.append('read_incomplete:' + rpc)
        params = {k: v for k, v in response['params'].items() if k not in ('p_page', 'p_page_size')}
        group = groups.setdefault((rpc, digest(params)), [])
        group.append(response)
    for (rpc, _), group in groups.items():
        pages = [r['completeness'].get('pagination') for r in group]
        if any(p is not None for p in pages):
            if any(not isinstance(p, dict) for p in pages):
                blocked.append('pagination_metadata_missing:' + rpc)
                continue
            try:
                sizes = {p['page_size'] for p in pages}
                totals = {p['total_count'] for p in pages}
                numbers = [p['page'] for p in pages]
                size = next(iter(sizes))
                total = next(iter(totals))
                valid = (len(sizes) == len(totals) == 1 and type(size) is int and 1 <= size <= 100
                         and type(total) is int and total >= 0 and all(type(n) is int for n in numbers)
                         and sorted(numbers) == list(range(1, max(1, (total + size - 1) // size) + 1))
                         and sum(len(r['data']) for r in group) == total)
                for r, p in zip(group, pages):
                    if r['params'].get('p_page', 1) != p['page'] or r['params'].get('p_page_size', size) != size:
                        valid = False
                    if any(int(row.get('total_count', total)) != total for row in r['data']):
                        valid = False
                if not valid:
                    blocked.append('pagination_not_closed:' + rpc)
            except (KeyError, TypeError, ValueError, StopIteration):
                blocked.append('pagination_invalid:' + rpc)
        elif rpc not in ('qry_review_get_items', 'qry_review_get_comment_items', 'qry_membership_get_mine', 'qry_root_review_reference_progress_v2'):
            blocked.append('pagination_metadata_missing:' + rpc)
        elif any(len(r['data']) >= 1000 for r in group):
            blocked.append('possible_server_row_limit:' + rpc)
        elif len({digest(r['data']) for r in group}) > 1:
            blocked.append('unstable_read:' + rpc)


def _approved_checksum(review):
    direct = review.get('approved_revision_checksum')
    if _is_sha(direct):
        return direct
    logs = review.get('json', {}).get('logs', []) if isinstance(review.get('json'), dict) else []
    for log in reversed(logs if isinstance(logs, list) else []):
        if isinstance(log, dict) and log.get('action') == 'approved':
            value = log.get('approved_revision_checksum')
            if _is_sha(value):
                return value
    return None


def _assess_capture(capture, previous=None, *, expected_project=None, expected_account_user_id=None):
    """Read native observations; scientific decisions remain with the responsible agent."""
    blocked = validate_capture(capture, expected_project=expected_project, expected_account_user_id=expected_account_user_id)
    result = {'schema': 'data-workspace.review-observation.v1', 'profile': capture.get('profile') if isinstance(capture, dict) else None,
              'blocked_conditions': blocked, 'facts': {}, 'evidence_gaps': [],
              'opinion_changes': [], 'cursor': None, 'live_transition_authority': False}
    if not isinstance(capture, dict):
        return result
    # A broken envelope limits this interpretation. Independent readable rows
    # remain evidence; normalization affects only this view, never the original.
    def object_value(value):
        return value if isinstance(value, dict) else {}
    def object_rows(value):
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    source = object_value(capture.get('source'))
    source = {**source, 'account_user_id': source.get('account_user_id'),
              'project': source.get('project'), 'observed_at': source.get('observed_at')}
    scope = object_value(capture.get('scope'))
    scope = {**scope, 'targets': object_rows(scope.get('targets')),
             'closure_targets': object_rows(scope.get('closure_targets')),
             'review_ids': [v for v in scope.get('review_ids', []) if isinstance(v, str)]
                           if isinstance(scope.get('review_ids'), list) else []}
    responses = [{**r, 'rpc': r.get('rpc') if isinstance(r.get('rpc'), str) else '<unreadable RPC>', 'params': object_value(r.get('params')),
                  'data': object_rows(r.get('data')), 'completeness': object_value(r.get('completeness')),
                  'http_status': r.get('http_status')} for r in object_rows(capture.get('responses'))]
    capture = {**capture, 'profile': capture.get('profile'), 'source': source, 'scope': scope,
               'responses': responses, 'datasets': object_rows(capture.get('datasets'))}
    _complete(responses, blocked)
    if not scope['review_ids']:
        blocked.append('review_scope_unresolved:no_current_review_ids')
    account = source['account_user_id']
    targets = {_key(t) for t in scope['targets']}
    closure = {_key(t) for t in scope.get('closure_targets', [])}
    if not closure or not targets.issubset(closure):
        blocked.append('declared_publication_closure_missing')
    reviews, comments, memberships, reviewer_memberships = {}, {}, [], []
    comment_reads = {}
    reference_sets = {}
    for response in responses:
        rpc, rows = response['rpc'], response['data']
        if rpc in REVIEW_RPCS:
            for row in rows:
                if not isinstance(row.get('id'), str) or type(_state(row)) is not int:
                    continue
                if row.get('id') not in scope['review_ids']:
                    blocked.append('out_of_scope_review:' + str(row.get('id')))
                    continue
                if _target(row) not in targets | closure:
                    blocked.append('review_target_out_of_scope:' + str(row.get('id')))
                existing = reviews.get(row['id'])
                if existing and ((_state(existing) is not None and _state(row) is not None and _state(existing) != _state(row))
                                 or (_target(existing) != _target(row) and all(_target(existing)) and all(_target(row)))
                                 or any(existing.get(k) is not None and row.get(k) is not None and existing[k] != row[k]
                                        for k in ('reviewer_id', 'modified_at', 'submitted_revision_checksum', 'json'))):
                    blocked.append('unstable_review_read:' + row['id'])
                reviews[row['id']] = {**(existing or {}), **row}
        elif rpc == 'qry_review_get_comment_items':
            review_id = response['params'].get('p_review_id')
            if not isinstance(review_id, str):
                blocked.append('invalid_comment_read_identity')
                continue
            if review_id not in scope['review_ids']:
                blocked.append('out_of_scope_comment_read')
            scope_value = response['params'].get('p_scope')
            comment_reads[review_id] = scope_value.lower() if isinstance(scope_value, str) else 'auto'
            for row in rows:
                key = (row.get('review_id'), row.get('reviewer_id'))
                if not all(isinstance(v, str) and v for v in key) or key[0] != review_id or type(row.get('state_code')) is not int or row.get('state_code') not in COMMENT_STATES or not _time(row.get('modified_at')):
                    blocked.append('invalid_comment_binding')
                    continue
                if key in comments and comments[key] != row:
                    blocked.append('unstable_comment_read:' + str(key))
                comments[key] = row
        elif rpc == 'qry_membership_get_mine':
            memberships.extend(rows)
        elif rpc in ('qry_review_get_member_list', 'qry_review_get_member_workload'):
            reviewer_memberships.extend(rows)
        elif rpc == 'qry_root_review_reference_progress_v2':
            root = response['params'].get('p_root_review_id')
            if not isinstance(root, str):
                blocked.append('invalid_reference_read_identity')
                continue
            if root not in scope['review_ids']:
                blocked.append('out_of_scope_reference_read')
            if root in reference_sets and digest(reference_sets[root]) != digest(rows):
                blocked.append('unstable_reference_read:' + str(root))
            reference_sets[root] = rows
    for review_id in scope['review_ids']:
        if review_id not in reviews:
            blocked.append('review_missing_or_rls_hidden:' + review_id)
    for row in memberships:
        if row.get('user_id') != account:
            blocked.append('membership_account_mismatch')
    admin = any(r.get('user_id') == account and r.get('team_id') == ZERO_TEAM and r.get('role') == 'review-admin' for r in memberships)
    datasets = {}
    for row in capture.get('datasets', []):
        key = _key(row)
        if key not in targets | closure:
            blocked.append('dataset_out_of_scope:' + str(key))
        if key in datasets and {k: v for k, v in datasets[key].items() if k != 'observed_at'} != {k: v for k, v in row.items() if k != 'observed_at'}:
            blocked.append('unstable_dataset_read:' + str(key))
        datasets[key] = row
    opinion_facts = []
    for key, row in sorted(comments.items()):
        opinion_facts.append({'opinion_key': list(key), 'state_code': row['state_code'],
                              'meaning': COMMENT_STATES[row['state_code']], 'modified_at': row['modified_at'],
                              'content_sha256': digest(row.get('json')), 'observation_sha256': digest(row),
                              'prior_reviewer_vote': row['state_code'] if row['state_code'] in (1, -3) else None})
    review_facts = []
    for review_id, review in reviews.items():
        target = _target(review)
        state = _state(review)
        review_json = object_value(review.get('json'))
        review_kind = review.get('review_kind', review_json.get('review_kind'))
        if review_kind not in ('root', 'reference'):
            blocked.append('legacy_or_unknown_review_kind:' + review_id)
        if type(state) is not int or state not in REVIEW_STATES:
            blocked.append('invalid_review_state:' + review_id)
        owner = object_value(review_json.get('user')).get('id')
        if review_id not in comment_reads or comment_reads.get(review_id) in ('mine', 'self') or not (admin or owner == account):
            blocked.append('all_review_comments_not_proven_visible:' + review_id)
        ids = review.get('reviewer_id')
        if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            blocked.append('invalid_reviewer_ids:' + review_id)
            ids = []
        active = [i for i in ids if (review_id, i) in comments and comments[(review_id, i)]['state_code'] != -2]
        if state == 1:
            if any(comments[(review_id, i)]['state_code'] not in (0, 1, -3) for i in active):
                blocked.append('native_state_inconsistent_closed_comment_on_active_review:' + review_id)
            active = [i for i in active if comments[(review_id, i)]['state_code'] in (0, 1, -3)]
        if set(active) != set(ids):
            blocked.append('assignment_comment_binding_incomplete:' + review_id)
        # A draft assignment list has no effective pending comments/state.
        if state == 0:
            active = []
        reviewer_roles = {}
        for member in active:
            roles = sorted({r.get('role') for r in reviewer_memberships if r.get('user_id') == member
                            and r.get('team_id') == ZERO_TEAM and r.get('role') in ('review-member', 'review-admin')})
            reviewer_roles[member] = roles
            if not roles:
                blocked.append('reviewer_membership_not_verified:' + review_id + ':' + member)
        dataset = datasets.get(target)
        if dataset is None:
            blocked.append('exact_target_body_missing:' + review_id)
        submitted = review.get('submitted_revision_checksum')
        if not _is_sha(submitted):
            blocked.append('native_submitted_checksum_missing:' + review_id)
        approved = _approved_checksum(review)
        # The native SQL prefers json_ordered. A read of json alone cannot prove
        # that it equals the stored fingerprint basis, even with lossless JSON.
        current_body = dataset.get('json_ordered', dataset.get('json')) if dataset else None
        body_basis = ('json_ordered' if dataset and isinstance(dataset.get('json_ordered'), dict)
                      else 'json' if dataset else None)
        body_digest = native_body_checksum(current_body) if dataset else None
        body_native_checksum = body_digest if body_basis == 'json_ordered' else None
        if not _is_sha(body_native_checksum):
            blocked.append('native_body_checksum_unverified:' + review_id)
        comparison = ('missing_submitted_checksum' if not _is_sha(submitted)
                      else 'current_native_checksum_unverified' if not _is_sha(body_native_checksum)
                      else 'match' if submitted == body_native_checksum else 'mismatch')
        if state in (0, 1) and dataset and (dataset.get('state_code') != 20 or comparison == 'mismatch'):
            blocked.append('submitted_body_or_state_changed:' + review_id)
        if state == 2 and (not dataset or dataset.get('state_code') != 100 or not approved or approved != body_native_checksum):
            blocked.append('approved_publication_body_not_verified:' + review_id)
        if state == -1 and (not dataset or dataset.get('state_code') != 0):
            blocked.append('rejected_target_not_editable:' + review_id)
        if state == -1 and any(_target(r) == target and _state(r) in (0, 1) for rid, r in reviews.items() if rid != review_id):
            blocked.append('another_active_review:' + review_id)
        if state == -1:
            full_target_read = any(r['rpc'] == 'qry_review_get_items'
                                   and r['params'].get('p_review_ids') is None
                                   and r['params'].get('p_data_id') == target[1]
                                   and r['params'].get('p_data_version') == target[2]
                                   and r['params'].get('p_state_codes') is None for r in responses)
            if not full_target_read:
                blocked.append('other_active_reviews_not_enumerated:' + review_id)
        if review_kind == 'root':
            if review_id not in reference_sets:
                blocked.append('current_reference_closure_not_read:' + review_id)
            elif any(_target(r) not in closure for r in reference_sets[review_id]):
                blocked.append('reference_outside_declared_closure:' + review_id)
            else:
                for child in reference_sets[review_id]:
                    child_id = child.get('reference_review_id')
                    native = reviews.get(child_id) if isinstance(child_id, str) else None
                    child_target = (child.get('target_table'), child.get('data_id'), child.get('data_version'))
                    if (not native or _target(native) != child_target
                            or native.get('submitted_revision_checksum') != child.get('submitted_revision_checksum')
                            or _state(native) != child.get('state_code')):
                        blocked.append('reference_review_binding_mismatch:' + review_id)
        review_facts.append({'review_id': review_id, 'target': list(target), 'state_code': state,
                             'meaning': REVIEW_STATES.get(state), 'active_reviewer_ids': active,
                             'reviewer_roles': reviewer_roles,
                             'submitted_revision_checksum': submitted,
                             'submitted_checksum_observation': ('field_absent' if 'submitted_revision_checksum' not in review
                                                                else 'reported_null' if submitted is None else 'reported_value'),
                             'current_body_sha256': body_digest, 'current_body_basis': body_basis,
                             'native_body_checksum': body_native_checksum,
                             'submitted_body_comparison': comparison,
                             'approved_revision_checksum': approved, 'target_state_code': dataset.get('state_code') if dataset else None})
    if targets != {_target(r) for r in reviews.values() if _target(r) in targets}:
        blocked.append('target_review_coverage_incomplete')
    for target in closure:
        if target not in datasets or not any(_target(r) == target for r in reviews.values()):
            blocked.append('closure_member_not_read:' + str(target))
    if previous is not None:
        previous_errors = validate_capture(previous)
        if previous_errors or previous.get('profile') != capture['profile'] or previous['source']['project'] != source['project'] or previous['source']['account_user_id'] != account or previous['scope'] != scope:
            blocked.append('previous_capture_binding_mismatch')
        elif _time(previous['source']['observed_at']) >= _time(source['observed_at']):
            blocked.append('previous_capture_not_older')
        else:
            previous_observation = assess_capture(previous)
            if previous_observation['cursor'] is None:
                blocked.append('previous_capture_incomplete')
            old = previous_observation['facts'].get('opinions', [])
            before = {tuple(o['opinion_key']): o for o in old}
            now = {tuple(o['opinion_key']): o for o in opinion_facts}
            for key in sorted(before.keys() | now.keys()):
                change = ('new' if key not in before else 'removed_uncertain' if key not in now else
                          'modified' if before[key]['observation_sha256'] != now[key]['observation_sha256'] else None)
                if change:
                    result['opinion_changes'].append({'opinion_key': list(key), 'change': change})
                if change == 'removed_uncertain':
                    blocked.append('opinion_disappeared_without_native_withdrawal:' + str(key))
    # A successful read cursor records collection completion, not science acceptance.
    evidence_gaps = ('reviewer_membership_not_verified:',
                    'declared_publication_closure_missing', 'exact_target_body_missing:',
                    'native_submitted_checksum_missing:', 'native_body_checksum_unverified:',
                    'submitted_body_or_state_changed:', 'approved_publication_body_not_verified:',
                    'rejected_target_not_editable:', 'another_active_review:',
                    'other_active_reviews_not_enumerated:', 'current_reference_closure_not_read:',
                    'reference_outside_declared_closure:', 'reference_review_binding_mismatch:',
                    'closure_member_not_read:')
    collection_blocked = any(not b.startswith(evidence_gaps) for b in blocked)
    result['collection_errors'] = sorted(set(b for b in blocked if not b.startswith(evidence_gaps)))
    result['evidence_gaps'] = sorted(set(b for b in blocked if b.startswith(evidence_gaps)))
    if not collection_blocked and responses:
        result['cursor'] = {'observed_at': source['observed_at'], 'capture_sha256': digest(capture),
                            'profile': capture['profile'], 'scope_sha256': digest(scope)}
    collection_reads = []
    for response in responses:
        error = response.get('error')
        error_code = error.get('code') if isinstance(error, dict) else None
        local_error = (error_code == 'HAR_READ_INCOMPLETE'
                       and error.get('message') == response['completeness'].get('reason'))
        collection_reads.append({'rpc': response['rpc'], 'http_status': response['http_status'],
                                 'collector_complete': response['completeness'].get('complete') is True,
                                 'error_code': error_code,
                                 'error_origin': ('local_capture_validation' if local_error
                                                  else 'native_response' if error else None)})
    result['facts'] = {'reviews': review_facts, 'opinions': opinion_facts,
                       'collection_reads': collection_reads,
                       'account_roles': [r.get('role') for r in memberships],
                       'reviewer_memberships': reviewer_memberships}
    result['blocked_conditions'] = result['collection_errors']
    return result


def assess_capture(capture, previous=None, *, expected_project=None, expected_account_user_id=None):
    try:
        return _assess_capture(capture, previous, expected_project=expected_project,
                               expected_account_user_id=expected_account_user_id)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
        return {'schema': 'data-workspace.review-observation.v1',
                'profile': capture.get('profile') if isinstance(capture, dict) else None,
                'blocked_conditions': ['malformed capture: ' + type(error).__name__],
                'facts': {}, 'opinion_changes': [], 'cursor': None, 'live_transition_authority': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--previous', type=Path)
    args = parser.parse_args()
    capture = load_capture(args.capture)
    previous = load_capture(args.previous) if args.previous else None
    print(json.dumps(assess_capture(capture, previous), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
