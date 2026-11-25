"""Package input contract; no execution, scientific policy or second state store."""
import json


def launch_spec(root, run):
    """Find the recorded launch facts by their original run binding."""
    from pathlib import Path
    import harness_tasks as t
    for ref in run.get('input_refs', []):
        try:
            spec = json.loads(Path(t.evidence_ref(root, ref)['path']).read_bytes())
        except (OSError, ValueError):
            continue
        if (isinstance(spec, dict) and spec.get('run_id') == run['id']
                and spec.get('request_id') == run['request_id']
                and ('native_cwd' in spec or 'requested_session_id' in spec)):
            home=Path(spec['work_home']) if spec.get('work_home') else Path(ref['path']).parent
            from workspace_files import safe_path
            home=safe_path(root,home,must_exist=False)
            return {**spec, 'evidence_home': str(home)}
    return {}


def author_lineage(c, request):
    """Exact author requests, newest first. Never infer identity from names."""
    import harness_tasks as t
    from task_ledger import finite_work as finite
    if request['independent'] or request['kind'] != 'plan':
        return [request['id']]
    configs = finite.request_configs(c, request['task_id'])
    ids, current = [], request['id']
    while current:
        if current in ids:
            raise ValueError('Cyclic package continuation')
        row = t._row(c, 'work_requests', current)
        if row['task_id'] != request['task_id'] or row['independent'] or row['recipient_role'] != request['recipient_role']:
            raise ValueError('Package continuation must retain the original author responsibility')
        ids.append(current)
        current = configs.get(current, {}).get('continues_request_id')
    return ids


def latest_author(c, request):
    for ident in author_lineage(c, request):
        row = c.execute('SELECT * FROM work_runs WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
                        (ident,)).fetchone()
        if row is not None:
            return row
    return None


def predecessor(root, request):
    import contextlib
    import harness_tasks as t
    with contextlib.closing(t._connect(root, True)) as c:
        row = latest_author(c, request)
        return t._run_view(c, row) if row is not None and row['request_id'] != request['id'] else None


def missing_devin_writer(c, root, request, original_request_id):
    """A new author may take over an ended writer whose Session never received a prompt."""
    import harness_tasks as t
    lineage=author_lineage(c,t._decode(request))
    if original_request_id not in lineage[1:]:
        return None
    if any(t.pending_runs(c,ident) for ident in lineage[1:] if ident!=original_request_id):
        return None
    return ended_devin_writer(c,root,original_request_id)


def ended_devin_writer(c, root, original_request_id):
    """Read the original host's evidence without loading or prompting the Session."""
    from pathlib import Path
    from agent_base import read_receipt
    from devin_acp import pre_prompt_rejection
    from execution_lease import observe_run
    from harness_hosts import confirmed_not_started
    from production_review import scientific_path
    import harness_tasks as t
    request=t._row(c,'work_requests',original_request_id)
    if t._delegation_decisions(c,request['task_id']).get(original_request_id,{}).get('action')!='end':
        return None
    attempts=[t._decode(row) for row in c.execute('SELECT * FROM work_runs WHERE request_id=? ORDER BY created_at DESC,id DESC',
                                               (original_request_id,))]
    if not attempts or c.execute('SELECT 1 FROM work_returns WHERE request_id=? LIMIT 1',(original_request_id,)).fetchone():
        return None
    last=attempts[0]
    if last['target_host']!='devin_cli' or last['outcome']!='failed' or last.get('native_identity'):
        return None
    try:
        last=t._run_view(c,t._row(c,'work_runs',last['id']))
        receipt=read_receipt(root,last['latest_receipt'])
        if not confirmed_not_started(receipt,host='devin_cli',run_id=last['id'],request_id=original_request_id,
                                     config_sha256=last['config_ref']['sha256']):
            return None
        launch=launch_spec(root,last)
        target=launch.get('requested_session_id')
        parent=next((r for r in attempts if r['id']==launch.get('native_parent_run_id')),None)
        if not target or not parent or (parent.get('native_identity') or {}).get('id')!=target:
            return None
        for run in attempts:
            home=launch_spec(root,run).get('evidence_home')
            if not home or run['target_host']!='devin_cli' or observe_run(root,run)['state']!='exited':
                return None
            directory=scientific_path(root,Path(home)/'runs'/run['id'],must_exist=False)
            if not directory.is_dir() or any((directory/'delivery').glob('operation-*')):
                return None  # Existing operation evidence needs its own original reconciliation.
            log=scientific_path(root,directory/'acp.jsonl')
            transcript=[json.loads(line) for line in log.read_bytes().splitlines() if line.strip()]
            if not transcript or any(not isinstance(row,dict)
                    or (row.get('direction')=='client' and row.get('method')=='session/prompt')
                    or (row.get('direction')=='agent' and row.get('method') and 'id' in row) for row in transcript):
                return None
            if run['id']==last['id']:
                fact=pre_prompt_rejection(transcript,session_id=target) or {}
                error=fact.get('error') or {}
                if fact.get('method')!='session/load' or (error.get('data') or {}).get('cognition.ai/errorKind')!='session_not_found':
                    return None
        return {'request_id':original_request_id,'run_id':last['id'],'receipt':last['latest_receipt'],
                'target_session':target,'state':'session_missing_before_prompt'}
    except (OSError,ValueError,KeyError,TypeError):
        return None


def admit_author_run(c, request, operation, previous_id):
    """Called inside the original run-intent transaction, before any native effect."""
    import harness_tasks as t
    from harness_hosts import supports
    ids = author_lineage(c, request)
    latest = latest_author(c, request)
    if operation=='start' and not previous_id and latest and latest['request_id']!=request['id']:
        if proof:=missing_devin_writer(c,t.workspace_root(c),request,latest['request_id']):
            return proof
    for ident in ids:
        for pending in t.pending_runs(c, ident):
            if (pending['id'] == previous_id and pending['request_id'] == request['id'] and operation == 'resume'
                    and pending['outcome'] == 'unknown' and supports(pending['target_host'], 'interrupted_resume')):
                t.session_origin(c, pending)
            else:
                t._fail('run_recovery_required', 'Original package has an in-flight or unknown run; reconcile ' + pending['id'], request['id'])
    if latest is None:
        if operation != 'start' or previous_id:
            t._fail('original_attempt_required', 'Select an existing native attempt before resuming', request['id'])
        return
    if operation != 'resume' or previous_id != latest['id']:
        t._fail('original_attempt_required', 'Resume the exact latest original native attempt for this package', request['id'])
    if latest['request_id'] != request['id']:
        if latest['stop_requested'] or latest['outcome'] != 'succeeded' or not latest['native_identity_json']:
            t._fail('package_recovery_required', 'Recover the original author before continuing its package', latest['id'])
        for ident in ids[1:]:
            stopped = c.execute('''SELECT * FROM work_runs WHERE request_id=?
                ORDER BY created_at DESC,id DESC LIMIT 1''', (ident,)).fetchone()
            if stopped is not None and (stopped['stop_requested'] or stopped['outcome'] == 'stopped'):
                t._fail('package_stopped', 'Original package stop remains in effect', stopped['id'])


def work_home(root, request):
    """Keep recorded working artifacts in place, including pre-refactor runs."""
    import contextlib
    from pathlib import Path
    import hashlib
    import harness_tasks as t
    with contextlib.closing(t._connect(root, True)) as c:
        ids = author_lineage(c, request)
        for ident in ids:
            row = c.execute('SELECT * FROM work_runs WHERE request_id=? ORDER BY created_at,id LIMIT 1', (ident,)).fetchone()
            if row is not None:
                spec = launch_spec(root, t._decode(row))
                if spec.get('evidence_home'):
                    return Path(spec['evidence_home'])
        if request['independent'] and request.get('parent_request_id'):
            parent=t._decode(t._row(c,'work_requests',request['parent_request_id']))
            if parent['task_id']!=request['task_id']:
                raise ValueError('Independent review must retain the original parent task')
            return work_home(root,parent)
        original = t._decode(t._row(c, 'work_requests', ids[-1]))
    from production_review import program_home
    return program_home(original)


def snapshot(root,task_id=None,*,detail=False):
    from harness_snapshot import package_snapshot
    if detail:return historical_snapshot(root,task_id)
    return package_snapshot(root,task_id)


def historical_snapshot(root, task_id=None):
    """Read original requests, explicit coordinates and the existing dsh-batch directory layout."""
    import contextlib
    from pathlib import Path
    import harness_tasks as t
    from task_ledger import finite_work as finite
    packages, unassigned = {}, []
    def load(ref):
        try:
            raw = Path(t.evidence_ref(root, ref)['path']).read_text()
            try:
                value = json.loads(raw)
            except ValueError:
                value = {}
            return value if isinstance(value, dict) else {}, []
        except (OSError, ValueError) as exc:
            return {}, [{'ref': ref, 'error': type(exc).__name__, 'meaning': 'Original evidence unavailable or changed; stage is unknown'}]
    with contextlib.closing(t._connect(root, True)) as c:
        c.execute('BEGIN')
        t.ensure_schema(c)
        tasks = list(c.execute('SELECT * FROM work_tasks' + (' WHERE id=?' if task_id else ''),
                               (task_id,) if task_id else ()))
        for task in tasks:
            configs = finite.request_configs(c, task['id'])
            requests = {r['id']: r for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id', (task['id'],))}
            for request in requests.values():
                if request['kind'] != 'plan' and not request['independent']:
                    unassigned.append(t._decode(request))
                    continue
                package_id = finite._package_id(request, configs)
                if not package_id:
                    continue
                original = requests[package_id]
                ref = json.loads(original['input_ref_json'])
                doc, original_issues = load(ref)
                coordinate = doc.get('package_ref') if isinstance(doc.get('package_ref'), dict) else {}
                home = Path(ref['path']).parent
                label, source = original['summary'], 'original_request'
                if 'dsh-batch' in home.parts:
                    index = home.parts.index('dsh-batch')
                    if len(home.parts) > index + 1:
                        home = Path(*home.parts[:index + 2])
                        label, source = home.name, 'package_directory'
                explicit_id = coordinate.get('id')
                key = task['id'] + ':' + str(explicit_id or (home if source == 'package_directory' else package_id))
                group = packages.setdefault(key, {'id': key, 'package': coordinate.get('name') or label,
                    'packageRoot': coordinate.get('root') or str(home), 'coordinateSource': 'brief' if coordinate else source,
                    'taskId': task['id'],
                    'currentPlan': json.loads(task['plan_ref_json']), 'planRevision': task['plan_revision'],
                    'taskState': task['status'] if 'status' in task.keys() else task['state'],
                    'requestIds': [], 'programs': [], 'stages': [], 'returns': [], 'decisions': [], 'evidenceIssues': []})
                group['requestIds'].append(request['id'])
                current_ref = json.loads(request['input_ref_json'])
                current, current_issues = load(current_ref)
                for issue in original_issues + current_issues:
                    if issue not in group['evidenceIssues']:
                        group['evidenceIssues'].append(issue)
                declared = current.get('stage') or ('data_support' if current.get('data_support') is True else None)
                group['stages'].append({'requestId': request['id'], 'declared': declared,
                    'endpoint': current.get('endpoint'), 'brief': current_ref, 'independent': bool(request['independent']),
                    'createdAt': request['created_at'], 'continuesRequestId': configs.get(request['id'], {}).get('continues_request_id')})
                for run in c.execute('SELECT * FROM work_runs WHERE request_id=? ORDER BY created_at,id', (request['id'],)):
                    native = json.loads(run['native_identity_json']) if run['native_identity_json'] else {}
                    receipts = [json.loads(e['receipt_json']) for e in c.execute(
                        'SELECT receipt_json FROM work_run_events WHERE run_id=? AND receipt_json IS NOT NULL ORDER BY id', (run['id'],))]
                    turns = set()
                    goal = None
                    for receipt in receipts:
                        try:
                            observation = json.loads(Path(t.evidence_ref(root, receipt)['path']).read_bytes())
                            identity = observation.get('native_identity') or {}
                            if identity.get('id') == native.get('id') and identity.get('turn_id'):
                                turns.add(identity['turn_id'])
                                result = observation.get('result') or {}
                                if not isinstance(result, dict):
                                    continue
                                live = result.get('native_goal_observation') or {}
                                if (live.get('sessionId') == identity['id'] and
                                        str(live.get('turn')) == str(identity['turn_id']) and
                                        live.get('source') in ('dsh_live_agent', 'codex_native_rpc')):
                                    goal = {**live, 'receipt': receipt, 'observedAt': 'invocation_end'}
                                else:
                                    persisted = result.get('native_session_observation') or {}
                                    if persisted.get('native_goal'):
                                        goal = {'goal': persisted['native_goal'], 'source': 'persisted_session',
                                                'sessionId': identity['id'], 'turn': identity['turn_id'],
                                                'receipt': receipt, 'observedAt': 'receipt'}
                        except (OSError, ValueError):
                            continue
                    try:
                        launch = launch_spec(root, t._decode(run))
                    except (OSError, ValueError):
                        launch = {}
                    group['programs'].append({'runId': run['id'], 'requestId': request['id'],
                        'operation': run['operation'], 'independent': bool(request['independent']),
                        'sessionId': native.get('id'), 'turns': sorted(turns, key=lambda v: (len(str(v)), str(v))),
                        'nativeStatus': run['outcome'], 'stopRequested': bool(run['stop_requested']),
                        'continuationRef': launch.get('continuation_ref'), 'goalObservation': goal,
                        'updatedAt': run['updated_at'], 'receipt': receipts[-1] if receipts else None})
                for row in c.execute('SELECT * FROM work_returns WHERE request_id=? ORDER BY created_at,id', (request['id'],)):
                    returned = t._decode(row)
                    body, return_issues = load(returned['result_ref'])
                    group['evidenceIssues'].extend(return_issues)
                    group['returns'].append({'id': returned['id'], 'requestId': request['id'],
                        'createdAt': returned['created_at'], 'stage': body.get('stage', body.get('actual_stage')), 'contribution': returned['summary'], 'coverage': returned['coverage'],
                        'artifacts': returned['artifact_refs'], 'remainingIssues': body.get('remaining_issues'),
                        'structuredSummaryAvailable': bool(body),
                        'resultRef': returned['result_ref']})
            for group in (g for g in packages.values() if g['taskId'] == task['id']):
                ids = {r['id'] for r in group['returns']}
                for row in c.execute('SELECT d.* FROM work_dispositions d JOIN work_returns r ON r.id=d.return_id JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY d.created_at,d.id', (task['id'],)):
                    decision = t._decode(row)
                    if decision.get('return_id') in ids:
                        group['decisions'].append(decision)
    return {'source':'original_work_task_store', 'packages': list(packages.values()), 'unassigned': unassigned}


def input_status(root, request):
    import contextlib
    import harness_tasks as t
    from task_ledger import finite_work as finite
    with contextlib.closing(t._connect(root, True)) as c:
        configs = finite.request_configs(c, request['task_id'])
        refs, unavailable = [], []
        for ident in reversed(author_lineage(c, request)):
            for ref in configs.get(ident, {}).get('package_input_refs', []):
                try:
                    frozen = t.evidence_ref(root, ref)
                except (ValueError, OSError) as exc:
                    unavailable.append({'request_id': ident, 'ref': ref, 'reason': str(exc),
                                        'effect': 'Do not use this unavailable version for dependent claims; continue independent work.'})
                    continue
                if frozen not in refs:
                    refs.append(frozen)
    return {'available': refs, 'unavailable': unavailable}


def bound_inputs(root, request):
    """Available original versions; input_status retains all missing dependencies."""
    return input_status(root, request)['available']
