"""One technical continuation of an exited DSH run on its original Session."""
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time

import dsh_capacity as capacity
import execution_lease as execution
import harness_tasks as tasks
from agent_base import read_receipt
from harness_package import author_lineage, latest_author, launch_spec

WINDOW = 600
FAULT = 'dsh-transport-fault'
CLAIM = 'dsh-auto-continuation'
FUSE = 'dsh-auto-fuse'
DEFERRED = 'dsh-auto-deferred'
WATCH = 'dsh-recovery-attached'
HANDLED = 'dsh-recovery-handled'
PROVIDER_FAULTS = ('transport_timeout','sse_json_decode')


def pending_view(run):
    if run.get('stop_requested') or run.get('outcome') in ('stopped', 'succeeded'):
        return None
    for event in reversed(run.get('events', ())):
        if event['kind'] == HANDLED:
            return None
        if event['kind'] == CLAIM:
            return {'state':'claimed', 'next_run_id':event['payload']['next_run_id']}
        if event['kind'] == DEFERRED:
            return event['payload']
        if event['kind'] == WATCH:
            return {'state':'checking_original'} if run.get('outcome') in ('failed', 'unknown') else None
    return None


def recovery_view(run):
    if pending := pending_view(run):
        return pending
    for event in reversed(run.get('events', ())):
        if event['kind'] == HANDLED:
            return event['payload'] if event['payload'].get('state') != 'nothing_to_resume' else None
        if event['kind'] == WATCH:
            break
    return None
MALFORMED_SSE = 'Error reading response: malformed server-sent event JSON.'


def classify(error):
    if not isinstance(error, dict):
        return None
    if error.get('code') in ('TIMEOUT', 'LLM_STREAM_IDLE_TIMEOUT') or error.get('name') == 'APIConnectionTimeoutError':
        return 'transport_timeout'
    if error.get('message') == MALFORMED_SSE:
        return 'sse_json_decode'
    return None


def _provider_events(c, kind, provider, *, after=0):
    rows = c.execute('SELECT id,run_id,payload_json FROM work_run_events WHERE kind=? AND id>? '
                     'AND json_extract(payload_json,\'$.provider\')=? ORDER BY id DESC', (kind,after,provider))
    events = [{**json.loads(row['payload_json']), 'event_id':row['id'], 'run_id':row['run_id']} for row in rows]
    # Older releases treated the loss of the local observer as a provider fault.
    return [event for event in events if event.get('kind') in PROVIDER_FAULTS] if kind==FAULT else events


def effective_fuse(c, provider):
    legacy_faults = None
    for event in _provider_events(c,FUSE,provider):
        if event.get('basis')=='observed_transport' or event['state']=='reset':
            return event
        if legacy_faults is None:
            legacy_faults = _provider_events(c,FAULT,provider)
        through = event['until']-WINDOW
        observed = [f['observed_at'] for f in legacy_faults if f['event_id'] < event['event_id']
                    and through-WINDOW <= f['observed_at'] <= through]
        if len(observed)>=2:
            return {**event,'until':max(observed)+WINDOW}
    return {}


def _diagnostics(root, run):
    home = launch_spec(root, run).get('evidence_home')
    return Path(home) / 'runs' / run['id'] / 'diagnostics' if home else None


def _sse_request(root, run, kind):
    home = _diagnostics(root, run)
    if kind != 'sse_json_decode' or not home:
        return None
    ids = set()
    for path in home.glob('sse-*.json'):
        try:
            value = json.loads(path.read_bytes())
        except (OSError,ValueError):
            continue
        if value.get('schema') == 'htam.dsh-sse-diagnostic.v1' and value.get('error') == MALFORMED_SSE:
            headers = value.get('request_ids') or {}
            if request_id := next((headers[k] for k in ('x-request-id','request-id','x-deepseek-request-id','traceparent') if k in headers),None):
                ids.add(request_id)
    return next(iter(ids)) if len(ids) == 1 else None


def transport_request(root, run, result, kind):
    diagnostic = result.get('transport_diagnostic') or {}
    ids = diagnostic.get('request_ids') or (diagnostic.get('response') or {}).get('request_ids') or {}
    return (next((ids[key] for key in ('x-request-id','request-id','x-deepseek-request-id','traceparent')
                  if isinstance(ids.get(key),str) and ids[key]), None) or _sse_request(root,run,kind)
            or diagnostic.get('client_traceparent'))


def _session(fault):
    return fault.get('native_session') or fault['native_turn'].rsplit(':',1)[0]


def _same_fault(observed, current):
    return (observed['run_id']==current['run_id'] or observed['native_turn']==current['native_turn']
            or bool(current.get('native_request') and observed.get('native_request')==current['native_request']))


def transport_budget(history, current):
    """At most one retry after the first same-cause failure without new progress."""
    streak = 1
    if current.get('verified_progress'):
        return streak
    counted=[current]
    for fault in sorted(history,key=lambda value:(value['observed_at'], value.get('event_id',0)), reverse=True):
        if (_session(fault)!=_session(current) or any(_same_fault(fault,prior) for prior in counted)
                or fault['observed_at']>current['observed_at']):
            continue
        counted.append(fault)
        if fault['kind']==current['kind']:
            streak += 1
        if fault.get('verified_progress'):
            break
    return streak


def _observed(run, result, provider):
    native = run['native_identity'] or {}
    log = result.get('native_session_observation') or {}
    requests = log.get('requests') or []
    if (log.get('source') == 'dsh_persisted_session_log' and log.get('session_id') == native.get('id')
            and str(log.get('latest_turn')) == native.get('turn_id') and requests
            and all(row.get('provider') == provider for row in requests)):
        return log
    return {}


def recent_timeout_deadline(run, result, provider):
    log = _observed(run, result, provider)
    if (run['outcome'] != 'failed' or log.get('terminal') != 'error'
            or classify(log.get('native_error')) != 'transport_timeout'):
        return 0
    finished = ((result.get('turn_activity') or {}).get('native_terminal_at')
                or (result.get('metering') or {}).get('finished_at'))
    try:
        ended = datetime.fromisoformat(finished)
    except (TypeError, ValueError):
        return 0
    now = time.time()
    if ended.tzinfo and now-WINDOW <= ended.timestamp() <= now:
        return round((ended.timestamp() + WINDOW) * 1000)
    return 0


def verified_progress(root, run_home, binding, started, proposed_reviews):
    """Find new, attributable results in the last turn's host-owned evidence."""
    from delivery_evidence import CLI_SCHEMA, _json, _read_success, _subject, group_snapshot
    from workspace_files import sha256
    run_id, request_id = binding['run_id'], binding['request_id']
    facts = []
    try:
        with contextlib.closing(tasks._connect(root,True)) as c:
            for row in c.execute("SELECT payload_json FROM work_run_events WHERE run_id=? "
                                 "AND kind='review-delivered' AND created_at>=?",(run_id,started)):
                delivery = json.loads(row[0])
                facts.append({'kind':'review_received', 'return_id':delivery['return_id'], 'ref':delivery['opinion_ref']})
            for review_id in sorted(proposed_reviews):
                row = c.execute('SELECT input_ref_json FROM work_requests WHERE id=? AND parent_request_id=? '
                                'AND independent=1 AND created_at>=?',(review_id,request_id,started)).fetchone()
                if row:
                    ref = json.loads(row[0])
                    packet = _json(root,ref)
                    if isinstance(packet,dict) and packet.get('author_run_id') == run_id:
                        facts.append({'kind':'review_registered', 'review_id':review_id, 'ref':ref})
    except (OSError, ValueError, sqlite3.Error, KeyError):
        pass

    for path in sorted((run_home/'delivery').glob('operation-*/record.json')):
        try:
            raw = path.read_bytes()
            record = json.loads(raw)
            if (record.get('schema') != CLI_SCHEMA or record.get('source') != 'trusted_public_cli'
                    or record.get('run_id') != run_id or record.get('request_id') != request_id
                    or (record.get('started_at') or '') < started or record.get('capture_complete') is not True
                    or record.get('exit_code') != 0 or '--dry-run' in record.get('argv',[])):
                continue
            operation = record['operation']
            if not (operation.endswith((' create',' save')) or operation == 'dataset model result-group'):
                continue
            reply = _json(root,record.get('stdout_ref'))
            if not isinstance(reply,dict) or not _read_success(reply):
                continue
            data = reply.get('data') or {}
            if operation.endswith((' create',' save')):
                readback = data.get('readback') or {}
                if (reply.get('outcome') not in ('dataset_written','dataset_created','dataset_saved')
                        or readback.get('contentMatchesIntent') is not True
                        or not _subject(readback.get('subject'))
                        or _subject(readback.get('subject')) != _subject(data.get('subject'))):
                    continue
            else:
                intent = _json(root,record.get('input_ref')) or {}
                if intent.get('action') != 'save' or data.get('status') != 'verified' or not group_snapshot(data):
                    continue
            facts.append({'kind':'write_readback', 'ref':{'path':str(path),'sha256':sha256(raw)}})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return facts


def _unresolved_operations(c, root, request):
    """Trust the actual original operation archives, including unfinished runs of tools."""
    from delivery_evidence import CLI_SCHEMA, _json, mutation_attempt, operation, operation_effect, terminal_mutation
    unknown, resolved = {}, set()
    for ident in author_lineage(c, request):
        for row in c.execute('SELECT * FROM work_runs WHERE request_id=?',(ident,)):
            run = tasks._decode(row)
            home = launch_spec(root, run).get('evidence_home')
            if not home:
                continue
            for path in (Path(home) / 'runs' / run['id'] / 'delivery').glob('operation-*'):
                try:
                    record = json.loads((path/'record.json').read_bytes())
                except (OSError,ValueError):
                    try:
                        journal = json.loads((path/'dispatch.json').read_bytes())
                    except (OSError, ValueError):
                        journal = {}
                    if (isinstance(journal, dict) and journal.get('schema') == 'htam.cli-dispatch.v1'
                            and journal.get('run_id') == run['id'] and journal.get('request_id') == ident
                            and journal.get('read_only') is True):
                        argv = journal.get('argv')
                        if argv is None or (operation(argv)==journal.get('operation')
                                and operation_effect(argv,_json(root,journal.get('input_ref')))=='read_only'):
                            continue
                    return {'run_id': run['id'], 'archive': path.relative_to(root).as_posix(),
                            'issue': 'missing_final_operation_receipt'}
                if not isinstance(record,dict):
                    return {'run_id': run['id'], 'issue': 'unreadable_final_operation_receipt'}
                if (record.get('schema')!=CLI_SCHEMA or record.get('run_id')!=run['id']
                        or record.get('request_id')!=ident or record.get('source')!='trusted_public_cli'):
                    return {'run_id':run['id'],'archive':path.relative_to(root).as_posix(),
                            'issue':'operation_binding_mismatch'}
                action = record.get('operation','')
                if record.get('dispatch') == 'not_started':
                    continue
                request_value = _json(root,record.get('input_ref')) or {}
                if not isinstance(request_value,dict):
                    request_value={}
                argv = record.get('argv')
                if operation(argv)!=action:
                    return {'run_id':run['id'],'archive':path.relative_to(root).as_posix(),
                            'issue':'operation_binding_mismatch','operation':action}
                # An exit of zero can be a structured continuation/unknown result.
                reply = _json(root, record.get('stdout_ref')) or {}
                if not isinstance(reply,dict):
                    reply={}
                key=mutation_attempt(action,reply,request_value)
                captured=record.get('capture_complete') is True and record.get('input_unchanged') is True
                terminal=captured and reply.get('operation')==action and terminal_mutation(reply)
                if operation_effect(argv,request_value)=='read_only' and not record.get('write_authority'):
                    if (action=='dataset model result-group' and request_value.get('action')=='recover'
                            and terminal and key):
                        resolved.add(key)
                    continue
                error = reply.get('error') or {}
                if terminal and (key or action=='dataset model result-group' and reply.get('outcome')=='draft_validation_failed'):
                    if key:
                        resolved.add(key)
                else:
                    unknown[key or (action,str(path))] = {'run_id': run['id'], 'archive': path.relative_to(root).as_posix(),
                                       'issue': 'operation_result_unverified', 'operation': action,
                                       **({'operation_id':key[-1]} if key else {}),
                                       **({'error_code':error['code']} if isinstance(error,dict) and isinstance(error.get('code'),str) else {})}
    return next((value for key, value in unknown.items() if key not in resolved), None)


def _lineage_stamp(c, request):
    lineage = author_lineage(c, request)
    return c.execute(f"""SELECT coalesce(max(e.id),0) FROM work_run_events e
        JOIN work_runs r ON r.id=e.run_id WHERE r.request_id IN ({','.join('?' for _ in lineage)})
        AND e.kind IN ('intent','run-bind','run-outcome','unknown','stop_requested','delivery-evidence')""",
        lineage).fetchone()[0]


def _has_checkpoint(c, root, request, run, goal_id):
    lineage = author_lineage(c, request)
    for row in c.execute(f"""SELECT * FROM work_runs WHERE request_id IN ({','.join('?' for _ in lineage)})
                    AND id!=? AND created_at<=? ORDER BY created_at DESC""",
                         (*lineage, run['id'], run['created_at'])):
        previous = tasks._run_view(c, row)
        if ((previous.get('native_identity') or {}).get('id') != run['native_identity']['id']
                or not previous['latest_receipt']):
            continue
        try:
            result = read_receipt(root, previous['latest_receipt']).get('result') or {}
        except (OSError, ValueError, RuntimeError):
            continue
        goal = (result.get('native_session_observation') or {}).get('native_goal') or {}
        if (goal.get('id') == goal_id and ((result.get('turn_activity') or {}).get('ending_turn_verified_progress')
                                          or previous['return_ids'])):
            return True
    return False


def _ready(c, root, request, run, config, result, log, delivery_effects=None):
    if (run['stop_requested'] or log.get('unreturned_tool_calls') != [] or result.get('input_issues')
            or execution.observe_run(root, run)['state'] != 'exited'):
        return False
    activity = result.get('turn_activity') or {}
    if activity.get('unreturned_tools', 0) != 0:
        return False
    if request['independent']:
        return (config.get('role') == 'scientific_reviewer' and config.get('profile') == 'read-only-evidence'
                and not run.get('return_ids') and not tasks.check_inputs(root, [request['input_ref']]))
    if request['recipient_role'] != 'executor':
        return False
    live = result.get('native_goal_observation') or {}
    goal = log.get('native_goal') or {}
    if live.get('goal') and any(live['goal'].get(k) != goal.get(k) for k in ('id','revision','phase')):
        return False
    used, limit = goal.get('roundsStarted'), goal.get('maxGoalRounds')
    if (not goal.get('id') or goal.get('phase') != 'active' or log.get('goal_observation_issue')
            or type(used) is not int or type(limit) is not int or not 0 <= used < limit):
        return False
    # A confirmed active Goal can lose its initial turn after successful reads
    # but before an autonomous round or candidate exists. It has the same
    # bounded transport retry as any other safe original attempt.
    first_goal = (used == 0 and str(log.get('latest_turn')) == '1'
                  and live.get('sessionId') == run['native_identity']['id']
                  and (live.get('goal') or {}).get('roundsStarted') == 0
                  and live['goal'].get('activation') == 'disarmed')
    if (first_goal or activity.get('ending_turn_verified_progress') or activity.get('ending_turn_tool_calls') == 0
            or log.get('tool_calls') == [] or _has_checkpoint(c,root,request,run,goal['id'])):
        return True
    # A frozen validation input is sufficient to resume once after transport
    # failure. It does not reset the retry budget or establish a remote write.
    from delivery_evidence import handoff_effects
    effects=delivery_effects or handoff_effects(c,root,request,run=run,result=result)
    return bool(effects and effects.get('run_id')==run['id'] and effects.get('prepared_candidate_refs'))


def _active_probe(c, provider):
    settled = {e.get('settled_probe') for e in _provider_events(c, FUSE, provider)}
    return next((p for p in _provider_events(c, CLAIM, provider)
                 if p['probe'] and p['next_run_id'] not in settled), None)


def _probe_result(root, probe, *, manager_finished=False):
    """Read the probe outside the shared capacity and ledger write locks."""
    with contextlib.closing(tasks._connect(root, True)) as c:
        row = c.execute('SELECT * FROM work_runs WHERE id=?', (probe['next_run_id'],)).fetchone()
        if row is None:
            if not manager_finished and execution.matches(probe['manager']) is not False:
                return None
            return 'inconclusive'
        child = tasks._run_view(c, row)
        if child['outcome'] not in ('succeeded', 'failed', 'stopped', 'unknown'):
            return None
        if execution.observe_run(root, child)['state'] != 'exited':
            return None
        intent = c.execute("SELECT payload_json FROM work_run_events WHERE run_id=? AND kind='intent' ORDER BY id LIMIT 1",
                           (child['id'],)).fetchone()
        started = intent and json.loads(intent[0]).get('dsh_auto_continue_from') == probe['run_id']
        result = read_receipt(root, child['latest_receipt']).get('result') or {} if child['latest_receipt'] else {}
        log = _observed(child, result, probe['provider'])
        # A completed model result proves transport recovery even when an
        # independent data effect still needs its own readback.
        if started and child['outcome']=='succeeded' and log.get('terminal')=='completed' and log.get('unreturned_tool_calls')==[]:
            return 'succeeded'
        if started and failure_kind(root,child,result,log) in PROVIDER_FAULTS:
            return 'provider_failure'
        return 'inconclusive'


def _finish_probe(c, actor, source, probe, now, outcome):
    later_fault = _provider_events(c, FAULT, probe['provider'], after=probe['event_id'])
    if later_fault or outcome=='provider_failure':
        state = {'state':'open','until':max([now,*[e['observed_at'] for e in later_fault]])+WINDOW}
    elif outcome=='succeeded':
        state = {'state':'reset','until':0}
    else:
        previous = effective_fuse(c,probe['provider'])
        state = {'state':previous.get('state','reset'),'until':previous.get('until',0)}
    tasks._run_event(c, source, FUSE, actor, None,
                     {'provider':probe['provider'], **state, 'settled_probe':probe['next_run_id'],
                      'basis':'observed_transport','probe_outcome':outcome})
    return state


def _fault(run, result, log):
    if run['outcome'] == 'failed' and log.get('terminal') == 'error' and result.get('native_status') == 'failed':
        return classify(log.get('native_error'))
    if (run['outcome'] == 'unknown' and result.get('native_status') == 'unknown' and log
            and log.get('terminal') in (None, 'aborted') and result.get('transport_status') == 'model_watchdog_timeout'):
        return 'transport_timeout'
    if (run['outcome'] == 'unknown' and result.get('native_status') == 'unknown' and log
            and log.get('terminal') == 'aborted' and result.get('transport_status') == 'original_control_disconnected'):
        return 'interrupted_transport'
    if (run['outcome'] == 'unknown' and result.get('native_status') == 'unknown' and log
            and not log.get('terminal') and (result.get('exit_code') not in (None, 0)
                or result.get('transport_status') == 'interrupted; native turn unresolved')):
        return 'interrupted_transport'
    return None


def failure_kind(root,run,result,log):
    """Use initiating local evidence as well as the later native observation."""
    kind = _fault(run,result,log)
    if not kind or run['outcome']!='unknown':
        return kind
    originals = []
    for event in run.get('events',()):
        if event['kind']!='unknown' or not event.get('receipt'):
            continue
        try:
            original=read_receipt(root,event['receipt'])
        except (ValueError,OSError,KeyError):
            continue
        originals.append((original.get('observed_at') or event['created_at'],original))
    if diagnostic_home := _diagnostics(root, run):
        # A locked ledger can also prevent indexing the first error receipt.
        # The trusted manager archives it by the exact run before attempting SQL.
        from workspace_files import sha256
        prefix = sha256(run['id'].encode())[:24]
        for path in diagnostic_home.parent.glob('runtime-'+prefix+'-*.json'):
            try:
                if path.is_symlink():
                    continue
                raw=json.loads(path.read_bytes())
                if raw.get('schema')=='data-workspace.agent-runtime-observation.v1' and raw.get('observed_at'):
                    originals.append((raw['observed_at'],raw))
            except (ValueError,OSError,TypeError,AttributeError):
                continue
    first = min((item for item in originals if isinstance(item[0],str) and isinstance(item[1],dict)
                 and item[1].get('source')=='adapter_exception' and item[1].get('run_id')==run['id']
                 and item[1].get('request_id')==run['request_id']
                 and item[1].get('config_sha256')==run['config_ref']['sha256']),key=lambda item:item[0],default=None)
    if first:
        original=first[1];origin=original.get('failure_origin') or {}
        if (origin.get('surface')=='workspace_ledger' or original.get('error_type')=='OperationalError'
                and original.get('detail') in ('database is locked','database table is locked')):
            return 'local_ledger_contention'
    return kind


def _defer(c, source, actor, provider, child, fault, until):
    value = {'provider':provider, 'state':'cooldown', 'next_run_id':child, 'fault':fault, 'until':until}
    row = c.execute('SELECT payload_json FROM work_run_events WHERE run_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                    (source['id'], DEFERRED)).fetchone()
    if not row or json.loads(row[0]) != value:
        tasks._run_event(c, source, DEFERRED, actor, None, value)
    return value


def consider(root, actor, request, run, config, next_run_id, *, recover_claim=False, delivery_effects=None):
    provider = config['provider'].get('dsh_provider')
    if not capacity.recovery_enabled(root, provider) or not run.get('latest_receipt'):
        return None
    if (not capacity.is_pilot_provider(root, provider) and not any(e['kind'] == WATCH
            and e['payload'].get('provider') == provider for e in run.get('events', ()))):
        return None
    result = read_receipt(root, run['latest_receipt']).get('result') or {}
    log = _observed(run, result, provider)
    kind = failure_kind(root,run,result,log)
    if not kind:
        return None
    now = time.time()
    finished = ((result.get('turn_activity') or {}).get('native_terminal_at')
                or (result.get('metering') or {}).get('finished_at'))
    stamp = datetime.fromisoformat(finished).timestamp() if finished else now
    native = run['native_identity']
    native_turn = native['id'] + ':' + native['turn_id']
    native_request = transport_request(root, run, result, kind)
    progress = (result.get('turn_activity') or {}).get('ending_turn_verified_progress')
    current_fault = {'provider':provider,'kind':kind,'run_id':run['id'],'native_turn':native_turn,
                     'native_session':native['id'],'native_request':native_request,'observed_at':stamp,
                     'verified_progress':bool(progress),
                     'runtime_role':'scientific_reviewer' if request['independent'] else request['recipient_role']}
    with contextlib.closing(tasks._connect(root, True)) as read:
        read.execute('BEGIN')
        probe = _active_probe(read, provider)
        lineage_stamp = _lineage_stamp(read, request)
        ready = next_run_id and _ready(read,root,request,run,config,result,log,delivery_effects)
        unresolved = _unresolved_operations(read, root, request) if ready else None
        prior = next((p for p in _provider_events(read,CLAIM,provider) if p['run_id']==run['id']),None) if recover_claim else None
    probe_result = _probe_result(root, probe) if probe else None
    can_readopt = prior and execution.matches(prior['manager']) is False
    manager = execution.process_identity(os.getpid())

    with tasks.transaction(root) as c:
        if not capacity.recovery_enabled(root, provider):
            return None
        source = tasks._row(c, 'work_runs', run['id'])
        latest = effective_fuse(c,provider)
        faults = _provider_events(c, FAULT, provider)
        earlier = next((fault for fault in faults if _same_fault(fault,current_fault)),None)
        if kind in PROVIDER_FAULTS and not earlier:
            tasks._run_event(c, source, FAULT, actor, None,
                             {**current_fault,'original_receipt_ref':run['latest_receipt']})
            cutoff = now-WINDOW
            recent = [f for f in faults if f['event_id'] > (latest['event_id'] if latest.get('state') == 'reset' else 0)
                      and cutoff <= f['observed_at'] <= now and cutoff <= stamp <= now
                      and _session(f)!=native['id'] and f['kind']==kind]
            already_open = latest.get('state') == 'open'
            if (already_open and latest['until'] > now) or (not already_open and recent):
                until = max(latest.get('until', 0), max([stamp, *[f['observed_at'] for f in recent]]) + WINDOW)
                tasks._run_event(c,source,FUSE,actor,None,{'provider':provider,'state':'open',
                                 'until':until,'basis':'observed_transport'})
                latest = {'state':'open','until':until}

        current_probe = _active_probe(c, provider)
        if current_probe and (probe or {}).get('event_id') == current_probe['event_id'] and probe_result is not None:
            latest = _finish_probe(c, actor, source, current_probe, now, probe_result)
            current_probe = None
        if kind in PROVIDER_FAULTS and transport_budget(faults,earlier or current_fault)>=2:
            return {'state':'session_retry_limit','fault':kind,'session_id':native['id'],
                    'reason':'Two distinct same-cause transport failures with no new verified progress.'}
        if not ready:
            return {'state':'ineligible','fault':kind}
        active_request = tasks._row(c,'work_requests', request['id'])
        task = tasks._row(c,'work_tasks',request['task_id'])
        try:
            tasks._admit_request(c, task, active_request, side_effect=True)
        except tasks.WorkConflict:
            return {'state':'delegation_unavailable','fault':kind}
        last = latest_author(c, request)
        claims = [p for p in _provider_events(c, CLAIM, provider) if p['run_id'] == run['id']]
        old = claims[0] if claims else None
        can_reattach = (can_readopt and old==prior and old['next_run_id']==next_run_id
                        and not c.execute('SELECT 1 FROM work_runs WHERE id=?', (next_run_id,)).fetchone())
        if (source['stop_requested'] or source['outcome'] != run['outcome'] or not last or last['id'] != run['id']
                or old and not can_reattach or _lineage_stamp(c, request) != lineage_stamp
                or not progress and c.execute("SELECT 1 FROM work_run_events WHERE kind=? AND json_extract(payload_json,'$.next_run_id')=?",
                                               (CLAIM, run['id'])).fetchone()
                or unresolved):
            return {'state':'original_effect_or_attempt_unresolved','fault':kind,
                    **({'effect':unresolved} if unresolved else {})}
        if current_probe and current_probe['run_id'] != run['id']:
            if capacity.recovery_managed(root, provider):
                return _defer(c, source, actor, provider, next_run_id, kind, None)
            return {'state':'probe_in_progress'}
        if latest.get('until', 0) > now:
            if capacity.recovery_managed(root, provider):
                return _defer(c, source, actor, provider, next_run_id, kind, latest['until'])
            return {'state':'cooldown','until':latest['until'],'fault':kind}
        claim = {'state':'claimed','provider':provider,'next_run_id':next_run_id,'fault':kind,
                 'probe':latest.get('state') == 'open', 'manager':manager,
                 'evidence_stamp':lineage_stamp}
        tasks._run_event(c, source, CLAIM, actor, None, claim)
        return claim


def finish(root, actor, original, claim, *, manager_finished=True):
    if not claim['probe']:
        return
    provider = claim['provider']
    with contextlib.closing(tasks._connect(root, True)) as c:
        probe = _active_probe(c, provider)
    if not probe or probe['next_run_id'] != claim['next_run_id']:
        return
    success = _probe_result(root, probe, manager_finished=manager_finished)
    if success is None:
        return
    with tasks.transaction(root) as c:
        current = _active_probe(c, provider)
        if current and current['event_id'] == probe['event_id']:
            _finish_probe(c, actor, tasks._row(c, 'work_runs', original), probe, time.time(), success)


def finish_observed_probe(root, actor, run_id):
    """Settle the original provider probe even after its claimant disappears."""
    with contextlib.closing(tasks._connect(root, True)) as c:
        row = c.execute("""SELECT run_id,payload_json FROM work_run_events WHERE kind=?
                           AND json_extract(payload_json,'$.next_run_id')=? ORDER BY id DESC LIMIT 1""",
                        (CLAIM, run_id)).fetchone()
    if row:
        finish(root, actor, row['run_id'], json.loads(row['payload_json']), manager_finished=False)


def guard(c, root, previous, new, *, entering=False):
    source = tasks._row(c,'work_runs',previous)
    row = c.execute("""SELECT payload_json FROM work_run_events WHERE run_id=? AND kind=?
                    AND json_extract(payload_json,'$.next_run_id')=? ORDER BY id DESC LIMIT 1""",
                    (previous, CLAIM, new)).fetchone()
    if row is None or source['stop_requested'] or source['outcome'] not in ('failed', 'unknown'):
        tasks._fail('auto_resume_no_authority','Original automatic continuation is no longer authorized.',previous)
    if entering:
        request = tasks._decode(tasks._row(c,'work_requests',source['request_id']))
        latest = latest_author(c,request)
        claim = json.loads(row[0])
        if (not latest or latest['id'] != previous or not capacity.recovery_enabled(root, claim['provider'])
                or 'evidence_stamp' in claim and claim['evidence_stamp'] != _lineage_stamp(c, request)):
            tasks._fail('auto_resume_no_authority','Original DSH attempt, effects or provider admission has changed.',previous)


def guard_original_stop(root, previous, new):
    with contextlib.closing(tasks._connect(root,True)) as c:
        guard(c,root,previous,new)
