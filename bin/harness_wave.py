"""Current responsibilities and wave closure from the original task ledger."""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import time

from task_ledger import decisions
import harness_tasks as tasks

DEPENDENCY = 'work-dependency'
READY = 'work-dispatch-ready'
WAIT = 'work-wait'
WAVE = 'work-wave-close'
WAVE_CHECK = 'work-wave-check'
INTAKE = 'work-role-intake'
PRIORITY = 'work-scope-priority'
DEFERRED = 'work-item-deferred'
WAVE_HANDLED = 'wave-review-handled'
ACTIONS = ('dispatch', 'continue', 'adopt', 'review', 'intake', 'resolve_dependency', 'review_correction')


def _digest(value):
    return hashlib.sha256(tasks._json(value).encode()).hexdigest()


def _selected(c, root, selection, subject, *, at=None):
    resolved = decisions.resolve(root, selection, connection=c, subject=subject, as_of=at, verify_sources=False)
    for entry in resolved['subjects']:
        if entry['status'] == 'applicable' and len(entry['decisions']) == 1:
            return entry['decisions'][0]
    return None


def _valid(root, decision):
    try:
        tasks.evidence_ref(root, decision['source_ref'])
    except (OSError, ValueError):
        return False
    return True


def _dependency_versions(c, root, task_id, dependencies, *, at=None):
    versions = {}
    for ident in dependencies:
        entry = _selected(c, root, {'task': task_id, 'object': ident}, DEPENDENCY, at=at)
        versions[ident] = {'id': entry['id'], 'state': entry['statement']} if entry and _valid(root,entry) else None
    return versions


def decision_details(c, root, document, actor):
    """Validate coordination decisions at the existing decision write boundary."""
    subject = document['subject']
    from scientific_review.rounds import EXTRA, budget_details
    from platform_review.submission import APPROVE, INDEPENDENT_USE, SUBMISSION, decision_details as review_details
    users = (DEPENDENCY, READY, WAIT, WAVE, WAVE_CHECK, INTAKE, EXTRA, APPROVE, INDEPENDENT_USE, PRIORITY, SUBMISSION)
    if subject not in users:
        return {}
    selected = document['scope']
    task = tasks._row(c, 'work_tasks', selected.get('task'))
    key = tasks.native_key(actor)
    owners = (tasks.native_key(task['owner_actor']), tasks.native_key(task['sponsor_actor']))
    details = document.get('details') or {}
    if not isinstance(details, dict):
        raise ValueError('Work decision details must be an object')
    if subject != WAIT and key not in owners:
        tasks._fail('work_decision_scope', 'The current owner or sponsor must make this decision.', task['id'])
    if subject == EXTRA:
        return budget_details(c, task, actor, selected, document)
    if subject in (APPROVE, INDEPENDENT_USE, SUBMISSION):
        return review_details(c, root, task, actor, selected, document)
    if subject in (WAVE,WAVE_CHECK):
        if not selected.get('object'):
            raise ValueError('Identify the exact wave or wave decision')
        if subject==WAVE_CHECK and key!=owners[1]:
            tasks._fail('wave_check_authority_required','The original sponsor makes this wave check.',task['id'])
        return {}
    if subject == WAIT:
        ident, action = selected.get('object'), selected.get('action')
        if not ident or action not in ACTIONS:
            raise ValueError('Waiting decision needs one exact original object and action')
        if key not in owners:
            if action != 'review':
                tasks._fail('work_decision_scope', 'The current owner, sponsor or actual recipient must decide this wait.', task['id'])
            review = tasks._row(c, 'work_requests', ident)
            if not review['independent'] or review['task_id'] != task['id']:
                raise ValueError('Select one original independent review in this task')
            parent = tasks._row(c, 'work_requests', review['parent_request_id'])
            if all(key != tasks.native_key(author) for author in tasks.request_authors(c, parent)):
                tasks._fail('work_decision_scope', 'Only the original author may defer this review.', ident)
        deps = tasks._list(details.get('depends_on', []), 'Exact dependency IDs')
        when = details.get('recheck_at')
        if when:
            at = datetime.fromisoformat(when)
            if at.tzinfo is None:
                raise ValueError('Use a recheck time with its timezone')
            when = at.astimezone(timezone.utc).isoformat()
        if not deps and not when:
            raise ValueError('Identify the dependency or time that will end the wait')
        return {'depends_on': deps, 'recheck_at': when,
                'dependency_versions': _dependency_versions(c, root, task['id'], deps)}
    if subject == DEPENDENCY:
        if not selected.get('object') or document['statement'] not in ('pending', 'released', 'failed'):
            raise ValueError('Record pending, released or failed for an exact dependency')
        return {}
    if subject in (READY,PRIORITY):
        ident = selected.get('object')
        if not ident or subject==READY and selected.get('action')!='dispatch':
            raise ValueError('Use the exact original scope item and applicable action')
        from harness_scope import _scope_rows
        if ident not in {row['scope_id'] for row in _scope_rows(c, task)}:
            raise ValueError('Use an item in the original scope')
        deps = tasks._list(details.get('depends_on', []), 'Exact dependency IDs')
        if subject == PRIORITY:
            if document['statement'] not in ('core','reserve'):
                raise ValueError('Identified commitments are core or an authorized reserve')
            return {'external_dependencies': deps}
        from dsh_capacity import POOL_NAMES
        if details.get('host') not in POOL_NAMES:
            raise ValueError('Select a host with observable author capacity')
        return {'host': details['host'], 'depends_on': deps,
                **({'provider': tasks._text(details['provider'], 'Provider')} if 'provider' in details else {})}
    if subject == INTAKE:
        if key != owners[1] or not selected.get('object') or selected.get('action') != 'intake':
            tasks._fail('intake_authority_required', 'Only the original sponsor may delegate this exact pending decision.', task['id'])
        # The scope and original decision bind one named matter. Host enrollment
        # and a positive exit observation are checked again before initiation.
        expires = datetime.fromisoformat(tasks._text(details.get('expires_at'), 'Intake decision expiry'))
        if expires.tzinfo is None:
            raise ValueError('Use an expiry with its timezone')
        decision_scope = decisions.scope(details.get('decision_scope'))
        if decision_scope.get('task') != task['id'] or decision_scope.get('object') != selected['object']:
            raise ValueError('Bind the successor to the same task and exact pending object')
        subject = tasks._text(details.get('decision_subject'), 'Subject the original sponsor may delegate')
        if subject!=WAVE_CHECK and subject in (*users, decisions.DATASET_WRITE):
            raise ValueError('A cold takeover is limited to its stated coordination decision')
        from task_ledger.role_intake import check_reassignment
        check_reassignment(c,task,selected['object'],document['id'])
        return {'successor_host': tasks._text(details.get('successor_host'), 'Successor host'),
                'expires_at': expires.astimezone(timezone.utc).isoformat(), 'decision_scope': decision_scope,
                'decision_subject': subject}
    return {}


def _receipt_items(c, task, *, at=None, return_ids=None):
    """Independent opinions go to the author. Returns for use go to the lead."""
    at = at or tasks._now()
    query = """SELECT ret.id,ret.created_at,ret.result_ref_json,ret.request_id,ret.run_id,
                      req.independent,req.parent_request_id
               FROM work_returns ret JOIN work_requests req ON req.id=ret.request_id
               WHERE req.task_id=? AND req.kind IN ('plan','review') AND ret.created_at<=?"""
    args = (task['id'], at)
    if return_ids is not None:
        query += ' AND ret.id IN (SELECT value FROM json_each(?))'
        args += (tasks._json(list(return_ids)),)
    handled = {row['return_id'] for row in tasks._effective_dispositions(c, task['id'], before=at)}
    delivered = {(event['review_id'], event['return_id'], tasks.native_key(event['receiver']))
                 for row in c.execute("""SELECT e.payload_json FROM work_run_events e
                    JOIN work_runs r ON r.id=e.run_id JOIN work_requests q ON q.id=r.request_id
                    WHERE q.task_id=? AND e.kind='review-delivered' AND e.created_at<=?""", (task['id'], at))
                 if (event := json.loads(row[0])).get('receiver') and event.get('return_id')}
    for row in c.execute(query + ' ORDER BY ret.created_at,ret.id', args):
        if row['independent']:
            if row['run_id'] and not c.execute("""SELECT 1 FROM work_run_events WHERE run_id=?
                AND kind='run-outcome' AND json_extract(payload_json,'$.outcome')='succeeded' AND created_at<=?""",
                                               (row['run_id'], at)).fetchone():
                continue
            parent = tasks._row(c, 'work_requests', row['parent_request_id'])
            bound = c.execute("""SELECT r.native_identity_json FROM work_runs r
                JOIN work_run_events e ON e.run_id=r.id WHERE r.request_id=? AND e.created_at<=?
                AND r.native_identity_json IS NOT NULL
                AND e.kind IN ('run-bind','run-outcome') AND e.receipt_json IS NOT NULL ORDER BY e.id DESC LIMIT 1""",
                              (parent['id'], at)).fetchone()
            receiver = (json.loads(bound[0]) if bound else json.loads(parent['recipient_actor'])
                        if parent['recipient_actor'] and parent['created_at'] <= at else None)
            if receiver is None or (row['request_id'], row['id'], tasks.native_key(receiver)) in delivered:
                continue
            action, unit = 'review', row['request_id']
        else:
            if row['id'] in handled:
                continue
            receiver = json.loads(task['owner_actor'])
            action, unit = 'adopt', row['id']
        yield {'action': action, 'object': unit, 'request_id': row['request_id'], 'return_id': row['id'],
               'responsible': receiver, 'visible_at': row['created_at'], 'source_ref': json.loads(row['result_ref_json'])}


def receipt_items(c, task, *, at=None, return_ids=None):
    """The same arrival projection is used by the live view and historical replay."""
    return list(_receipt_items(c, task, at=at, return_ids=return_ids))


def _author_capacity(view, provider):
    occupied = view.get('counts', {}).get('occupied')
    waiting = view.get('counts', {}).get('queued', {})
    limits = view.get('limits', {})
    if not occupied or not view.get('policy_ready') or waiting.get('author'):
        return None
    free = min(limits['author'] - occupied['author'], limits['total'] - occupied['total'])
    pilot = view.get('provider_pilot')
    if pilot:
        if provider is None:
            return None  # Unresolved provider routing while provider quotas differ.
        if provider == pilot['provider']:
            pool = pilot['occupied']
            free = min(free, pilot['limits']['author'] - pool['author'], pilot['limits']['total'] - pool['total'])
    return max(0, free)


def _hard_dependency(c,root,wait):
    dependency=wait.get('dependency')
    if not dependency:
        return None
    if 'path' in dependency:
        from workspace_files import sha256_file,safe_path,is_credential_path,ProductionError
        try:
            path=safe_path(root,dependency['path'])
            if str(path)!=dependency['path'] or is_credential_path(path.relative_to(root)):
                return {'unavailable':True}
            return {'sha256':sha256_file(path)}
        except (OSError,ProductionError):
            return {'sha256':None}
    ident=dependency['request_id']
    original=tasks._row(c,'work_requests',ident)
    latest=c.execute('SELECT id,outcome,stop_requested FROM work_runs WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',(ident,)).fetchone()
    returned=c.execute('SELECT id FROM work_returns WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',(ident,)).fetchone()
    dispositions=tasks._effective_dispositions(c,original['task_id'],request_ids=[ident])
    control=tasks._delegation_decisions(c,original['task_id']).get(ident)
    return {'latest_run':dict(latest) if latest else None,'latest_return':returned[0] if returned else None,
            'dispositions':[row['id'] for row in dispositions],'control':control}


def _deferred(c,task_id):
    waits = {}
    for row in c.execute('SELECT payload_json FROM work_task_events WHERE task_id=? AND kind=? ORDER BY id',
                         (task_id, DEFERRED)):
        value = json.loads(row[0])
        key = value['item_id']
        # Older dispatch waits used a shared scope ID, but recorded the exact
        # request. Read both generations under that request without rewriting events.
        if value.get('request_id') and value.get('action') in ('dispatch', 'resolve_dependency'):
            key = item_id(task_id, value)
        waits[key] = value
    return waits


def _waiting(c,root,wait,now):
    return ((not wait['recheck_at'] or wait['recheck_at']>now)
            and wait['dependency_at_decision']==_hard_dependency(c,root,wait))


def _wait_scope(c, root, task, selected):
    """An exact return can leave only its own declared scope externally blocked."""
    from harness_scope import _returns
    if selected['action']=='dispatch':
        return selected['scope_ids']
    if selected['action']!='adopt':
        return []
    original=tasks._row(c,'work_requests',selected['request_id'])
    assigned=json.loads(original['required_scope_json'])
    if len(assigned)==1:
        return assigned
    returned=next((r for r in _returns(c,task['id'],request_ids=[original['id']])
                   if r['id']==selected['return_id']),None)
    return [sid for sid in (returned or {}).get('coverage',[]) if sid in assigned]


def defer(root, task_id, item, *, until=None, request=None, path=None, reason=None, external=False):
    from harness_scope import projection
    from workspace_files import safe_path, is_credential_path
    root=Path(root).resolve()
    actor=tasks.verified_actor(root)
    if not (until or request or path) or request and path or external and not (request or path):
        raise ValueError('Defer one pending item until a time or exact original dependency changes')
    if until:
        when=datetime.fromisoformat(until)
        if when.tzinfo is None:
            raise ValueError('Use a recheck time with timezone')
        until=when.astimezone(timezone.utc).isoformat()
    dependency=None
    if path:
        source=safe_path(root,path,must_exist=False)
        if is_credential_path(source.relative_to(root)):
            raise ValueError('Credentials cannot be a work dependency')
        dependency={'path':str(source)}
    elif request:
        dependency={'request_id':request}
    with closing(tasks._connect(root,True)) as c:
        task=tasks._row(c,'work_tasks',task_id)
        if request:
            tasks._row(c,'work_requests',request)
        view=project(c,root,task,projection(c,root,task_id,compact=True),actor=actor)
        selected=next((x for x in [*view['actionable'],*view['waiting']] if x['item_id']==item),None)
    if selected is None or not selected.get('source_ref'):
        raise ValueError('Use one pending item from your original work-show view')
    if request and request==selected.get('request_id'):
        raise ValueError('Choose an original dependency distinct from the waiting request')
    with tasks.transaction(root) as c:
        task=tasks._row(c,'work_tasks',task_id)
        tasks._admit_scope(c,task)
        if selected.get('return_id') and selected['action'] in ('review','adopt'):
            visible=receipt_items(c,task,return_ids=[selected['return_id']])
            if not any(row['action']==selected['action'] and tasks.native_key(row['responsible'])==tasks.native_key(actor)
                       for row in visible):
                tasks._fail('work_decision_scope','The original return or its holder changed before this wait decision.',item)
        if selected['action']!='review':
            actual=json.loads(task['sponsor_actor'] if selected['action']=='intake' else task['owner_actor'])
            if tasks.native_key(actual)!=tasks.native_key(actor):
                tasks._fail('work_decision_scope','The item holder changed before this wait decision.',item)
        meta={'item_id':item,'recheck_at':until,'dependency':dependency,'external':bool(external),
              'request_id':selected.get('request_id'),'action':selected['action'],
              'return_id':selected.get('return_id'),'scope_ids':_wait_scope(c,root,task,selected) if external else []}
        meta['dependency_at_decision']=_hard_dependency(c,root,meta)
        previous=_deferred(c,task_id).get(item)
        if previous and all(previous.get(k)==v for k,v in meta.items()) and _waiting(c,root,previous,tasks._now()):
            return previous
        source_ref=(tasks.save_note(root,Path(json.loads(task['plan_ref_json'])['path']).parent,'wait-reason',reason)
                    if reason else selected['source_ref'])
        record={**meta,'id':'defer:'+_digest([task_id,meta,source_ref]),'source_ref':source_ref}
        tasks._event(c,task,DEFERRED,actor,record)
        return record


def priority(c,root,task,scope_items,*,agreement=None):
    """Identify coverage from explicit decisions, including evidence of external dependencies."""
    groups = {key: [] for key in ('core_complete','core_blocked','core_open','reserve','unmapped')}
    if agreement is None:
        from harness_scope import contract
        agreement=contract(c,'task',task['id']) or {}
    if reserve:=agreement.get('body',{}).get('reserve'):
        deferred=_deferred(c,task['id'])
        handled={d['return_id'] for d in tasks._effective_dispositions(c,task['id'])} if deferred else set()
        external={}
        for wait in deferred.values():
            if (wait['external'] and wait.get('scope_ids') and wait.get('return_id') not in handled
                    and _waiting(c,root,wait,tasks._now())):
                for sid in wait['scope_ids']:
                    external.setdefault(sid,set()).add(wait['request_id'])
        by_id={item['scope_id']:item for item in scope_items}
        core=set(reserve['core_scope_ids'])
        reserves=set(reserve['scope_ids'])
        for ident in sorted(core):
            item=by_id.get(ident)
            if item and not item['remaining']:
                groups['core_complete'].append(ident)
                continue
            requests={q['request_id'] for q in item['responsibility']} if item else set()
            in_flight=any(run['state'] in ('running','intent','unknown') and not run['stop_requested']
                          for run in item['execution']) if item else False
            groups['core_blocked' if requests and requests<=external.get(ident,set()) and not in_flight else 'core_open'].append(ident)
        groups['reserve']=[x for x in sorted(reserves) if x in by_id and by_id[x]['remaining']]
        groups['unmapped']=[item['scope_id'] for item in scope_items if item['remaining'] and item['scope_id'] not in core|reserves]
        return {**groups,'reserve_window_open':not groups['core_open'],'reserve_policy':reserve}
    has_priority = (tasks._exists(c,'work_decisions') and c.execute("""
        SELECT 1 FROM work_decisions WHERE subject=? AND json_extract(scope_json,'$.task')=? LIMIT 1""",
        (PRIORITY,task['id'])).fetchone())
    for item in scope_items:
        ident = item['scope_id']
        selected = _selected(c,root,{'task':task['id'],'object':ident},PRIORITY) if has_priority else None
        if not selected or not _valid(root,selected):
            if item['remaining']:
                groups['unmapped'].append(ident)
            continue
        if selected['statement']=='reserve':
            if item['remaining']:
                groups['reserve'].append(ident)
        elif not item['remaining']:
            groups['core_complete'].append(ident)
        else:
            ids = selected['details'].get('external_dependencies',[])
            known = [_selected(c,root,{'task':task['id'],'object':key},DEPENDENCY) for key in ids]
            blocked = any(entry and entry['statement'] in ('pending','failed') and _valid(root,entry) for entry in known)
            groups['core_blocked' if blocked else 'core_open'].append(ident)
    return {**groups,'reserve_window_open':not groups['core_open'] and not groups['unmapped']}


def guard_reserve(c,root,task,request):
    scopes = json.loads(request['required_scope_json'])
    from harness_scope import _contract_for_request, contract, projection
    _,bound=_contract_for_request(c,tasks._decode(request))
    live=contract(c,'task',task['id']) or {}
    bound_rule=(bound or {}).get('body',{}).get('reserve') or {}
    live_rule=live.get('body',{}).get('reserve') or {}
    reserve_ids=set(bound_rule.get('scope_ids',[]))|set(live_rule.get('scope_ids',[]))
    if reserve_ids.intersection(scopes):
        if c.execute('SELECT 1 FROM work_runs WHERE request_id=? AND native_identity_json IS NOT NULL',(request['id'],)).fetchone():
            return
        if (not bound_rule or not live_rule or not (set(scopes)&reserve_ids).issubset(
                set(bound_rule['scope_ids'])&set(live_rule['scope_ids']))):
            tasks._fail('reserve_not_authorized','Read the original frozen reserve and current authority before first launch.',request['id'])
        current=projection(c,root,task['id'],compact=True)
        if any(not priority(c,root,task,current['scope_items'],agreement=agreement)['reserve_window_open']
               for agreement in (bound,live)):
            tasks._fail('reserve_not_ready','Core commitments still have work without a concrete external wait.',request['id'])
        used=c.execute('''SELECT count(DISTINCT other.id) FROM work_requests other JOIN work_runs run ON run.request_id=other.id
            WHERE other.task_id=? AND other.id!=? AND other.kind='plan' AND other.independent=0
            AND (run.native_identity_json IS NOT NULL OR run.outcome IN ('intent','running','unknown'))
            AND EXISTS(SELECT 1 FROM json_each(other.required_scope_json) WHERE value IN (SELECT value FROM json_each(?)))''',
            (task['id'],request['id'],tasks._json(sorted(reserve_ids)))).fetchone()[0]
        if used>=min(bound_rule['max_requests'],live_rule['max_requests']):
            tasks._fail('reserve_limit','The authorized reserve request investment is already allocated.',request['id'])
        return
    if not tasks._exists(c,'work_decisions'):
        return
    if not c.execute("""SELECT 1 FROM work_decisions WHERE subject=? AND statement='reserve' AND status='active'
        AND json_extract(scope_json,'$.task')=?
        AND json_extract(scope_json,'$.object') IN (SELECT value FROM json_each(?)) LIMIT 1""",
        (PRIORITY,task['id'],tasks._json(scopes))).fetchone():
        return
    if c.execute("SELECT 1 FROM work_runs WHERE request_id=? AND native_identity_json IS NOT NULL LIMIT 1",
                 (request['id'],)).fetchone():
        return
    current=projection(c,root,task['id'],compact=True)
    priorities=priority(c,root,task,current['scope_items'])
    if not priorities['reserve_window_open']:
        tasks._fail('reserve_not_ready','Core commitments still have open actions or unresolved mappings.',request['id'])


def _assigned_placements(c, root, task, scopes, explicit):
    """Find original requests that the ordinary dispatch entry can start."""
    from agent_runtime import capability, load_config
    from dsh_capacity import POOL_NAMES
    from harness_hosts import ADAPTER_HOSTS, installed_program
    from harness_package import latest_author
    from harness_scope import build_check
    configs = {}
    # Facts about this commission decide whether it is fresh. A scope may have
    # many valid units, and a previously adopted unit retains its adoption.
    unfinished = {}
    rows = c.execute("""SELECT q.required_scope_json, r.id, r.request_id FROM work_requests q JOIN work_runs r
        ON r.id=(SELECT id FROM work_runs WHERE request_id=q.id ORDER BY created_at DESC,id DESC LIMIT 1)
        WHERE q.task_id=? AND q.kind='plan' AND q.independent=0
        AND (r.stop_requested OR r.outcome!='succeeded')""", (task['id'],))
    for run in rows:
        for sid in json.loads(run['required_scope_json']):
            unfinished.setdefault(sid, {})[run['id']] = run['request_id']
    for row in c.execute("""SELECT * FROM work_requests q WHERE task_id=? AND kind='plan' AND independent=0
            AND NOT EXISTS (SELECT 1 FROM work_runs WHERE request_id=q.id)
            AND NOT EXISTS (SELECT 1 FROM work_returns WHERE request_id=q.id) ORDER BY created_at,id""", (task['id'],)):
        request = tasks._decode(row)
        request_id, scope_ids = request['id'], request['required_scope']
        if (not scope_ids or any(sid not in scopes for sid in scope_ids)
                or latest_author(c, request)):
            continue
        role = request['recipient_role']
        if role not in configs:
            try:
                ref = installed_program(root)
                config = load_config(root, ref['path'], role) if ref else None
                host = ADAPTER_HOSTS[config['host']['adapter']] if config else None
                observed = capability(config) if host in POOL_NAMES and config['programmatic'] else {}
                configs[role] = (host, config['provider'].get('dsh_provider')) if (
                    observed.get('enabled') and observed.get('executable_found')) else None
            except (OSError, RuntimeError, ValueError, KeyError):
                configs[role] = None
        routes = {(explicit[s]['details']['host'], explicit[s]['details'].get('provider'))
                  for s in scope_ids if s in explicit}
        route = next(iter(routes)) if len(routes) == 1 else configs[role]
        try:
            tasks._admit_request(c, task, row, side_effect=True)
        except tasks.WorkConflict:
            continue
        report = build_check(c, root, 'request', request_id, 'dispatch')
        if not report['ready']:
            continue
        if len(routes) > 1 or not route:
            yield {'object': scope_ids[0], 'scope_ids': scope_ids, 'request_id': request_id,
                   'source_ref': request['input_ref'], 'action': 'dispatch', 'depends_on': [],
                   'blocked_by': 'host_selection', 'command': shlex.join(['work-dispatch', '--request', request_id])}
            continue
        host, provider = route
        dependencies = []
        for sid in scope_ids:
            if sid in explicit:
                dependencies.extend(explicit[sid]['details']['depends_on'])
            if tasks._exists(c, 'work_decisions'):
                selected = _selected(c, root, {'task': task['id'], 'object': sid}, PRIORITY)
                if selected and _valid(root, selected):
                    dependencies.extend(selected['details'].get('external_dependencies', []))
        # Sharing a branch proves neither a write conflict nor disjoint objects.
        # Give the lead the original attempts for comparison before execution.
        prior_runs = {rid: qid for sid in scope_ids for rid, qid in unfinished.get(sid, {}).items()}
        command = ['work-dispatch', '--request', request_id]
        if not prior_runs and route == configs[role]:
            command.append('--execute')
        yield {'object': scope_ids[0], 'scope_ids': scope_ids, 'request_id': request_id,
               'source_ref': request['input_ref'], 'action': 'dispatch', 'command': shlex.join(command),
               **({'check_original_runs': [{'request_id': qid, 'run_id': rid,
                        'command': shlex.join(['work-show', '--id', rid, '--detail'])}
                       for rid, qid in sorted(prior_runs.items())]} if prior_runs else {}),
               'host': host, 'provider': provider, 'depends_on': list(dict.fromkeys(dependencies))}


def _placements(c, root, task, projected, capacity):
    from dsh_capacity import snapshot
    capacity = capacity or (lambda host: snapshot(root, host))
    hosts = {}
    scopes = {item['scope_id']:item for item in projected['scope_items']
              if not item.get('disposition') and not item.get('legacy_unindexed')}
    if not scopes:
        return
    explicit = {}
    if tasks._exists(c, 'work_decisions') and c.execute('''SELECT 1 FROM work_decisions
            WHERE subject=? AND json_extract(scope_json,'$.task')=? AND status='active' LIMIT 1''', (READY, task['id'])).fetchone():
        for ident in scopes:
            if not scopes[ident]['remaining']:
                continue
            ready = _selected(c, root, {'task': task['id'], 'object': ident, 'action': 'dispatch'}, READY)
            if ready and _valid(root, ready):
                explicit[ident] = ready
    priorities = priority(c, root, task, projected['scope_items'])
    entries = []
    for ident, ready in explicit.items():
        item = scopes[ident]
        requests = item['responsibility']
        if len(requests) > 1 or item['coverage_returns'] or item['coverage_unindexed']:
            continue
        request_id = requests[0]['request_id'] if requests else None
        latest = next((run for run in item['execution'] if run['request_id'] == request_id), None)
        plan = ready['details']
        if not request_id and item['execution']:
            continue  # An earlier writer's stopped or uncertain effects require its original disposition.
        if latest and (latest['stop_requested'] or latest['state'] != 'succeeded'
                       or not latest['native_identity'] or latest['native_identity']['host'] != plan['host']
                       or tasks.pending_runs(c, request_id)
                       or (latest.get('execution_observation') or {}).get('state') != 'exited'):
            continue
        if request_id and not latest:
            continue  # Check a fresh commission once, through its own dispatch admission below.
        entries.append({'object': ident, 'scope_ids': [ident], 'request_id': request_id, 'source_ref': ready['source_ref'],
                        'action': 'continue' if latest else 'dispatch', **plan,
                        **({'run_id': latest['run_id']} if latest else {})})
    entries.extend(_assigned_placements(c, root, task, scopes, explicit))
    for entry in entries:
        if entry.get('blocked_by') == 'host_selection':
            yield {**entry, 'responsible': json.loads(task['owner_actor'])}
            continue
        deps = _dependency_versions(c, root, task['id'], entry['depends_on'])
        known = [value for value in deps.values() if value]
        failures = [value['id'] for value in known if value['state'] == 'failed']
        entry.update(responsible=json.loads(task['owner_actor']), dependency_versions=deps)
        if set(entry.get('scope_ids', [entry['object']])).intersection(priorities['reserve']) and not priorities['reserve_window_open']:
            yield {**entry, 'blocked_by': 'open_core_commitment'}
            continue
        if failures:
            yield {**entry, 'action': 'resolve_dependency', 'failed_dependencies': failures}
            continue
        if any(value is None or value['state'] != 'released' for value in deps.values()):
            yield {**entry, 'blocked_by': 'dependency'}
            continue
        host = entry['host']
        if host not in hosts:
            try:
                hosts[host] = capacity(host)
            except (OSError, ValueError):
                hosts[host] = {}  # Preserve other work when this host's capacity cannot be read.
        seats = _author_capacity(hosts[host], entry.get('provider'))
        entry['available_author_seats'] = seats
        if not seats:
            entry['blocked_by'] = 'capacity_unproven' if seats is None else 'capacity'
        yield entry


def project(c, root, task, projected, *, actor=None, capacity=None):
    pending, waiting = [], []
    if task['state'] != 'active':
        return {'actionable': pending, 'waiting': waiting, 'stopped': task['state']}
    current = tasks._now()
    deferrals=_deferred(c,task['id'])
    waits_exist = (tasks._exists(c, 'work_decisions') and
                   c.execute('SELECT 1 FROM work_decisions WHERE subject=? LIMIT 1', (WAIT,)).fetchone())
    from task_ledger.role_intake import pending as pending_intake
    from scientific_review.rounds import escalation_items
    entries = (receipt_items(c, task) + list(_placements(c, root, task, projected, capacity))
               + list(pending_intake(c, root, task)) + list(escalation_items(c,task)) + list(_wave_checks(c,root,task)))
    for item in entries:
        if actor and tasks.native_key(actor) != tasks.native_key(item['responsible']):
            continue
        item['item_id'] = item_id(task['id'], item)
        deferred = deferrals.get(item['item_id'])
        if deferred:
            if _waiting(c,root,deferred,current):
                waiting.append({**item,'wait_decision':deferred['id'],'reason_ref':deferred['source_ref'],
                                'recheck_at':deferred['recheck_at'], 'dependency':deferred['dependency'],
                                'external':deferred['external']})
                continue
            item['previous_wait_decision']=deferred['id']
            item['wait_recheck_due']=deferred['recheck_at']
            item['previous_wait']={key:deferred.get(key) for key in ('dependency','external','source_ref')}
        previous = wait = None
        if waits_exist:
            for ident in item.get('scope_ids', [item['object']]):
                reason = _selected(c, root, {'task': task['id'], 'object': ident, 'action': item['action']}, WAIT)
                if not reason:
                    continue
                previous = reason
                detail = reason['details']
                if (_valid(root, reason) and (not detail['recheck_at'] or detail['recheck_at'] > current)
                        and detail['dependency_versions'] == _dependency_versions(c, root, task['id'], detail['depends_on'])):
                    wait = reason
                    break
        if wait:
            waiting.append({**item, 'wait_decision': wait['id'], 'reason_ref': wait['source_ref'],
                            'recheck_at': wait['details']['recheck_at'], 'depends_on':wait['details']['depends_on']})
        elif item.get('blocked_by'):
            waiting.append(item)
        else:
            if previous:
                item['previous_wait_decision'] = previous['id']
                item['wait_recheck_due'] = previous['details']['recheck_at']
            pending.append(item)
    return {'actionable': pending, 'waiting': waiting}


def item_id(task_id, item):
    if item.get('request_id') and item['action'] in ('dispatch', 'resolve_dependency'):
        return _digest([task_id, 'request', item['request_id'], item['action']])
    return _digest([task_id, item['object'], item['action']])


def _display_key(item):
    # Several independent opinions may arrive on the same review request.
    # Preserve its original wait/decision key and track each displayed receipt separately.
    if item['action'] == 'review' and item.get('return_id'):
        return _digest([item['item_id'], item['return_id']])
    return item['item_id']


def _signature(item):
    facts = [item.get('source_ref'), item.get('request_id'), item.get('run_id'),
             item.get('return_id'), item.get('dependency_versions'), item.get('previous_wait_decision'),
             item.get('wait_recheck_due'), tasks.native_key(item['responsible'])]
    if item.get('check_original_runs'):
        facts.append(item['check_original_runs'])
    return _digest(facts)


def _encode_cursor(task_ids, actor, items):
    data = {'selection': _digest([sorted(task_ids), tasks.native_key(actor)]), 'items': items}
    return urlsafe_b64encode(tasks._json(data).encode()).decode().rstrip('=')


def cursor(task_ids, actor, pending):
    return _encode_cursor(task_ids, actor, {_display_key(item): _signature(item) for item in pending})


def pending_page(view, limit, offset):
    result = dict(view)
    for key in ('actionable', 'waiting'):
        # An original dispatch or correction stays visible alongside older adoption receipts.
        rows = sorted(view[key], key=lambda item: item['action'] == 'adopt')
        result[key] = rows[offset:offset + limit]
        result[key + '_count'] = len(rows)
        result[key + '_next_offset'] = offset + limit if offset + limit < len(rows) else None
    return result


def _previous(value, task_ids, actor):
    if not value:
        return {}
    try:
        document = json.loads(urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if not isinstance(document['selection'], str) or not isinstance(document['items'], dict):
            raise ValueError
        return document['items'] if document['selection'] == _digest([sorted(task_ids), tasks.native_key(actor)]) else {}
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError('Use the last cursor for the same original Session and task selection') from exc


def wait_view(task_ids, actor, previous, views, limit=10, offset=0):
    active = [item for view in views.values() for item in view['actionable']]
    signatures = {_display_key(item): _signature(item) for item in active}
    seen = {key: sig for key, sig in signatures.items() if previous.get(key) == sig}
    changes = [i for i in active if _display_key(i) not in seen]
    changes.sort(key=lambda i: i.get('visible_at') or '', reverse=True)
    changes.sort(key=lambda i: i['action'] == 'adopt' and not i.get('previous_wait_decision'))
    displayed = {key: pending_page(view, limit, offset) for key, view in views.items()}
    # The incremental stream advances from its cursor. The offset selects only
    # the browsable inbox. Only the incremental page acknowledges delivery to
    # that stream, so old items can still be enumerated in full.
    new = changes[:limit]
    for item in new:
        seen[_display_key(item)] = signatures[_display_key(item)]
    return {'status': 'actionable' if active else 'current', 'tasks': displayed, 'new': new,
            'new_count': len(changes), 'cursor': _encode_cursor(task_ids, actor, seen)}


def wait(root, task_ids, after, seconds, *, mine=False, cancelled=lambda: False, capacity=None, limit=10, offset=0):
    """Sleep inside the local host, with no transaction or model check in the interval."""
    if not 0 <= seconds <= 1800 or (mine and task_ids) or (not mine and not task_ids):
        raise ValueError('Select exact task IDs or --mine and wait 0..1800 seconds')
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Use limit 1..100 and nonnegative offset.')
    actor = tasks.verified_actor(root)
    deadline = time.monotonic() + seconds
    task_ids = tuple(sorted(set(task_ids)))
    previous = _previous(after, task_ids, actor)
    from harness_scope import projection
    version = None
    with closing(tasks._connect(root, True)) as c:
        while True:
            epoch = c.execute('PRAGMA data_version').fetchone()[0]
            now = time.monotonic()
            if epoch != version or now >= refresh_at:
                c.execute('BEGIN')
                try:
                    tasks.ensure_schema(c)
                    if mine:
                        from harness_snapshot import actor_tasks
                        selected, _ = actor_tasks(c, actor)
                    else:
                        selected = [tasks._row(c, 'work_tasks', ident) for ident in task_ids]
                    from harness_scope import _returns
                    from workspace_queries.scope_work import material_view
                    views = {}
                    for task in selected:
                        if not tasks.participates(c, task, actor):
                            continue
                        receipts = _returns(c, task['id'])
                        scope = projection(c, root, task['id'], compact=True, returns=receipts)
                        pending = project(c, root, task, scope, actor=actor, capacity=capacity)
                        if tasks.native_key(task['owner_actor']) == tasks.native_key(actor):
                            materials = material_view(c, task, scope, pending, receipts, overview=True)
                            pending = {'scope': materials, **pending}
                        views[task['id']] = pending
                finally:
                    c.rollback()
                result = wait_view(task_ids, actor, previous, views, limit, offset)
                if result['status'] == 'actionable' or seconds == 0:
                    return result
                # Capacity or timer changes can occur without a ledger write.
                refresh_at = now + (5 if any(v['waiting'] for v in views.values()) else 60)
                version = epoch
            if cancelled():
                return {**result, 'status': 'cancelled'}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**result, 'status': 'timeout'}
            time.sleep(min(1, remaining))


def _wave_checks(c,root,task):
    done={json.loads(row[0])['bookmark'] for row in c.execute('SELECT payload_json FROM work_task_events WHERE task_id=? AND kind=?',
                                                            (task['id'],WAVE_HANDLED))}
    rows=c.execute("SELECT payload_json,created_at FROM work_task_events WHERE task_id=? AND kind='wave-closed' ORDER BY id",
                   (task['id'],))
    for row in rows:
        wave=json.loads(row['payload_json'])
        if not wave['sponsor_check_due']:
            continue
        decision=wave['decision']
        if decision['id'] not in done and not _selected(c,root,{'task':task['id'],'object':decision['id']},WAVE_CHECK):
            yield {'action':'intake','object':decision['id'],'visible_at':row['created_at'],
                   'source_ref':decision['source_ref'],'responsible':json.loads(task['sponsor_actor'])}


def wave_facts(c, root, task, projected):
    ident = task['id']
    run_cut = c.execute("""SELECT max(e.id) FROM work_run_events e JOIN work_runs r ON r.id=e.run_id
        JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=?""", (ident,)).fetchone()[0]
    task_cut = c.execute('SELECT max(id) FROM work_task_events WHERE task_id=?', (ident,)).fetchone()[0]
    previous = c.execute("SELECT created_at FROM work_task_events WHERE task_id=? AND kind='wave-closed' ORDER BY id DESC LIMIT 1",
                         (ident,)).fetchone()
    attempts = []
    for req in c.execute("SELECT id FROM work_requests WHERE task_id=? AND kind IN ('plan','review')", (ident,)):
        attempts.extend({'run_id': run['id'], 'request_id': req['id'], 'state': run['outcome'],
                         'stop_requested': bool(run['stop_requested']),
                         'native_identity': json.loads(run['native_identity_json']) if run['native_identity_json'] else None}
                        for run in tasks.pending_runs(c, req['id']))
    decisions_since = []
    if tasks._exists(c,'work_decisions'):
        for row in c.execute("""SELECT id,source_ref_json FROM work_decisions WHERE status='active'
            AND json_extract(scope_json,'$.task')=? AND recorded_at>? ORDER BY recorded_at,id""",
                             (ident,previous['created_at'] if previous else '')):
            decisions_since.append({'id':row['id'],'source_ref':json.loads(row['source_ref_json'])})
    return {'task_id': ident, 'cutoff': {'task_event': task_cut, 'run_event': run_cut, 'at': tasks._now()},
            'commitment': json.loads(task['plan_ref_json']), 'plan_revision': task['plan_revision'],
            'state': task['state'], 'owner': json.loads(task['owner_actor']),
            'counts': projected['counts'], 'remaining_scope': projected['remaining_scope'],
            'scope_priority': priority(c,root,task,projected['scope_items']),
            'unresolved_attempts': attempts, 'pending_returns': receipt_items(c, task),
            'decisions':decisions_since,
            'effective_adoptions': [{'id': row['id'], 'return_id': row['return_id'],
                                      'decision_ref': json.loads(row['evidence_ref_json'])}
                                     for row in tasks._effective_dispositions(c, ident)
                                     if not previous or row['created_at']>previous['created_at']]}


def wave_view(c, root, task, projected, selected):
    rows = list(c.execute("""SELECT id,payload_json,created_at FROM work_task_events WHERE task_id=?
                             AND kind='wave-closed' ORDER BY id DESC""", (task['id'],)))
    for row in rows:
        value = json.loads(row['payload_json'])
        if selected == 'latest' or selected == value['decision']['id']:
            return {'status': 'closed', **value, 'wave_event': row['id'], 'closed_at': row['created_at']}
    if selected != 'latest':
        tasks._fail('not_found', 'Unknown wave closure', selected)
    return {'status': 'current', 'facts': wave_facts(c, root, task, projected)}


def close(c, root, actor, decision):
    from harness_scope import projection
    ident = decision['scope']['task']
    task = tasks._row(c, 'work_tasks', ident)
    facts = wave_facts(c, root, task, projection(c, root, ident, compact=True))
    count = c.execute("SELECT count(*) FROM work_task_events WHERE task_id=? AND kind='wave-closed'", (ident,)).fetchone()[0] + 1
    from scientific_review.rounds import escalation_items
    early=[item['source_ref'] for item in escalation_items(c,task) if item.get('recurs_after')]
    tasks._event(c, task, 'wave-closed', actor, {'decision': decision, 'facts': facts,
                 'wave_number': count, 'sponsor_check_due': bool(early) or count % 2 == 0,
                 **({'early_review_sources':early} if early else {})})


def bookmark(root,task_id,decision_id):
    """Index the already made continuation or disposition once at the original cutoff."""
    from harness_scope import _owner,projection
    actor=tasks.verified_actor(root)
    with tasks.transaction(root) as c:
        task=tasks._row(c,'work_tasks',task_id)
        _owner(c,task,actor)
        existing=c.execute("SELECT 1 FROM work_task_events WHERE task_id=? AND kind='wave-closed' AND json_extract(payload_json,'$.decision.id')=?",
                           (task_id,decision_id)).fetchone()
        if not existing:
            row=next((d for d in tasks._effective_dispositions(c,task_id) if d['id']==decision_id),None)
            if row:
                source=json.loads(row['evidence_ref_json'])
            else:
                event=c.execute('''SELECT payload_json FROM work_task_events WHERE task_id=?
                    AND kind IN ('revise','reopen','complete','pause','resume') AND json_extract(payload_json,'$.id')=?''',
                    (task_id,decision_id)).fetchone()
                if not event:
                    raise ValueError('Use an original disposition or accepted plan/continuation decision from this task')
                source=json.loads(event[0])['evidence_ref']
            close(c,root,actor,{'id':decision_id,'scope':{'task':task_id},'source_ref':tasks.evidence_ref(root,source)})
        return wave_view(c,root,task,projection(c,root,task_id,compact=True),decision_id)


def record_wave_review(c, task, actor, bookmark_id, disposition):
    if tasks.native_key(actor)!=tasks.native_key(task['sponsor_actor']):
        tasks._fail('wave_check_authority_required','The original sponsor makes this wave review.',task['id'])
    row=c.execute("SELECT 1 FROM work_task_events WHERE task_id=? AND kind='wave-closed' AND json_extract(payload_json,'$.decision.id')=?",
                 (task['id'],bookmark_id)).fetchone()
    if not row:
        raise ValueError('Cite an original bookmark of this task')
    if not c.execute("SELECT 1 FROM work_task_events WHERE task_id=? AND kind=? AND json_extract(payload_json,'$.bookmark')=?",
                     (task['id'],WAVE_HANDLED,bookmark_id)).fetchone():
        tasks._event(c,task,WAVE_HANDLED,actor,{'bookmark':bookmark_id,'disposition_id':disposition['id'],
                                               'source_ref':disposition['evidence_ref']})
