#!/usr/bin/env python3
"""Import sanitized, local HAR reads into a workspace-owned review capture.

This module never makes requests, opens browser sessions, or invokes commands.
Raw headers, cookies, URLs and HAR bodies are never copied into the output.
Only allowlisted RPC parameters and decoded native JSON results are retained.
"""
import argparse
import base64
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

import review_adapter as adapter

MAX_CAPTURE_BYTES = adapter.MAX_CAPTURE_BYTES
PARAMS = {
    'qry_review_get_items': {'p_review_ids', 'p_data_id', 'p_data_version', 'p_state_codes'},
    'qry_review_get_comment_items': {'p_review_id', 'p_scope'},
    'qry_membership_get_mine': set(),
    'qry_review_get_member_list': {'p_page', 'p_page_size', 'p_sort_by', 'p_sort_order', 'p_role'},
    'qry_review_get_member_workload': {'p_page', 'p_page_size', 'p_sort_by', 'p_sort_order', 'p_role'},
    'qry_root_review_reference_progress_v2': {'p_root_review_id'},
}
QUEUE_PARAMS = {'p_status', 'p_page', 'p_page_size', 'p_sort_by', 'p_sort_order', 'p_display_mode',
                'p_target_table', 'p_query'}
for _rpc in adapter.REVIEW_RPCS - {'qry_review_get_items'}:
    PARAMS[_rpc] = {'p_status', 'p_page', 'p_page_size'} if _rpc.endswith('_v2') else QUEUE_PARAMS
SENSITIVE = {'authorization', 'cookie', 'set-cookie', 'cookies', 'headers', 'apikey', 'api_key',
             'access_token', 'refresh_token', 'id_token', 'client_secret', 'password', 'service_role_key'}


class CaptureError(ValueError):
    pass


def _json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CaptureError('Duplicate JSON object key')
            result[key] = value
        return result
    def invalid(_value):
        raise CaptureError('Nonfinite JSON number')
    return json.loads(text, parse_float=Decimal, parse_constant=invalid, object_pairs_hook=unique)


def _read(path):
    path = Path(path).resolve()
    if any(p.startswith('.env') or p in ('.secrets', '.state', '.runtime', '.foundry', '.toolchain')
           or p.endswith(('.sqlite', '.sqlite-wal', '.sqlite-shm')) for p in path.parts):
        raise CaptureError('Credential and native-state paths are not capture inputs')
    if not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
        raise CaptureError('Input is missing or exceeds 16 MiB')
    return path.read_bytes()


def _sensitive(value):
    if isinstance(value, dict):
        return any(str(k).lower().replace('-', '_') in {s.replace('-', '_') for s in SENSITIVE}
                   or _sensitive(v) for k, v in value.items())
    return isinstance(value, list) and any(_sensitive(v) for v in value)


def _request(entry, project):
    request = entry.get('request')
    if not isinstance(request, dict):
        raise CaptureError('Malformed HAR request')
    method = request.get('method')
    url = urlsplit(request.get('url', ''))
    host = project + '.supabase.co'
    if (url.scheme != 'https' or url.netloc != host or url.fragment
            or url.username is not None or url.password is not None):
        raise CaptureError('HAR request origin is outside the exact project')
    prefix = '/rest/v1/rpc/'
    rpc = url.path[len(prefix):] if url.path.startswith(prefix) else ''
    if rpc not in adapter.RPCLIST or '/' in rpc or rpc not in PARAMS:
        raise CaptureError('HAR endpoint is not an allowlisted read RPC')
    query = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
    if any(key not in PARAMS[rpc] for key, _value in query):
        raise CaptureError('Unexpected or credential-bearing query parameter')
    if method == 'OPTIONS':
        if query:
            raise CaptureError('Unexpected preflight query')
        return None
    if method not in ('GET', 'POST'):
        raise CaptureError('HAR method is not an allowed read transport')
    if method == 'POST':
        if query:
            raise CaptureError('POST RPC query parameters are not allowed')
        post = request.get('postData', {})
        if not isinstance(post, dict) or not isinstance(post.get('text'), str):
            raise CaptureError('POST RPC JSON parameters are missing')
        params = _json(post['text'])
        if not isinstance(params, dict):
            raise CaptureError('POST RPC parameters must be an object')
    else:
        params = {}
        for key, raw in query:
            if key in params:
                raise CaptureError('Duplicate RPC parameter')
            if raw.startswith(('{', '[', '"')) or raw in ('null', 'true', 'false') or re.fullmatch(r'-?\d+', raw):
                try:
                    value = _json(raw)
                except (ValueError, TypeError):
                    value = raw
            else:
                value = raw
            params[key] = value
    if set(params) - PARAMS[rpc] or _sensitive(params):
        raise CaptureError('Unexpected or credential-bearing RPC parameters')
    return rpc, params


def _response(entry, rpc, params):
    response = entry.get('response', {})
    if not isinstance(response, dict):
        raise CaptureError('Malformed HAR response')
    status = response.get('status')
    if type(status) is not int:
        raise CaptureError('Native HTTP status is missing')
    content = response.get('content', {})
    output = {'rpc': rpc, 'params': params, 'data': [], 'http_status': status, 'error': None,
              'completeness': {'complete': False}}
    incomplete = None
    def has_header(container, wanted):
        headers = container.get('headers', []) if isinstance(container, dict) else []
        if isinstance(headers, dict):
            return any(isinstance(name, str) and name.lower() == wanted for name in headers)
        return isinstance(headers, list) and any(isinstance(header, dict)
                                                 and isinstance(header.get('name'), str)
                                                 and header['name'].lower() == wanted for header in headers)
    range_present = (has_header(entry.get('request'), 'range')
                     or has_header(response, 'content-range'))
    data = None
    try:
        if not isinstance(content, dict) or not isinstance(content.get('text'), str):
            raise CaptureError('Native response body is missing')
        text = content['text']
        if content.get('encoding') == 'base64':
            raw = base64.b64decode(text, validate=True)
        elif content.get('encoding') in (None, ''):
            raw = text.encode()
        else:
            raise CaptureError('Unsupported HAR body encoding')
        if len(raw) > MAX_CAPTURE_BYTES:
            raise CaptureError('Native response body exceeds bound')
        if any(container.get(key) for container in (entry, response, content)
               for key in ('_truncated', 'truncated', '_error')):
            raise CaptureError('HAR marks response incomplete')
        if type(content.get('size')) is int and content['size'] >= 0 and content['size'] != len(raw):
            raise CaptureError('Native response body size does not match HAR')
        data = _json(raw)
        if _sensitive(data):
            raise CaptureError('Native JSON contains credential fields')
    except (ValueError, TypeError, UnicodeError, CaptureError):
        data = None
        incomplete = 'missing_invalid_or_truncated_native_body'
    if data is not None:
        if isinstance(data, list) and all(isinstance(row, dict) for row in data):
            output['data'] = data
        elif isinstance(data, dict) and status != 200:
            output['error'] = data
        else:
            incomplete = 'native_rpc_result_is_not_row_array'
    if status != 200:
        incomplete = incomplete or 'native_http_read_failed'
    if range_present:
        incomplete = incomplete or 'native_http_range_not_proven_complete'
        output['completeness']['range_present'] = True
    if 'p_page' in params or 'p_page_size' in params or rpc not in {
            'qry_review_get_items', 'qry_review_get_comment_items', 'qry_membership_get_mine',
            'qry_root_review_reference_progress_v2'}:
        default_size = 10 if rpc in ('qry_review_get_member_list', 'qry_review_get_member_workload') else 20 if rpc.endswith('_v2') else 50
        page, size = params.get('p_page', 1), params.get('p_page_size', default_size)
        totals = [row.get('total_count') for row in output['data']]
        totals = [int(value) if isinstance(value, str) and value.isdigit() else value for value in totals]
        total = totals[0] if totals and all(type(value) is int and value == totals[0] for value in totals) else None
        output['completeness']['pagination'] = {'page': page, 'page_size': size, 'total_count': total}
        if (type(page) is not int or type(size) is not int or not 1 <= page or not 1 <= size <= 100
                or type(total) is not int or total < 0 or len(output['data']) > size
                or len(output['data']) != max(0, min(size, total - (page - 1) * size))):
            incomplete = incomplete or 'native_page_count_unverified'
    if incomplete:
        if output['error'] is None:
            output['error'] = {'code': 'HAR_READ_INCOMPLETE', 'message': incomplete}
        output['completeness']['reason'] = incomplete
    else:
        output['completeness']['complete'] = True
    return output


def _check_scope(responses, scope, account):
    ids = set(scope['review_ids'])
    targets = {adapter._key(t) for t in scope['targets'] + scope.get('closure_targets', [])}
    for response in responses:
        rpc, params, rows = response['rpc'], response['params'], response['data']
        if rpc in adapter.REVIEW_RPCS:
            if rpc == 'qry_review_get_items':
                exact_ids = params.get('p_review_ids')
                exact_target = any(t[1] == params.get('p_data_id') and t[2] == params.get('p_data_version') for t in targets)
                if exact_ids is not None:
                    if not isinstance(exact_ids, list) or not exact_ids or not set(exact_ids).issubset(ids):
                        raise CaptureError('Review read IDs are outside scope')
                elif not exact_target:
                    raise CaptureError('Unbounded review read is not a scoped capture')
            if any(row.get('id') not in ids or adapter._target(row) not in targets for row in rows):
                raise CaptureError('Native review row is outside exact scope')
        elif rpc == 'qry_review_get_comment_items':
            review_id = params.get('p_review_id')
            if review_id not in ids or any(row.get('review_id') != review_id for row in rows):
                raise CaptureError('Native comment row is outside exact review scope')
        elif rpc == 'qry_root_review_reference_progress_v2':
            if params.get('p_root_review_id') not in ids:
                raise CaptureError('Root reference read is outside scope')
            if any(row.get('reference_review_id') not in ids
                   or (row.get('target_table'), row.get('data_id'), row.get('data_version')) not in targets for row in rows):
                raise CaptureError('Native reference review row is outside exact scope')
        elif rpc == 'qry_membership_get_mine':
            if params or any(row.get('user_id') != account for row in rows):
                raise CaptureError('Native membership account is outside scope')
        elif rpc in ('qry_review_get_member_list', 'qry_review_get_member_workload'):
            # Native review administration lists the system roster, including
            # unassigned members. It proves roles/workload, never eligibility.
            if (params.get('p_role') not in (None, 'review-admin', 'review-member')
                    or any(not isinstance(row.get('user_id'), str) or not row['user_id']
                           or row.get('team_id') != adapter.ZERO_TEAM
                           or row.get('role') not in ('review-admin', 'review-member')
                           or (params.get('p_role') is not None and row['role'] != params['p_role']) for row in rows)):
                raise CaptureError('Native reviewer roster row is outside scope')
            if rpc == 'qry_review_get_member_workload':
                for row in rows:
                    for key in ('pending_count', 'reviewed_count'):
                        value = row.get(key)
                        if not (type(value) is int and value >= 0
                                or isinstance(value, str) and value.isdigit()):
                            raise CaptureError('Native reviewer workload count is invalid')


def _process_export(record, raw, project, targets):
    """Interpret the installed public CLI contract; unsupported sources stay original."""
    if not isinstance(record, dict) or _sensitive(record):
        raise CaptureError('Invalid official process-get export')
    if (type(record.get('schemaVersion')) is not int or record['schemaVersion'] != 1
            or record.get('operation') != 'dataset process get'
            or record.get('status') != 'completed' or record.get('outcome') != 'dataset_read'
            or record.get('error') is not None):
        raise CaptureError('Process export must be a completed typed remote read')
    data, context, complete = (record.get(key) for key in ('data', 'context', 'completeness'))
    if not all(isinstance(item, dict) for item in (data, context, complete)):
        raise CaptureError('Process export metadata is missing')
    subject, requested, observation = data.get('subject'), context.get('effectiveInput'), context.get('observation')
    if not all(isinstance(item, dict) for item in (subject, requested, observation)):
        raise CaptureError('Process export exact read facts are missing')
    process_id, version = subject.get('id'), subject.get('version')
    if (not isinstance(process_id, str) or not isinstance(version, str)
            or not re.fullmatch(r'\d{2}\.\d{2}\.\d{3}', version)):
        raise CaptureError('Process export exact identity/version must be strings')
    target = ('processes', process_id, version)
    if (target not in targets or subject.get('type') != 'process'
            or requested.get('type') != 'process' or requested.get('action') != 'get'
            or requested.get('id') != process_id or requested.get('version') != version):
        raise CaptureError('Process export identity/version is outside exact scope or used latest')
    if (context.get('environment') != 'https://' + project + '.supabase.co'
            or observation.get('source') != 'remote'):
        raise CaptureError('Process export is not from the exact remote project')
    if (complete.get('scope') != 'one_dataset_version' or complete.get('returned') != 1
            or complete.get('truncated') is not False or complete.get('hasMore') is not False
            or complete.get('completeContent') is False
            or requested.get('fields') is not None and complete.get('completeContent') is not True):
        raise CaptureError('Use a full, unprojected exact Process read')
    observed = observation.get('time')
    if (not adapter._time(observed) or not adapter._time(subject.get('modifiedAt'))
            or type(subject.get('stateCode')) is not int):
        raise CaptureError('Process export state/time is missing')
    body = data.get('content')
    dataset = body.get('processDataSet') if isinstance(body, dict) else None
    try:
        body_id = dataset['processInformation']['dataSetInformation']['common:UUID']
        body_version = dataset['administrativeInformation']['publicationAndOwnership']['common:dataSetVersion']
    except (KeyError, TypeError):
        raise CaptureError('Complete process body identity/version is missing')
    if body_id != process_id or body_version != version:
        raise CaptureError('Process body identity/version contradicts the exact export')
    row = {'table': 'processes', 'id': process_id, 'version': version,
           'state_code': subject['stateCode'], 'observed_at': observed,
           'modified_at': subject['modifiedAt'], 'json': body,
           'read_evidence': {'origin': 'official_cli_export', 'sha256': hashlib.sha256(raw).hexdigest(),
                             'resolution': 'remote_supabase_exact', 'selected_body_column': 'json',
                             'native_fingerprint_basis_verified': False}}
    return row


def attach_process_exports(capture, process_get_paths):
    """Retain selected original exports and interpret each independently."""
    result = copy.deepcopy(capture)
    source = result.setdefault('source', {})
    scope = result.get('scope', {})
    targets = {adapter._key(row) for row in scope.get('targets', []) + scope.get('closure_targets', [])}
    datasets = result.setdefault('datasets', [])
    exports = source.setdefault('export_provenance', [])
    for path in process_get_paths:
        raw = _read(path)
        original = {'path': str(Path(path).resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}
        try:
            record = _json(raw)
        except ValueError:
            record = None
        if _sensitive(record):
            raise CaptureError('Credential fields are not process evidence')
        try:
            row = _process_export(record, raw, source.get('project'), targets)
            if any(adapter._key(existing) == adapter._key(row) for existing in datasets):
                raise CaptureError('An exact dataset body is already attached; compare the originals')
        except (ValueError, KeyError, TypeError) as exc:
            exports.append({**original, 'interpretation': 'unavailable', 'reason': str(exc)})
        else:
            datasets.append(row)
            exports.append({**original, 'interpretation': 'current_cli_process_read'})
    if len(adapter.canonical_json(result).encode()) > MAX_CAPTURE_BYTES:
        raise CaptureError('Imported capture exceeds 16 MiB')
    return result


def import_capture(identity_record, scope, har_paths, process_get_paths=()):
    """Import only explicit local inputs; capture provenance grants no write authority."""
    if not isinstance(identity_record, dict) or not isinstance(scope, dict):
        raise CaptureError('Identity and scope records must be objects')
    given = identity_record.get('source', identity_record)
    if not isinstance(given, dict):
        raise CaptureError('Source identity is malformed')
    source = {key: given.get(key) for key in ('project', 'account_user_id', 'observed_at', 'identity_observed_at')}
    project = source.get('project')
    if not isinstance(project, str) or not re.fullmatch('[a-z0-9]{20}', project):
        raise CaptureError('Exact Supabase project identifier required')
    source['origin'] = 'browser_network_export'
    identity = given.get('identity')
    source['identity'] = {'id': identity.get('id')} if isinstance(identity, dict) else {}
    capture = {'schema': adapter.SCHEMA, 'profile': 'native', 'contract_profile': 'source_v2',
               'source': source, 'scope': {key: scope.get(key, []) for key in ('targets', 'review_ids', 'closure_targets')},
               'responses': [], 'datasets': []}
    scope = capture['scope']
    initial_errors = adapter.validate_capture(capture)
    if initial_errors:
        raise CaptureError('Identity/scope validation failed: ' + '; '.join(initial_errors))
    for name in ('targets', 'closure_targets'):
        scope[name] = [{key: row[key] for key in ('table', 'id', 'version')} for row in scope[name]]
    membership = identity_record.get('membership_response')
    if not isinstance(membership, dict) or membership.get('rpc') != 'qry_membership_get_mine':
        raise CaptureError('Native membership response envelope required')
    membership = {key: membership.get(key) for key in ('rpc', 'params', 'data', 'http_status', 'error', 'completeness')}
    if _sensitive(membership):
        raise CaptureError('Membership body contains credential fields')
    capture['responses'].append(membership)
    provenance = []
    total_bytes = 0
    for path in har_paths:
        path = Path(path)
        if path.suffix.lower() != '.har':
            raise CaptureError('HAR input must use .har suffix')
        raw = _read(path)
        total_bytes += len(raw)
        if total_bytes > MAX_CAPTURE_BYTES:
            raise CaptureError('Combined HAR inputs exceed 16 MiB')
        document = _json(raw)
        log = document.get('log') if isinstance(document, dict) else None
        entries = log.get('entries') if isinstance(log, dict) else None
        if not isinstance(entries, list):
            raise CaptureError('HAR log entries are missing')
        imported = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise CaptureError('Malformed HAR entry')
            parsed = _request(entry, project)
            if parsed is None:
                continue
            rpc, params = parsed
            capture['responses'].append(_response(entry, rpc, params))
            if rpc.endswith('_v6'):
                capture['contract_profile'] = 'observed_v6'
            imported += 1
        provenance.append({'sha256': hashlib.sha256(raw).hexdigest(), 'entry_count': len(entries), 'imported_read_count': imported})
    if not provenance or not any(p['imported_read_count'] for p in provenance):
        raise CaptureError('No native read responses were imported')
    errors = adapter.validate_capture(capture)
    if errors:
        raise CaptureError('Imported capture validation failed: ' + '; '.join(errors))
    _check_scope(capture['responses'], scope, source['account_user_id'])
    source['har_provenance'] = provenance
    if len(adapter.canonical_json(capture).encode()) > MAX_CAPTURE_BYTES:
        raise CaptureError('Imported capture exceeds 16 MiB')
    return attach_process_exports(capture, process_get_paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--scope', type=Path, required=True)
    parser.add_argument('--har', type=Path, action='append', required=True)
    parser.add_argument('--process-get', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        capture = import_capture(_json(_read(args.identity)), _json(_read(args.scope)), args.har, args.process_get)
        raw = (adapter.canonical_json(capture) + '\n').encode()
        with args.output.open('xb') as stream:
            stream.write(raw)
    except (CaptureError, OSError, ValueError, TypeError) as error:
        parser.exit(2, 'review_capture: ' + str(error) + '\n')
    print(json.dumps({'schema': adapter.SCHEMA, 'profile': capture['profile'],
                      'responses': len(capture['responses']), 'sha256': hashlib.sha256(raw).hexdigest()}))


if __name__ == '__main__':
    main()
