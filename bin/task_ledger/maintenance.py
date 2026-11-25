"""Maintenance pickup over the original work_requests, never a second queue."""
import contextlib
import hashlib
import json
import re
from pathlib import Path
import shlex
import sqlite3


def core():
    import harness_tasks
    return harness_tasks


def observations(c, task_id, request_id):
    events = c.execute("SELECT payload_json FROM work_task_events WHERE task_id=? AND kind='maintenance-observation' ORDER BY id", (task_id,))
    return [d for r in events if (d := json.loads(r[0])).get('request_id') == request_id]


def claim(root, actor, request_id, *, authorization=None):
    t = core()
    with t.transaction(root) as c:
        actor = t.verified_actor(root, actor)
        req = t._row(c, 'work_requests', request_id)
        if req['kind'] != 'finding' or req['recipient_role'] != 'maintainer':
            raise ValueError('Only an original maintenance finding can be claimed')
        key = t.actor_key(actor)
        if req['recipient_actor'] == key:
            return t._decode(req)
        previous = req['recipient_actor']
        transfer_ref = t._saved_document(root, authorization, allow_text=True)[0] if authorization else None
        # Pickup assigns repair responsibility; it does not attest the report's bytes.
        # Keep the original reference and expose its integrity on the board.
        c.execute('UPDATE work_requests SET recipient_actor=? WHERE id=?', (key, request_id))
        t._event(c, t._row(c, 'work_tasks', req['task_id']), 'maintenance-transfer' if previous else 'maintenance-claim', actor,
                 {'request_id': request_id, 'original_recipient': json.loads(previous) if previous else None, 'recipient': actor, 'authorization_ref': transfer_ref})
        return t._decode(t._row(c, 'work_requests', request_id))


def _runtime_failure_facts(items):
    t = core()
    return {t._json({k: item[k] for k in ('native_item_id', 'kind', 'exit_code') if k in item})
            for item in (items if isinstance(items, list) else []) if isinstance(item, dict)}


def _recorded_runtime_failures(c, root, task_id, finding):
    t = core()
    known = set()
    try:
        original = json.loads(Path(t.evidence_ref(root, json.loads(finding['input_ref_json']))['path']).read_bytes())
        native_result = original.get('result', {})
        if isinstance(native_result, dict):
            known.update(_runtime_failure_facts(native_result.get('tool_failures', [])))
        if original.get('native_status') == 'failed':
            known.add(t._json({'kind': 'native_turn_failed'}))
    except (ValueError, OSError):
        pass  # Missing old evidence cannot suppress a new retained observation.
    for prior in observations(c, task_id, finding['id']):
        known.update(_runtime_failure_facts(prior.get('native_tool_failures', [])))
    return known


def runtime_observation(c, root, task, actor, finding, ref, failures):
    """Append newly observed failures when an older runtime explicitly links its finding."""
    t = core()
    fresh = _runtime_failure_facts(failures) - _recorded_runtime_failures(c, root, task['id'], finding)
    if fresh:
        t._event(c, task, 'maintenance-observation', actor, {
            'request_id': finding['id'], 'result_ref': ref,
            'summary': 'New explicit tool failures in the same original native run; original cause and return point retained.',
            'native_tool_failures': [json.loads(item) for item in sorted(fresh)]})


def retain_runtime_observations(root, actor, run_id, receipt):
    """Expose committed native failure evidence to the existing observation triage."""
    import harness_hook as hook

    t = core()
    with contextlib.closing(t._connect(root, True, timeout=1)) as c:
        actor = t.verified_actor(root, actor)
        run = t._row(c, 'work_runs', run_id)
        ref, original, native = t._native_receipt(root, receipt, run)
        if not c.execute('SELECT 1 FROM work_run_events WHERE run_id=? AND receipt_json=?',
                         (run_id, t._json(ref))).fetchone():
            raise ValueError('Tool triage requires the committed original native run receipt')
        request = t._row(c, 'work_requests', run['request_id'])
        legacy = c.execute('SELECT * FROM work_requests WHERE id=? AND task_id=? AND parent_request_id=?',
                           ('runtime-friction:' + run_id, request['task_id'], request['id'])).fetchone()
        known = _recorded_runtime_failures(c, root, request['task_id'], legacy) if legacy else set()
        parent = parent_finding(c, request, current_owner=False)
        finding_id = legacy['id'] if legacy else parent['id'] if parent else None

    result = original.get('result') or {}
    failures = result.get('tool_failures') or []
    if not failures and original['native_status'] == 'failed':
        failures = [{'kind': 'native_turn_failed', 'tool_name': 'native_turn'}]
    if not failures:
        return []
    turn = native.pop('turn_id', None)
    binding = {**hook.UNKNOWN_RUNTIME, 'config_sha256': original['config_sha256']}
    profile = result.get('tool_profile_sha256')
    if isinstance(profile, str) and re.fullmatch(r'[0-9a-f]{64}', profile):
        binding['profile_sha256'] = profile
    command = ['python3', 'bin/agent_runtime.py', 'readresult', '--run-id', run_id]
    source = {'kind': 'native_run_receipt', 'receipt': ref, 'run_id': run_id, 'request_id': request['id'],
              'native_status': original['native_status'], 'host_source': original['source'],
              'native_thread_or_session_id': native['id'], 'native_turn_id': turn, 'recovery_command': shlex.join(command)}
    if finding_id:
        source['related_finding_id'] = finding_id
    saved = []
    for item in failures:
        if _runtime_failure_facts([item]) <= known:
            continue
        native_item_id = item.get('native_item_id')
        if not isinstance(native_item_id, str) or not native_item_id:
            native_item_id = None
        call_id = native_item_id or ('native-turn:' + hook.digest(run_id)[:32] if item.get('kind') == 'native_turn_failed'
                                    else 'receipt-item:' + hook.digest([run_id, item])[:32])
        failure = {k: item[k] for k in ('kind', 'exit_code') if k in item}
        if item.get('error_class') in ('permission_denied', 'not_found', 'native_tool_error'):
            failure['error_class'] = item['error_class']
        code = failure.get('exit_code')
        if not (failure.get('kind') in ('tool_error', 'native_turn_failed')
                or failure.get('kind') == 'nonzero_exit' and type(code) is int and code):
            continue
        name = item.get('tool_name') or item.get('tool_type') or 'unknown'
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,160}', name):
            name = 'unknown'
        input_hash = item.get('input_sha256')
        if not isinstance(input_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', input_hash):
            input_hash = hook.digest([run_id, call_id])
        occurrence = ({**source, 'native_item_id': native_item_id} if native_item_id else source)
        saved.append(hook.retain_observation(root, {'actor': native, 'tool_name': name, 'tool_use_id': call_id,
            'turn_id': turn, 'observed_at': original['observed_at'], 'failure': failure, 'native_source': occurrence,
            'task_candidates': {request['task_id']: [request['id']]}, 'runtime_binding': binding,
            **({'component': item['component']} if hook.known_failure_component(item.get('component')) else {}),
            'input_sha256': input_hash, 'response_sha256': hook.digest(failure),
            'summary': 'Observed ' + name + ' ' + t._json(failure) + '; cause untriaged. Recover original native run ' + run_id,
            'authority': 'Observed failure only; no scientific coverage, repair assignment or replay permission.'}))
    return saved


def validate_finding_reference(c, task_id, request_id, *, source_request_id=None):
    old = core()._row(c, 'work_requests', request_id)
    if old['kind'] != 'finding' or old['recipient_role'] != 'maintainer':
        raise ValueError('Select an original maintenance finding.')
    if old['task_id'] != task_id:
        confirmed = [event.get('affected', {}) for event in observations(c, old['task_id'], request_id)]
        if not source_request_id or not any(item.get('task_id') == task_id and source_request_id in item.get('request_ids', [])
                                            for item in confirmed):
            raise ValueError('A cross-task observation needs the confirmed original request and finding association.')
    return old


def report(root, actor, task_id, path, *, native_run_id=None, finding_id=None):
    t = core()
    ref, _ = t._saved_document(root, path, allow_text=True)
    summary = Path(ref['path']).read_text(encoding='utf-8')
    ident = 'finding:' + hashlib.sha256(t._json([task_id, ref]).encode()).hexdigest()
    with t.transaction(root) as c:
        actor = t.verified_actor(root, actor)
        if native_run_id:
            run = t._row(c, 'work_runs', native_run_id)
            request = t._row(c, 'work_requests', run['request_id'])
            if (request['task_id'] != task_id or run['outcome'] != 'running'
                    or run['stop_requested'] or not run['native_identity_json']):
                raise ValueError('A native finding belongs to its observed live author run.')
            actor = json.loads(run['native_identity_json'])
        task = t._row(c, 'work_tasks', task_id)
        if finding_id:
            original = validate_finding_reference(c, task_id, finding_id,
                source_request_id=request['id'] if native_run_id else None)
            owner = t._row(c, 'work_tasks', original['task_id'])
            observation = {'request_id': finding_id, 'summary': summary, 'result_ref': ref}
            if owner['id'] != task_id:
                observation['affected'] = {'task_id': task_id, 'request_ids': [request['id']],
                    'original_request_commands': [shlex.join(['python3', 'bin/context.py', '--request', request['id']])]}
            if observation not in observations(c, owner['id'], finding_id):
                t._event(c, owner, 'maintenance-observation', actor, observation)
            return t._decode(original)
        old = c.execute('SELECT * FROM work_requests WHERE id=?', (ident,)).fetchone()
        if old:
            return t._decode(old)
        if native_run_id:
            # The trusted transport supplies the recorded native author, not a
            # caller-controlled CODEX_THREAD_ID or a second reporting queue.
            fields = {'task_id': task_id, 'kind': 'finding', 'sender_actor': t.actor_key(actor),
                      'recipient_actor': None, 'recipient_role': 'maintainer', 'input_ref_json': t._json(ref),
                      'summary': summary, 'plan_revision': task['plan_revision'], 'required_scope_json': '[]',
                      'independent': 0, 'requires_review': 0, 'authors_json': '[]',
                      'parent_request_id': request['id'], 'supersedes_request_id': None}
            return t._decode(t._immutable(c, 'work_requests', ident, fields))
        return t.apply(c, root, 'request', actor, {'id': ident, 'task_id': task_id, 'kind': 'finding',
                       'recipient_role': 'maintainer', 'input_ref': ref, 'summary': summary,
                       'required_scope': [], 'plan_revision': task['plan_revision']})


def queue(root, *, limit=20, offset=0):
    t = core()
    if not isinstance(limit, int) or not 1 <= limit <= 100 or not isinstance(offset, int) or offset < 0:
        raise ValueError('Maintenance pagination requires limit 1..100 and nonnegative offset')
    if not (Path(root) / '.control/harness.sqlite').is_file():
        return {'status': 'UNCONFIGURED', 'findings': [], 'generated': True}
    with t._connect(root, True) as c:
        c.execute('BEGIN')
        if not t._exists(c, 'work_contract'):
            return {'status': 'UNCONFIGURED', 'findings': [], 'generated': True}
        t.ensure_schema(c)
        import harness_feedback
        import harness_knowledge
        rows = c.execute("SELECT * FROM work_requests WHERE kind='finding' AND recipient_role='maintainer' ORDER BY created_at,id").fetchall()
        rows = [r for r in rows if not harness_feedback.maintenance_is_disposed(c, r['id'])]
        findings = []
        for row in rows[offset:offset + limit]:
            item = t._decode(row)
            item['input_integrity'] = harness_knowledge.integrity(root, item['input_ref'])
            task = t._row(c, 'work_tasks', row['task_id'])
            item.update(task_state=task['state'], original_owner=json.loads(task['owner_actor']),
                        pickup='assigned' if row['recipient_actor'] else 'unassigned',
                        original_task_command='python3 bin/harness.py work-show --id ' + row['task_id'])
            item['executions'] = executions(c, root, row['id'])
            item['decision'] = state(c, row['id'])
            item['new_observations'] = new_observations(c, row['id'], item['decision'])
            item['observations'] = observations(c, row['task_id'], row['id'])
            link = c.execute('SELECT * FROM work_maintenance_links WHERE request_id=?', (row['id'],)).fetchone()
            item['maintenance'] = harness_feedback.maintenance_projection(root, c, link) if link else None
            findings.append(item)
        triage = {}
        import harness_hook
        try:
            pending = harness_hook.pending_observations(root, None, all_actors=True, connection=c)
            triage['tool_observations'] = harness_hook.triage_page(pending, limit=min(limit, 10))
        except (ValueError, OSError, sqlite3.Error) as exc:
            triage['tool_observations'] = {'status': 'unavailable', 'reason': str(exc),
                'detail_command': 'python3 bin/harness_hook.py pending --all'}
        return {'status': 'configured', 'generated': True, 'findings': findings, **triage,
                'maintainers': [json.loads(r[0]) for r in c.execute("SELECT actor_json FROM work_roles WHERE org_role='maintainer'")],
                'pagination': {'total': len(rows), 'offset': offset,
                               'next_offset': offset + limit if offset + limit < len(rows) else None},
                'authority': 'Original finding pickup and evidence only; no automatic dispatch or scientific adoption.'}


def parent_finding(c, request, *, current_owner=True):
    """Resolve a bounded engineering child; no role-name or same-task inference."""
    t = core()
    req = dict(request)
    if req['kind'] != 'finding' or req['recipient_role'] != 'executor':
        return None
    parent_id = req.get('parent_request_id')
    parent = c.execute('SELECT * FROM work_requests WHERE id=?', (parent_id,)).fetchone()
    if not parent or parent['kind'] != 'finding' or parent['recipient_role'] != 'maintainer':
        return None  # An ordinary scientific finding retains its scientific route.
    if parent['task_id'] != req['task_id'] or req['independent']:
        raise ValueError('Engineering execution requires its exact original maintenance finding')
    return parent


def delegate(root, actor, request_id, path, *, recipient=None):
    t = core()
    actor = t.verified_actor(root, actor)
    with t._connect(root, True) as c:
        parent = t._row(c, 'work_requests', request_id)
        if parent['kind'] != 'finding' or parent['recipient_role'] != 'maintainer':
            t._fail('maintenance_finding_required', 'Select the original maintenance finding.', request_id)
        task_id = parent['task_id']
    from task_ledger import sessions as harness_sessions
    return harness_sessions.request(root, actor, task_id, path, maintenance_finding=request_id, recipient=recipient)


def executions(c, root, finding_id):
    """Raw repair assignments and native facts; no generated prompts or next actions."""
    t = core()
    result = []
    for row in c.execute("SELECT * FROM work_requests WHERE parent_request_id=? AND kind='finding' AND recipient_role='executor' ORDER BY created_at,id", (finding_id,)):
        request = t._decode(row)
        result.append({'request': request,
            'runs': [t._decode(r) for r in c.execute('SELECT * FROM work_runs WHERE request_id=? ORDER BY created_at,id', (row['id'],))],
            'returns': [t._decode(r) for r in c.execute('SELECT * FROM work_returns WHERE request_id=? ORDER BY created_at,id', (row['id'],))]})
    return result


def state(c, request_id):
    """Latest explicit finding decision. Prose and legacy fields are not control flow."""
    for row in c.execute("SELECT id,kind,payload_json,actor_json,created_at FROM work_task_events WHERE kind IN ('maintenance-note','maintenance-state-import') ORDER BY id DESC"):
        value = json.loads(row['payload_json'])
        if value.get('request_id') == request_id:
            return {**value, 'event_id':row['id'], 'actor':json.loads(row['actor_json']), 'recorded_at':row['created_at']}
    return {'request_id':request_id, 'state':'open'}


def record_note(root, actor, request_id, path, status, *, duplicate_of=None):
    t = core()
    if status not in ('open', 'fixed', 'verified', 'duplicate', 'deferred'):
        raise ValueError('Select an explicit finding disposition.')
    ref, _ = t._saved_document(root, path, allow_text=True)
    with t.transaction(root) as c:
        t.verify_actor(root, actor)
        req = t._row(c, 'work_requests', request_id)
        task = t._row(c, 'work_tasks', req['task_id'])
        if req['kind'] != 'finding' or req['recipient_role'] != 'maintainer':
            raise ValueError('Select the original maintenance finding.')
        if status == 'duplicate':
            target = t._row(c, 'work_requests', duplicate_of)
            if target['kind'] != 'finding' or target['recipient_role'] != 'maintainer':
                raise ValueError('Select the original duplicate finding.')
            seen = {request_id}
            current = duplicate_of
            while current:
                if current in seen:
                    raise ValueError('A duplicate link cannot be circular.')
                seen.add(current)
                current = state(c, current).get('duplicate_of')
        elif duplicate_of:
            raise ValueError('duplicate_of belongs to the duplicate action.')
        value = {'request_id':request_id, 'state':status, 'source_ref':ref}
        if duplicate_of:
            value['duplicate_of'] = duplicate_of
        previous = state(c, request_id)
        if all(previous.get(k) == v for k,v in value.items()) and previous.get('actor') == actor:
            return previous
        t._event(c, task, 'maintenance-note', actor, value)
        return state(c, request_id)


def new_observations(c, request_id, decision):
    rows = c.execute("SELECT payload_json FROM work_task_events WHERE kind='maintenance-observation' AND id>? ORDER BY id", (decision.get('event_id', 0),))
    return [item for row in rows if (item := json.loads(row[0])).get('request_id') == request_id]
