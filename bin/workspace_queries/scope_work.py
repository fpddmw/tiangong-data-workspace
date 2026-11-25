"""Read the agreed materials, original writers and receipts in a single ledger snapshot."""
from collections import Counter, defaultdict
import json
import shlex

import harness_tasks as tasks


def page(rows, limit, offset):
    return {'total': len(rows), 'items': rows[offset:offset + limit],
            'next_offset': offset + limit if offset + limit < len(rows) else None}


def _scope_ids(item, returned, requests):
    # A return observation can correct the original coverage. A broad assignment
    # alone cannot place an unindexed receipt against each of its materials.
    receipt = returned.get(item.get('return_id'), {})
    if receipt.get('coverage'):
        return receipt['coverage']
    if item.get('scope_ids'):
        return item['scope_ids']
    if item['action'] in ('dispatch', 'continue', 'resolve_dependency'):
        return [item['object']]
    assigned = requests.get(item.get('request_id'), {}).get('scope_ids', [])
    return assigned if len(assigned) == 1 else []


def _authors(row, phases, *, detail):
    units = row['responsibility']
    runs = {run['request_id']: run for run in row['execution']}
    authors = []
    for unit in units:
        qid = unit['request_id']
        run = runs.get(qid) or {}
        phase = phases.get(run.get('run_id'))
        state = run.get('state', 'no_recorded_run')
        if run.get('stop_requested'):
            state = 'stop_requested'
        elif state == 'running' and (run.get('execution_observation') or {}).get('state') == 'exited':
            state = 'exited'
        elif state == 'running' and phase in ('waiting_review', 'waiting_retry', 'queued'):
            state = phase
        author = {'request_id': qid, 'state': state}
        if run:
            author.update(run_id=run['run_id'], observed_at=run['observed_at'])
        if detail:
            author.update(owner=unit['owner'], brief_ref=unit['brief_ref'], native=run.get('native_identity'),
                          outcome=run.get('state'), last_capacity_phase=phase, receipt=run.get('source_ref'),
                          execution_observation=run.get('execution_observation'),
                          request_command='work-show --id ' + shlex.quote(qid))
            if run:
                author['continuation_preview'] = shlex.join(['work-dispatch','--request',qid])
                author['run_command'] = 'work-show --id ' + shlex.quote(run['run_id']) + ' --detail'
        authors.append(author)
    return sorted(authors, key=lambda a: a.get('observed_at', ''), reverse=True)


def material_view(c, task, projected, pending, returns, *, selected=None, limit=10, offset=0, overview=False):
    ident = task['id']
    navigation = {'original_brief_ref': json.loads(task['original_ref_json']),
                  'current_plan_path': json.loads(task['plan_ref_json'])['path']}
    scopes = {row['scope_id']: row for row in projected['scope_items']}
    if selected is not None and selected not in scopes:
        raise ValueError('Unknown scope item in ' + ident + ': ' + selected)
    requests = {r['id']: {'scope_ids': json.loads(r['required_scope_json']), 'parent': r['parent_request_id'],
                           'independent': bool(r['independent'])}
                for r in c.execute('SELECT id,required_scope_json,parent_request_id,independent FROM work_requests WHERE task_id=?', (ident,))}
    for request in requests.values():
        if request['independent'] and not request['scope_ids'] and request['parent'] in requests:
            request['scope_ids'] = requests[request['parent']]['scope_ids']
    returned = {r['id']: r for r in returns}
    inbox = defaultdict(lambda: {'actionable': [], 'waiting': []})
    for kind in ('actionable', 'waiting'):
        for item in pending[kind]:
            ids = _scope_ids(item, returned, requests)
            matched = set(ids).intersection(scopes)
            for sid in matched or {None}:
                inbox[sid][kind].append(item)
    shared = defaultdict(Counter)
    for kind, entries in inbox[None].items():
        for entry in entries:
            qid = entry.get('request_id')
            shared[qid][kind] += 1
    # An author can keep a live session while releasing the author seat.
    run_ids = {r['run_id'] for row in scopes.values() for r in row['execution']}
    phases = {r['run_id']: json.loads(r['payload_json']).get('phase') for r in c.execute('''
        SELECT e.run_id,e.payload_json FROM work_run_events e JOIN
          (SELECT max(id) AS id FROM work_run_events WHERE kind='capacity'
           AND run_id IN (SELECT value FROM json_each(?)) GROUP BY run_id) latest ON e.id=latest.id''',
        (tasks._json(sorted(run_ids)),))}
    items = []
    active_scopes = set()
    for sid, row in scopes.items():
        if selected is not None and sid != selected:
            continue
        authors = _authors(row, phases, detail=selected is not None)
        group = inbox[sid]
        if group['actionable'] or group['waiting'] or any(
                a['state'] in ('running', 'queued', 'waiting_review', 'waiting_retry', 'intent', 'unknown')
                for a in authors):
            active_scopes.add(sid)
        summary = {k: row[k] for k in ('scope_id','label','kind','state','endpoint','remaining',
                                        'remaining_reasons','mapping_verified','accepted_endpoint')}
        summary.update(author_states=dict(Counter(a['state'] for a in authors)),
                       pending_actions=dict(Counter(i['action'] for i in group['actionable'])),
                       waiting_count=len(group['waiting']),
                       coverage_return_count=row['coverage_returns_count'],
                       detail_command=shlex.join(['work-show','--id',ident,'--scope',sid]))
        if not overview:
            summary['request_ids'] = [q['request_id'] for q in row['responsibility']]
        if authors:
            summary['latest_author'] = {k: authors[0][k] for k in ('request_id','state')}
        if row['adoptions']:
            summary['latest_disposition'] = row['adoptions'][-1]['use']
        if group['waiting']:
            first = group['waiting'][0]
            summary['first_wait'] = {k:first[k] for k in ('dependency','depends_on','reason_ref','recheck_at','blocked_by')
                                     if first.get(k) is not None}
        elif due := next((i for i in group['actionable'] if i.get('previous_wait')), None):
            summary['recheck'] = due['previous_wait']
        if selected is not None:
            ended = [{'request_id': qid, 'decision_ref': decision['evidence_ref'],
                      'request_command': shlex.join(['work-show', '--id', qid])}
                     for qid, decision in tasks._delegation_decisions(c, ident).items()
                     if decision['action'] == 'end' and qid in requests and sid in requests[qid]['scope_ids']
                     and not requests[qid]['independent']]
            # The preceding writer can be ended or belong to an explicitly
            # continued request. Its recorded attempt still owns its effects.
            from harness_package import latest_author
            for author in [*ended, *authors]:
                if author.get('run_id'):
                    continue
                original = tasks._decode(tasks._row(c, 'work_requests', author['request_id']))
                run = latest_author(c, original)
                if run is not None:
                    author['original_attempt'] = {'run_id': run['id'], 'request_id': run['request_id'],
                            'outcome': run['outcome'], 'stop_requested': bool(run['stop_requested']),
                            'command': shlex.join(['work-show', '--id', run['id'], '--detail'])}
            coverage = [r for r in reversed(returns) if sid in r['coverage']]
            actions = sorted(group['actionable'], key=lambda i:i.get('visible_at') or '', reverse=True)
            actions.sort(key=lambda i:i['action'] == 'adopt' and not i.get('previous_wait'))
            return {**summary, 'accepted_use': row.get('accepted_use'), 'authors': authors, 'ended_authors': ended, 'source_refs': row['source_refs'],
                    'dispositions': page(list(reversed(row['adoptions'])), limit, offset),
                    'last_disposition': row['adoptions'][-1] if row['adoptions'] else None,
                    'actionable': page(actions, limit, offset),
                    'waiting': page(group['waiting'], limit, offset),
                    'related_unindexed': [{'request_id':qid, 'counts':dict(counts),
                          'command':shlex.join(['work-show','--id',qid])}
                        for qid,counts in shared.items() if qid and sid in requests[qid]['scope_ids']],
                    'coverage_returns': page([{'return_id': r['id'], 'request_id': r['request_id'],
                        'result_ref': r['result_ref']} for r in coverage], limit, offset),
                    **navigation}
        if overview:
            for key in ('kind', 'remaining_reasons'):
                summary.pop(key)
            if not row['remaining'] and sid not in active_scopes:
                summary = {k: summary[k] for k in ('scope_id', 'label', 'state', 'remaining', 'endpoint',
                                                    'accepted_endpoint', 'detail_command')}
        items.append(summary)
    items.sort(key=lambda row: (not bool(row.get('pending_actions')), row['scope_id'] not in active_scopes,
                                not row['remaining']))
    return {'scope_items': items, 'remaining_count': projected['remaining_count'], 'total': projected['total'],
            'unassigned_pending': {k: len(v) for k,v in inbox[None].items()},
            'unassigned_by_request': [
                {'request_id': qid, 'counts': dict(counts), 'assigned_scope': requests[qid]['scope_ids'],
                 'command': shlex.join(['work-show','--id',qid])} for qid,counts in shared.items() if qid],
            **navigation,
            'capacity_commands': ['work-capacity --host dsh','work-capacity --host devin']}
