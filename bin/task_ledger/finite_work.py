"""Original package lineage and native usage observations."""

import hashlib
import json
import math
from pathlib import Path
from urllib.parse import quote

import harness_tasks as t


def events(c, task_id, *kinds):
    placeholders = ','.join('?' for _ in kinds)
    return ((row['kind'], json.loads(row['payload_json'])) for row in c.execute(
        f'SELECT kind,payload_json FROM work_task_events WHERE task_id=? AND kind IN ({placeholders}) ORDER BY id',
        (task_id, *kinds)))


def request_configs(c, task_id):
    return {event['request_id']: event['limits'] for _, event in events(c, task_id, 'finite-request')}


def package_members(c, task, req, configs):
    members, ident = [], req['id']
    while ident and ident not in members:
        members.append(ident)
        ident = configs.get(ident, {}).get('continues_request_id')
    return members


def successor(configs, request_id):
    return next((ident for ident, limits in configs.items()
                 if limits.get('continues_request_id') == request_id), None)


def _package_root(configs, request_id):
    while parent := configs.get(request_id, {}).get('continues_request_id'):
        request_id = parent
    return request_id


def _package_id(req, configs):
    ident = req['parent_request_id'] if req['independent'] else req['id']
    return _package_root(configs, ident)


def programmatic_request(c, req, *, configs=None):
    return req['kind'] == 'plan' and not req['independent'] and req['recipient_role'] == 'executor'


USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens')


def _nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _run_meter(c, root, run):
    rows = c.execute('''SELECT receipt_json FROM work_run_events
        WHERE run_id=? AND receipt_json IS NOT NULL ORDER BY id DESC''', (run['id'],))
    for row in rows:
        try:
            ref = t.evidence_ref(root, json.loads(row['receipt_json']))
            receipt = json.loads(Path(ref['path']).read_bytes())
            if not isinstance(receipt, dict):
                continue
        except (OSError, ValueError):
            continue
        if receipt.get('run_id') != run['id'] or receipt.get('request_id') != run['request_id']:
            continue
        result = receipt.get('result')
        meter = result.get('metering') if isinstance(result, dict) else None
        meter = meter or receipt.get('metering')
        if isinstance(meter, dict):
            return meter
    return {}


def _usage_totals(measurements):
    elapsed = unknown_elapsed = 0
    usage = {key: [] for key in (*USAGE_FIELDS, 'cost_usd')}
    for meter in measurements:
        seconds = meter.get('elapsed_seconds')
        duration = meter.get('native_duration_ms')
        if not _nonnegative(seconds) and _nonnegative(duration):
            seconds = duration / 1000
        if _nonnegative(seconds):
            elapsed += seconds
        else:
            unknown_elapsed += 1
        native_usage = meter.get('usage')
        if not isinstance(native_usage, dict):
            native_usage = {}
        for key in usage:
            value = meter.get(key) if key == 'cost_usd' else native_usage.get(key)
            usage[key].append(value if _nonnegative(value) else None)
    return {'elapsed_seconds': None if unknown_elapsed == len(measurements) and measurements else round(elapsed, 6),
            'unknown_elapsed_runs': unknown_elapsed, 'run_count': len(measurements),
            'usage': {key: sum(values) if values and None not in values else None for key, values in usage.items()}}


def usage_view(c, task, req, *, configs=None):
    configs = request_configs(c, task['id']) if configs is None else configs
    package = _package_id(req, configs)
    if not package:
        return None
    previous_budget = configs[package].get('budget', {})
    batch = previous_budget.get('batch_request_id', package)
    from harness_tasks import workspace_root
    root = workspace_root(c)
    requests = {row['id']: _package_id(row, configs)
                for row in c.execute('SELECT * FROM work_requests WHERE task_id=?', (task['id'],))}
    related = {ident for ident in configs
               if configs[ident].get('budget', {}).get('batch_request_id', ident) == batch}
    measurements = [(requests[row['request_id']], _run_meter(c, root, row))
                    for row in c.execute('''SELECT r.* FROM work_runs r JOIN work_requests q ON q.id=r.request_id
                                           WHERE q.task_id=? ORDER BY r.created_at,r.id''', (task['id'],))
                    if requests[row['request_id']] in related]
    result = {'package_request_id': package, 'package': _usage_totals(
        [meter for ident, meter in measurements if ident == package])}
    if batch != package or len(related) > 1:
        result.update(batch_request_id=batch, batch=_usage_totals([meter for _, meter in measurements]))
    return result


def saved_object(root, ref):
    path = Path(t.evidence_ref(root, ref)['path'])
    try:
        doc = json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        return {}
    return doc if isinstance(doc, dict) else {}
