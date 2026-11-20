"""One technical continuation of an exited, progressing original DSH Goal."""
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
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
    return [{**json.loads(row['payload_json']), 'event_id':row['id'], 'run_id':row['run_id']} for row in rows]


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
    from delivery_evidence import _json
    unknown, resolved = set(), set()
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
                    return True
                if not isinstance(record,dict):
                    return True
                if record.get('run_id')!=run['id'] or record.get('request_id')!=ident or record.get('source')!='trusted_public_cli':
                    return True
                action = record.get('operation','')
                if record.get('dispatch') == 'not_started' or '--dry-run' in record.get('argv',[]):
                    continue
                request_value = _json(root,record.get('input_ref')) or {}
                if not isinstance(request_value,dict):
                    request_value={}
                group = action == 'dataset model result-group'
                mutation = (action.endswith((' create',' save',' delete',' submit',' resubmit',' publish'))
                            or group and request_value.get('action')!='inspect')
                if not mutation:
                    continue
                # An exit of zero can be a structured continuation/unknown result.
                reply = _json(root, record.get('stdout_ref')) or {}
                if not isinstance(reply,dict):
                    reply={}
                data = reply.get('data') if isinstance(reply.get('data'),dict) else {}
                context = reply.get('context') if isinstance(reply.get('context'),dict) else {}
                effect_id = data.get('operationId') if group else (request_value.get('requestId') or context.get('requestId'))
                write_scope = (record.get('write_authority') or {}).get('scope') or {}
                bound = write_scope.get('object'),write_scope.get('project'),write_scope.get('account')
                effect = (action,effect_id,*bound) if isinstance(effect_id,str) and all(bound) else (action,str(path))
                known = record.get('capture_complete') and record.get('exit_code')==0 and (
                    reply.get('outcome') in ('dataset_written','dataset_created','dataset_saved','dataset_deleted',
                                             'review_submitted','review_resubmitted','dataset_published')
                    or group and reply.get('outcome') in ('verified','observed','draft_validation_failed'))
                (resolved if known else unknown).add(effect)
    return bool(unknown-resolved)


def _ready(root, run, result, log):
    live = result.get('native_goal_observation') or {}
    goal = log.get('native_goal') or {}
    if live.get('goal') and any(live['goal'].get(k) != goal.get(k) for k in ('id','revision','phase')):
        return False
    activity = result.get('turn_activity') or {}
    used, limit = goal.get('roundsStarted'), goal.get('maxGoalRounds')
    if (run['outcome'] != 'failed' or run['stop_requested'] or not goal.get('id') or goal.get('phase') != 'active'
            or type(used) is not int or type(limit) is not int or not 0 <= used < limit
            or log.get('unreturned_tool_calls') != [] or activity.get('unreturned_tools') != 0
            or not activity.get('ending_turn_verified_progress') or result.get('input_issues')):
        return False
    return execution.observe_run(root, run)['state'] == 'exited'


def _finish_probe(c, root, actor, source, probe, now, *, manager_finished=False):
    row = c.execute('SELECT * FROM work_runs WHERE id=?',(probe['next_run_id'],)).fetchone()
    if row is None:
        if not manager_finished and execution.matches(probe['manager']) is not False:
            return False
        success = False
    else:
        child = tasks._run_view(c,row)
        if execution.observe_run(root,child)['state'] != 'exited':
            return False
        if child['outcome'] not in ('succeeded','failed','stopped','unknown'):
            return False
        intent = c.execute("SELECT payload_json FROM work_run_events WHERE run_id=? AND kind='intent' ORDER BY id LIMIT 1",
                           (child['id'],)).fetchone()
        probe_started = intent and json.loads(intent[0]).get('dsh_auto_continue_from')==probe['run_id']
        result = read_receipt(root,child['latest_receipt']).get('result') or {} if child['latest_receipt'] else {}
        log = _observed(child,result,probe['provider'])
        success = (probe_started and child['outcome']=='succeeded' and log.get('terminal')=='completed'
                   and log.get('unreturned_tool_calls')==[] and not _unresolved_operations(c,root,
                       tasks._decode(tasks._row(c,'work_requests',child['request_id']))))
    later_fault = _provider_events(c,FAULT,probe['provider'],after=probe['event_id'])
    if success and not later_fault:
        state = {'state':'reset','until':0}
    else:
        state = {'state':'open','until':max([now,*[e['observed_at'] for e in later_fault]])+WINDOW}
    tasks._run_event(c,source,FUSE,actor,None,{'provider':probe['provider'], **state, 'settled_probe':probe['next_run_id']})
    return True


def consider(root, actor, request, run, config, next_run_id):
    provider = config['provider'].get('dsh_provider')
    if not capacity.is_pilot_provider(root,provider) or not run.get('latest_receipt'):
        return None
    result = read_receipt(root,run['latest_receipt']).get('result') or {}
    log = _observed(run,result,provider)
    if not log or log.get('terminal') != 'error' or result.get('native_status') != 'failed':
        return None
    kind = classify(log.get('native_error'))
    if kind is None:
        return None
    now = time.time()
    finished = ((result.get('turn_activity') or {}).get('native_terminal_at')
                or (result.get('metering') or {}).get('finished_at'))
    stamp = datetime.fromisoformat(finished).timestamp() if finished else now
    native = run['native_identity']
    native_turn = native['id'] + ':' + native['turn_id']
    native_request = _sse_request(root,run,kind)
    # Read the evidence before holding the admission lock. Recheck mutable
    # decisions and attempt identity inside the original transaction below.
    ready = not request['independent'] and request['recipient_role']=='executor' and _ready(root,run,result,log)
    with capacity._lock(root), tasks.transaction(root) as c:
        if not capacity.is_pilot_provider(root,provider):
            return None
        source = tasks._row(c,'work_runs',run['id'])
        fuses = _provider_events(c,FUSE,provider)
        latest = fuses[0] if fuses else {}
        faults = _provider_events(c,FAULT,provider)
        if not any(f['run_id']==run['id'] or f['native_turn']==native_turn or native_request and f.get('native_request')==native_request for f in faults):
            tasks._run_event(c,source,FAULT,actor,None,{'provider':provider, 'kind':kind,
                'native_turn':native_turn, 'native_request':native_request, 'observed_at':stamp,
                'runtime_role':'scientific_reviewer' if request['independent'] else request['recipient_role']})
            cutoff = now-WINDOW
            recent = [f for f in faults if f['event_id']> (latest['event_id'] if latest.get('state')=='reset' else 0)
                      and cutoff<=f['observed_at']<=now and cutoff<=stamp<=now]
            already_open = latest.get('state')=='open'
            if (already_open and latest['until']>now) or (not already_open and recent):
                until = max(latest.get('until',0), max([stamp,*[f['observed_at'] for f in recent]])+WINDOW)
                tasks._run_event(c,source,FUSE,actor,None,{'provider':provider,'state':'open','until':until})
                latest = {'state':'open','until':until}
        settled = {e.get('settled_probe') for e in fuses}
        probes = [p for p in _provider_events(c,CLAIM,provider) if p['probe'] and p['next_run_id'] not in settled]
        if probes:
            if not _finish_probe(c,root,actor,source,probes[0],now):
                return {'state':'probe_in_progress'}
            latest = _provider_events(c,FUSE,provider)[0]
        if not ready or not next_run_id:
            return {'state':'ineligible','fault':kind}
        active_request = tasks._row(c,'work_requests',request['id'])
        task = tasks._row(c,'work_tasks',request['task_id'])
        try:
            tasks._admit_request(c,task,active_request,side_effect=True)
        except tasks.WorkConflict:
            return {'state':'delegation_unavailable','fault':kind}
        last = latest_author(c,request)
        if (source['stop_requested'] or source['outcome'] != 'failed' or not last or last['id'] != run['id']
                or c.execute('SELECT 1 FROM work_run_events WHERE run_id=? AND kind=?',(run['id'],CLAIM)).fetchone()
                or c.execute('SELECT 1 FROM work_run_events WHERE kind=? AND json_extract(payload_json,\'$.next_run_id\')=?',
                             (CLAIM,run['id'])).fetchone()
                or _unresolved_operations(c,root,request)):
            return {'state':'original_effect_or_attempt_unresolved','fault':kind}
        if latest.get('until',0)>now:
            return {'state':'cooldown','until':latest['until'],'fault':kind}
        claim = {'state':'claimed','provider':provider,'next_run_id':next_run_id,'fault':kind,
                 'probe': latest.get('state')=='open','manager':execution.process_identity(os.getpid())}
        tasks._run_event(c,source,CLAIM,actor,None,claim)
        return claim


def finish(root, actor, original, claim):
    if not claim['probe']:
        return
    with capacity._lock(root), tasks.transaction(root) as c:
        provider = claim['provider']
        if any(e.get('settled_probe')==claim['next_run_id'] for e in _provider_events(c,FUSE,provider)):
            return
        probe = next(e for e in _provider_events(c,CLAIM,provider) if e['next_run_id']==claim['next_run_id'])
        _finish_probe(c,root,actor,tasks._row(c,'work_runs',original),probe,time.time(),manager_finished=True)


def guard(c, root, previous, new, *, entering=False):
    source = tasks._row(c,'work_runs',previous)
    row = c.execute('SELECT payload_json FROM work_run_events WHERE run_id=? AND kind=? '
                    'AND json_extract(payload_json,\'$.next_run_id\')=?',(previous,CLAIM,new)).fetchone()
    if row is None or source['stop_requested'] or source['outcome']!='failed':
        tasks._fail('auto_resume_no_authority','Original automatic continuation is no longer authorized.',previous)
    if entering:
        request = tasks._decode(tasks._row(c,'work_requests',source['request_id']))
        latest = latest_author(c,request)
        if latest['id'] != previous or not capacity.is_pilot_provider(root,json.loads(row[0])['provider']):
            tasks._fail('auto_resume_no_authority','Original DSH attempt or provider admission has changed.',previous)


def guard_original_stop(root, previous, new):
    with contextlib.closing(tasks._connect(root,True)) as c:
        guard(c,root,previous,new)
