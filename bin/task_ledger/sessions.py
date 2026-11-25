"""Normal session/dispatch adapters over the existing task store and hosts.

No session registry, inbox copy, scheduler or automatic authority inference.
"""
import hashlib
import contextlib
import json
from pathlib import Path
import shlex

import harness_tasks as t


def reference(root, path):
    path = Path(path)
    path = path if path.is_absolute() else Path(root) / path
    return t.evidence_ref(root, {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})


def summary(path, fallback='See the original brief'):
    """Short preview for current views. The evidence reference keeps the full text."""
    with Path(path).open(encoding='utf-8') as source:
        value = source.read(240).strip().splitlines()
    return next((line.strip() for line in value if line.strip()), fallback)


def target(root, value):
    if isinstance(value, str):
        value = {'host': 'codex', 'kind': 'thread', 'id': value, 'instance': value, 'cwd': str(Path(root).resolve())}
    return t._actor(root, value, observed=True)


def enrollment(root, actor, path, role):
    """Record an explicit operator appointment; native identity is host-derived."""
    actor = t.verified_actor(root, actor)
    if role not in t.ORG_ROLES:
        raise ValueError('Choose the appointed organizational role.')
    ref = reference(root, path)
    with t.transaction(root) as c:
        old = c.execute('SELECT * FROM work_roles WHERE actor_key=? AND org_role=?', (t.actor_key(actor),role)).fetchone()
        if old:
            return t._decode(old)
        c.execute('INSERT INTO work_roles VALUES (?,?,?,?,?,?)',
                  (t.actor_key(actor),role,t._json(actor),t.actor_key(actor),t._json(ref),t._now()))
    return {'actor':actor,'org_role':role,'evidence_ref':ref}


def create(root, actor, path, owner_id=None, *, task_id=None, contract_path=None):
    """One original brief and an actual lead; IDs and hashes are host bookkeeping."""
    actor = t.verified_actor(root, actor)
    ref = reference(root, path)
    owner = target(root, owner_id or actor)
    ident = task_id or 'task:' + hashlib.sha256(ref['sha256'].encode()).hexdigest()[:32]
    with contextlib.closing(t._connect(root, True)) as c:
        existing = c.execute('SELECT * FROM work_tasks WHERE id=?', (ident,)).fetchone()
        if existing is None and task_id is None:
            existing = c.execute("SELECT * FROM work_tasks WHERE json_extract(original_ref_json,'$.sha256')=? ORDER BY created_at,id LIMIT 1", (ref['sha256'],)).fetchone()
        if existing is not None:
            return t._decode(existing)
    plan = t._plan_ref(root, ref)
    payload = {'id':ident,'owner_actor':owner,'original_ref':ref,'plan_ref':plan,
               'summary':summary(ref['path']), 'required_scope':[]}
    if contract_path:
        import harness_scope
        harness_scope.migrate(root)
        payload['delivery_contract']=harness_scope.validate_contract(root,json.loads(Path(contract_path).read_bytes()))
    with t.transaction(root) as c:
        return t.apply(c, root, 'create', actor, payload, prepared={'plan': plan})


def request(root, actor, task_id, path, *, maintenance_finding=None, recipient=None, review_of=None,contract_path=None,scope_ids=(),
            continues=None):
    """Register the original complete brief; technical identity comes from the host."""
    actor = t.verified_actor(root, actor)
    task = t._read_one(root, 'work_tasks', task_id)
    home = Path(task['plan_ref']['path']).parent
    ref, _ = t._saved_document(root, path, home=home, label='assignment', allow_text=True)
    import harness_scope
    change=None
    if contract_path:
        harness_scope.migrate(root)
        change=harness_scope.validate_contract(root,json.loads(Path(contract_path).read_bytes()))
    scope_ids=sorted(t._list(list(scope_ids),'Original scope IDs'))
    use={key:change[key] for key in ('purpose','endpoint') if change and key in change}
    identity = [task_id,ref['sha256'],maintenance_finding,recipient,review_of]
    if continues:
        if maintenance_finding or review_of:
            raise ValueError('Only a production assignment can continue the same original delivery unit')
        identity.append(continues)
    legacy_ident='request:'+hashlib.sha256(t._json(identity).encode()).hexdigest()[:32]
    if not (maintenance_finding or review_of):
        identity.extend((scope_ids,use))
    ident = 'request:' + hashlib.sha256(t._json(identity).encode()).hexdigest()[:32]
    # Native ids are history, not a protocol version. Reusing the exact original
    # brief returns its existing assignment, including any stopped/unknown run.
    with contextlib.closing(t._connect(root, True)) as c:
        if not (maintenance_finding or review_of):
            harness_scope._owner(c,t._row(c,'work_tasks',task_id),actor)
        elif review_of and not scope_ids:
            original=t._row(c,'work_requests',review_of)
            if original['task_id']!=task_id:
                raise ValueError('The original author assignment belongs to another task')
            scope_ids=json.loads(original['required_scope_json'])
        expected_recipient = t.actor_key(target(root, recipient)) if recipient else None
        for row in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id', (task_id,)):
            if (not continues and row['kind'] == ('finding' if maintenance_finding else 'review' if review_of else 'plan')
                    and row['parent_request_id'] == (maintenance_finding or review_of)
                    and (row['recipient_actor'] == expected_recipient or row['id']==legacy_ident)
                    and json.loads(row['input_ref_json'])['sha256'] == ref['sha256']):
                existing=t._decode(row)
                if existing['kind']=='plan':
                    if scope_ids and sorted(existing['required_scope'])!=scope_ids:
                        continue
                    recorded=harness_scope.effective_contract(c,existing)['body']
                    if any(recorded.get(key)!=value for key,value in use.items()):
                        continue
                return existing
    try:
        existing = t.read_request(root,ident)
    except t.WorkConflict as exc:
        if exc.code != 'not_found': raise
    else:
        return existing
    body = {'id':ident, 'task_id':task_id, 'kind':'plan', 'recipient_role':'executor',
        'input_ref':ref, 'summary':summary(ref['path']),
        'plan_revision':task['plan_revision'], 'required_scope':list(scope_ids),
        'requires_review':False,
        'finite':True, 'programmatic':True}
    if maintenance_finding:
        body.update(kind='finding', parent_request_id=maintenance_finding, required_scope=[],
                    finite=False, programmatic=False, requires_review=False)
        if recipient: body['recipient']=target(root,recipient)
    elif review_of:
        body.update(kind='review', independent=True, parent_request_id=review_of)
    if continues:
        body['continues_request_id']=continues
    if change is not None:
        body['delivery_contract']=change
    # Input materials stay in the complete brief for the agent to read directly.
    return t.mutate(root,'request',actor,body)


def accept(root, actor, request_id=None, *, task_id=None):
    """Record an exact recipient's acceptance once, without a scientific return."""
    actor = t.verified_actor(root, actor)
    with t.transaction(root) as c:
        if task_id:
            task = t._row(c, 'work_tasks', task_id)
            t._active(task)
            payload = {'task_id': task_id, 'original_ref': json.loads(task['original_ref_json'])}
            t.evidence_ref(root, payload['original_ref'])
            found = c.execute("SELECT 1 FROM work_task_events WHERE task_id=? AND kind='task-accepted' AND actor_json=? AND payload_json=?",
                              (task_id, t._json(actor), t._json(payload))).fetchone()
            if not found:
                t._event(c, task, 'task-accepted', actor, payload)
            return {'task_id': task_id, 'status': 'accepted', 'actor': actor,
                    'meaning': 'Acceptance of existing task ownership; no duplicate scientific request/return.'}
        req = t._row(c, 'work_requests', request_id)
        task = t._row(c, 'work_tasks', req['task_id'])
        t._admit_request(c, task, req)
        t.evidence_ref(root, json.loads(req['input_ref_json']))
        previous = c.execute("SELECT 1 FROM work_task_events WHERE task_id=? AND kind='request-accepted' AND actor_json=? AND json_extract(payload_json,'$.request_id')=?", (task['id'],t._json(actor),request_id)).fetchone()
        if previous is None:
            t._event(c, task, 'request-accepted', actor, {'request_id': request_id, 'input_ref': json.loads(req['input_ref_json'])})
    return {'request_id': request_id, 'status': 'accepted', 'actor': actor,
            'context': 'python3 bin/context.py --request ' + shlex.quote(request_id),
            'meaning': 'Receipt of assigned responsibility; no result, adoption or data permission.'}


def dispatch(root, actor, request_id=None, *, task_id=None, config=None, execute=False,
             instructions=None, run_id=None, resume_stopped=False, continue_original=False, goal_max_rounds=None,basis=None):
    """Observe or explicitly run the selected original assignment."""
    actor = t.verified_actor(root, actor)
    if task_id:
        if execute or instructions or run_id or resume_stopped or continue_original or goal_max_rounds is not None or basis:
            raise ValueError('Select the exact assignment to execute.')
        return {'task_id':task_id, 'task':t.read_task(root,task_id)['task']}
    if instructions and continue_original:
        raise ValueError('--continue and --instructions are mutually exclusive.')
    if (instructions or resume_stopped or continue_original or goal_max_rounds is not None or basis) and not execute:
        raise ValueError('Continuing a Session requires --execute.')
    request = t.read_request(root, request_id)
    import run_worker
    from harness_hosts import installed_program
    if execute:
        if request['kind']=='plan' and not request['independent']:
            from harness_scope import _owner
            with contextlib.closing(t._connect(root,True)) as c:
                _owner(c,t._row(c,'work_tasks',request['task_id']),actor)
        return run_worker.programmatic(root, request_id, config_path=config,
            instructions_path=instructions, exact_run_id=run_id, resume_stopped=resume_stopped,
            continue_original=continue_original,goal_max_rounds=goal_max_rounds,basis=basis)
    run = run_worker._selected_run(root, request_id)
    selected = reference(root, config) if config else run['config_ref'] if run else installed_program(root)
    result={'request_id':request_id,'transport':'programmatic','configuration':selected,'request':request,'run':run}
    if request['kind']=='plan' and not request['independent']:
        from harness_scope import request_progress
        with contextlib.closing(t._connect(root,True)) as c:
            result['progress']=request_progress(c,request)
    return result


def overview(root, actor, *, limit=10, offset=0, assignments_only=False, timeout=30):
    """Read actual ownership, native activity and original reports."""
    actor = t.verified_actor(root, actor)
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Use limit 1..100 and a nonnegative offset.')
    with contextlib.closing(t._connect(root, True, timeout=timeout)) as c:
        c.execute('BEGIN')
        roles = sorted(t._roles(c, actor))
        items = []
        for task in c.execute('SELECT * FROM work_tasks ORDER BY updated_at DESC,id'):
            if task['state'] in ('completed','cancelled'):
                continue
            if assignments_only and not t.participates(c, task, actor):
                continue
            snapshot = t._snapshot(c, task)
            items.append({'task': snapshot['task'], 'requests': snapshot['requests'],
                          'runs': snapshot['runs'], 'returns': snapshot['returns'],
                          'decisions': snapshot['dispositions']})
    result = {'actor': actor, 'roles': roles, 'tasks': items[offset:offset+limit],
              'pagination': {'total':len(items),'offset':offset,
                            'next_offset':offset+limit if offset+limit<len(items) else None}}
    if not assignments_only:
        from task_ledger import maintenance as harness_maintenance
        result['maintenance'] = harness_maintenance.queue(root,limit=limit,offset=offset)
    return result
