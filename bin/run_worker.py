#!/usr/bin/env python3
"""Continue one original worker request through its selected native host."""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time

import agent_runtime as runtime
from codex_host import CodexHost, HostError, safe_diagnostic
import harness_maintenance
from harness_hosts import ROUTE_ADAPTERS, installed_program, supports
import harness_sessions
import harness_tasks as tasks
import workspace_files as entry
import production_review
from production_review import program_home, _save_fixed, _review_packet, _program_runs, _program_result


def _selected_run(root, request_id, exact_run_id=None):
    with contextlib.closing(tasks._connect(root, True)) as c:
        row = c.execute('SELECT * FROM work_runs WHERE request_id=?' + (' AND id=?' if exact_run_id else '') +
                        ' ORDER BY created_at DESC,id DESC LIMIT 1',
                        (request_id, exact_run_id) if exact_run_id else (request_id,)).fetchone()
        return tasks._run_view(c, row) if row else None


SCHEMA = 'data-workspace.collaboration-worker.v1'
TERMINAL = ('succeeded', 'failed', 'stopped')


def _return_from_original_run(root, actor, run, request):
    tasks.recovery_admission(root, run['id'], actor)
    receipt = run.get('latest_receipt') or request['input_ref']
    role = 'scientific_reviewer' if request['independent'] else request['recipient_role']
    readback = runtime.Runtime(root).readresult(run_id=run['id'], config_path=run['config_ref']['path'],
        role=role, evidence_home=str(Path(receipt['path']).parent))
    delivery = readback.get('management_return') or {}
    return tasks.read_return(root, delivery['return_id']) if delivery.get('return_id') else readback


def return_result(root, actor, path=None, *, request_id=None, run_id=None, scope_ids=(),artifact_paths=(),candidate_paths=()):
    from delivery_evidence import explicit_files, selected_files

    def associate(home, previous):
        linked = explicit_files(root,artifact_paths,home=home)
        selected = selected_files(root,candidate_paths,home=home)
        if selected:
            previous = [{**ref,'id':'file:'+ref['sha256']} if ref['id'].startswith('selected:') else ref
                        for ref in previous]
        return list({a['id']:a for a in [*previous,*linked,*selected]}.values()), [*linked,*selected]

    if bool(request_id) == bool(run_id):
        raise ValueError('Select the original --request with --result, or the original --run.')
    if run_id:
        if path is not None:
            raise ValueError('Read the original native report with --run; separate work uses its own --request and --result.')
        run = tasks.read_run(root, run_id)
        returned=_return_from_original_run(root, actor, run, tasks.read_request(root, run['request_id']))
        if not scope_ids and not artifact_paths and not candidate_paths:return returned
        if 'result_ref' not in returned:raise ValueError('Original run has no complete report to index; partial reports can always use --result.')
        from harness_scope import _returns
        with contextlib.closing(tasks._connect(root,True)) as c:
            current=next((r for r in _returns(c,tasks.read_request(root,run['request_id'])['task_id']) if r['id']==returned['id']),returned)
        artifacts,linked=associate(Path(run['latest_receipt']['path']).parent,current['artifact_refs'])
        return tasks.mutate(root,'return',actor,{'id':'return-association:'+runtime.digest(tasks._json([returned['id'],list(scope_ids),linked]).encode()),
            'request_id':run['request_id'],'run_id':run_id,'native_receipt':run['latest_receipt'],
            'result_ref':returned['result_ref'],'summary':returned['summary'],
            **({'artifact_refs':artifacts} if linked else {}),
            **({'coverage':list(scope_ids)} if scope_ids else {})})
    request = tasks.read_request(root, request_id)
    if path is None:
        raise ValueError('Provide the original result file or stdin.')
    ref, _ = tasks._saved_document(root, path, home=Path(request['input_ref']['path']).parent, label='result', allow_text=True)
    contribution=tasks.contribution_payload(root,request,ref)
    linked=[]
    if artifact_paths or candidate_paths:
        from harness_scope import _returns
        with contextlib.closing(tasks._connect(root,True)) as c:
            previous=[r for r in _returns(c,request['task_id']) if r['request_id']==request_id and r['result_ref']['sha256']==ref['sha256']]
        prior=previous[-1]['artifact_refs'] if previous else contribution.get('artifact_refs',[])
        contribution['artifact_refs'],linked=associate(program_home(request,root=root)/'checks'/'associations',prior)
    else:contribution.pop('artifact_refs',None)
    ident = 'result:' + hashlib.sha256(tasks._json([request_id, tasks.actor_key(actor), ref,list(scope_ids),linked]).encode()).hexdigest()
    contribution.pop('coverage',None);contribution.pop('remaining_scope',None)
    return tasks.mutate(root, 'return', actor, {'id':ident, **contribution,
        **({'coverage':list(scope_ids)} if scope_ids else {})})


def _program_metering(root, request):
    with contextlib.closing(tasks._connect(root, True)) as c:
        task = tasks._row(c, 'work_tasks', request['task_id'])
        row = tasks._row(c, 'work_requests', request['id'])
        return tasks.finite.usage_view(c, task, row)


def _review_subject(request_id,packet):
    # Location is not scientific identity. This also recognizes original
    # frozen packets retained under their historical archive paths.
    artifacts=sorted([[a.get('id',a.get('path')),a.get('version',a.get('sha256')),a.get('sha256')]
        for a in packet['artifact_refs']],key=tasks._json)
    evidence=sorted([ref['sha256'] for ref in packet['evidence_refs']])
    identity=[request_id,packet['review_scope'],artifacts,evidence]
    if packet.get('supersedes_review_id'):
        identity.append(['supersedes_review_id',packet['supersedes_review_id']])
    if prior := packet.get('prior_review'):
        identity.append(['prior_review_id',prior['review_id'],prior['submission_ref']['sha256'],
                         prior['return_id'],prior['opinion_ref']['sha256']])
    return identity


def _program_review_request(root, worker, request, packet):
    # Only an explicit author tool invocation calls this function.
    # A long-lived author may predate the owner's explicit scope correction.
    # Inherit its current recorded association, never infer scope from prose.
    request = tasks.read_request(root, request['id'])
    packet = {**packet, 'scope_keys': sorted(request['required_scope'])}
    subject = _review_subject(request['id'],packet)
    with contextlib.closing(tasks._connect(root,True)) as c:
        originals=[tasks._decode(row) for row in c.execute(
            "SELECT * FROM work_requests WHERE parent_request_id=? AND independent=1 ORDER BY created_at,id",(request['id'],))]
    for original in originals:
        try:matches=_review_subject(request['id'],_review_packet(root,original['input_ref']))==subject
        except (ValueError,OSError,KeyError,TypeError,AttributeError):continue
        if matches:return original
    fingerprint = runtime.digest(tasks._json(subject).encode())[:32]
    ident = 'program-review:' + fingerprint
    from execution_lease import FileLock
    with FileLock(root, ['review-packet', ident]):
        try:
            return tasks.read_request(root, ident)
        except tasks.WorkConflict as exc:
            if exc.code != 'not_found':
                raise
        return _register_review_packet(root, worker, request, packet, ident)


def _register_review_packet(root, worker, request, packet, ident):
    directory=program_home(request,root=root)/'reviews'/ident
    path = production_review.scientific_path(root, directory/'packet.json', must_exist=False)
    if path.is_file():
        raw = path.read_bytes()
        ref = {'path': str(path), 'sha256': runtime.digest(raw)}
        frozen_packet = _review_packet(root, ref)
        if _review_subject(request['id'], frozen_packet) != _review_subject(request['id'], packet):
            raise ValueError('Original review registration has different frozen scientific inputs.')
        packet = frozen_packet
    else:
        from production_review import _fix_artifact
        frozen=[_fix_artifact(root,directory,artifact) for artifact in packet['artifact_refs']]
        replacements={a['path']:{'path':b['path'],'sha256':b['sha256']} for a,b in zip(packet['artifact_refs'],frozen)}
        packet={**packet,'artifact_refs':frozen,
            'evidence_refs':[{**ref,**replacements.get(ref['path'],{})} for ref in packet['evidence_refs']]}
        if prior := packet.get('prior_review'):
            from review_delta import freeze
            packet['focus'] = freeze(root,directory,packet)
            packet['evidence_refs'] += [prior['submission_ref'],prior['opinion_ref'],
                                       packet['focus']['index_ref'],packet['focus']['delta_ref']]
        else:
            from review_facts import collect
            try:
                facts = collect(root, request, packet, runtime.now())
                if facts:
                    raw = (tasks._json(facts) + '\n').encode()
                    facts_ref = _save_fixed(root, directory/('review-facts-'+runtime.digest(raw)[:16]+'.json'), raw)
                    packet['facts_ref'] = facts_ref
                    packet['evidence_refs'].append(facts_ref)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                pass  # An optional projection never withholds the exact frozen originals.
        ref = _save_fixed(root, path, (tasks._json(packet) + '\n').encode())
    return tasks.mutate(root, 'request', worker.actor, {
        'id': ident, 'task_id': request['task_id'], 'kind': 'review', 'recipient_role': 'executor',
        'input_ref': ref, 'summary': packet['review_scope'], 'plan_revision': request['plan_revision'],
        'required_scope': request['required_scope'], 'independent': True, 'parent_request_id': request['id'],
        'delivery_contract':{'purpose':packet['review_scope'],'endpoint':'evidence',
            'scope_boundary':{'in_scope':'Read the frozen exact candidates and original author purpose/question.',
                              'out_of_scope':'No data writes, adoption, submission or author Session reuse.'},
            'acceptance':{'text':'Independent scientific conclusion, basis, actionable issues and limits for the original purpose.','refs':packet['evidence_refs']},
            'authority_refs':[],'interfaces':{'text':'Return the original opinion directly to the author.','refs':[]}}})


def _program_turn(root, worker, parent, request, config, *, previous=None, technical_recovery=None):
    role = 'scientific_reviewer' if request['independent'] else request['recipient_role']
    context = {'parent_brief_ref': parent['input_ref']} if request['independent'] else {}
    if technical_recovery:
        context['technical_recovery'] = technical_recovery
        context['prior_receipt_ref'] = previous['latest_receipt']
    run_id = _program_run_id(request,config,previous['id'] if previous else None)

    input_ref = technical_recovery['ref'] if technical_recovery else request['input_ref']
    result = worker.execute(config_path=config['config_ref']['path'], role=role, task_id=request['task_id'],
        request_id=request['id'], run_id=run_id,
        input_path=input_ref['path'], input_sha256=input_ref['sha256'],
        evidence_home=str(program_home(parent, root=root)), operation='resume' if previous else 'start',
        previous_run_id=previous['id'] if previous else None,
        program_context=context if config.get('programmatic') else None)
    if config['host']['adapter']=='dsh-headless' and isinstance(result,dict) and result.get('outcome')=='failed':
        from dsh_capacity import is_pilot_provider
        if is_pilot_provider(root,config['provider'].get('dsh_provider')):
            recovery = _automatic_continuation(root,worker,parent,request,config,run_id,
                                               nested=bool((technical_recovery or {}).get('automatic')))
            if recovery:
                result['automatic_continuation'] = recovery
    return result


def _program_run_id(request,config,previous):
    return 'program:' + runtime.digest(tasks._json([request['id'],config['config_ref'],previous]).encode())[:32]


def _automatic_continuation(root, worker, parent, request, config, run_id, *, nested):
    from dsh_auto_resume import consider, finish
    from execution_lease import ExecutionBusy
    original = tasks.read_run(root,run_id)
    claim = None
    try:
        child = None if nested else _program_run_id(request,config,run_id)
        claim = consider(root,worker.actor,request,original,config,child)
        if not claim or claim['state'] != 'claimed':
            return claim
        receipt = original['latest_receipt']
        instruction = ('Continue the existing active Goal in this original Session within its remaining budget. '
                       'Read get_goal and the previous terminal receipt. Retain and use prior results and review IDs. '
                       'Check uncertain effects through their original read-only recovery entry. Do not resend an earlier '
                       'write or request a duplicate independent review. Advance only the next authorized unfinished action.\n'
                       'Previous terminal receipt: '+receipt['path']+'\n')
        ref = _save_fixed(root, Path(receipt['path']).parent/'automatic-continuation.md', instruction.encode())
        result = _program_turn(root,worker,parent,request,config,previous=original,
            technical_recovery={'ref':ref,'evidence_refs':[receipt],'automatic':True})
        return {'state':'attempted','run_id':child,'outcome':result.get('outcome'),'receipt':result.get('receipt'),
                'probe':claim['probe']}
    except (ExecutionBusy, OSError, ValueError, KeyError, TypeError, sqlite3.Error, runtime.CapabilityUnavailable) as exc:
        return {'state':'withheld_or_recover_original','reason':safe_diagnostic(exc),
                **({'run_id':claim['next_run_id']} if claim and claim['state']=='claimed' else {})}
    finally:
        if claim and claim['state']=='claimed':
            try:
                finish(root,worker.actor,run_id,claim)
            except (OSError,ValueError,sqlite3.Error):
                # The next fault observes the exact saved probe, including after restart.
                pass


def _program_read(root, worker, request, run, *, stop=None, config_path=None):
    if not run:
        return {'request_id': request['id'], 'state': 'not_started'}
    role = 'scientific_reviewer' if request['independent'] else request['recipient_role']
    home = Path((run['latest_receipt'] or request['input_ref'])['path']).parent
    options = {'run_id': run['id'], 'role': role, 'config_path': config_path, 'evidence_home': str(home)}
    if stop:
        return worker.stop(**options, reason=stop)
    try:
        return worker.readresult(**options)
    except runtime.CapabilityUnavailable as exc:
        return {'run_id': run['id'], 'outcome': run['outcome'], 'receipt': run['latest_receipt'],
                'replay_allowed': False, 'reason': safe_diagnostic(exc)}


def _program_opinion(root, worker, parent, run):
    if run['outcome']=='succeeded' and not run.get('return_ids'):
        # Native terminal publication precedes automatic return indexing. A
        # concurrent author can arrive in that gap; use the retained-read path
        # to repair the same deterministic return, never launch another review.
        try:
            worker.readresult(run_id=run['id'],config_path=None,role='scientific_reviewer',
                evidence_home=str(Path(run['latest_receipt']['path']).parent))
            run=tasks.read_run(root,run['id'])
        except (OSError,sqlite3.Error):
            pass  # The exact native opinion remains readable if indexing fails.
    result = _program_result(root, run)
    report = result.get('result')
    if not isinstance(report, str):
        return None
    review = tasks.read_request(root, run['request_id'])
    try:
        packet = _review_packet(root, review['input_ref'])
    except ValueError:
        packet = {'brief_ref': review['input_ref']}
    if not isinstance(packet, dict):
        packet = {'brief_ref': review['input_ref']}
    returned = tasks.read_return(root, run['return_ids'][0]) if run['return_ids'] else None
    source = returned['result_ref'] if returned else run['latest_receipt']
    return {'return_id': returned['id'] if returned else None, 'ref': source,
            'submission_ref': review['input_ref'], 'opinion': {'text': report, 'submission': packet}}


def _drive_program(root, worker, request, config):
    run = _selected_run(root, request['id'])
    # A directly saved report remains usable even when an earlier host launch
    # failed before opening a Session. Observation never replaces that report.
    with contextlib.closing(tasks._connect(root, True)) as c:
        returned = c.execute('SELECT id,run_id FROM work_returns WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
                             (request['id'],)).fetchone()
    if returned and not returned['run_id']:
        return {'request_id': request['id'], 'state': 'returned',
                'management_return': worker._registered_delivery(returned['id']), 'original_run': run}
    if run:
        return {'request_id': request['id'], **_program_read(root, worker, request, run)}
    from harness_package import predecessor
    return _program_turn(root, worker, request, request, config, previous=predecessor(root, request))


def _continue_session(root, worker, request, config, path=None, *, exact_run_id=None, resume_stopped=False):
    """Continue the exact terminal Session from an ordinary owner instruction file."""
    ref = harness_sessions.reference(root, path) if path else request['input_ref']
    if not Path(ref['path']).read_text(encoding='utf-8').strip():
        raise ValueError('The continuation instruction is empty.')
    runs = _program_runs(root, request)
    original = next((r for r in reversed(runs) if r['id'] == exact_run_id), None) if exact_run_id else _selected_run(root, request['id'])
    if not original:
        raise ValueError('Select the exact original run to continue; no replacement Session is created.')
    if original['outcome'] in ('running', 'intent', 'unknown'):
        _program_read(root, worker, tasks.read_request(root, original['request_id']), original)
        original = tasks.read_run(root, original['id'])
    # An explicit instruction can continue a capable existing conversation
    # after a lost transport. It does not replay or settle unknown data effects.
    unknown_session = bool(original and original['outcome'] == 'unknown'
                           and supports(original['target_host'], 'interrupted_resume'))
    if original and path:
        with contextlib.closing(tasks._connect(root, True)) as c:
            children = {row[0] for row in c.execute("SELECT run_id FROM work_run_events WHERE kind='intent' AND json_extract(payload_json,'$.previous_run_id')=?", (original['id'],))}
        if any(r['id'] in children and any(item['sha256'] == ref['sha256'] for item in r['input_refs']) and not worker.confirmed_start_rejection(r) for r in runs):
            return
    if not original or original['outcome'] not in TERMINAL and not unknown_session:
        raise ValueError('Observe the original running or unknown attempt before continuing it.')
    selected = tasks.read_request(root, original['request_id'])
    latest = _selected_run(root, selected['id'])
    if latest['id'] != original['id']:
        raise ValueError('Continue the latest original Session attempt, not an earlier turn.')
    tasks.recovery_admission(root, original['id'], worker.actor)
    if supports(original['target_host'], 'tracked_processes'):
        from execution_lease import observe_run
        execution = observe_run(root, original)
        if execution['state'] != 'exited':
            raise ValueError('Original execution ' + execution['state'] + ': ' + execution.get('reason', '') + '; observe ' + original['id'])
        if original.get('latest_receipt'):
            receipt = runtime.read_receipt(root, original['latest_receipt'])
            result = receipt.get('result') or {}
            current_native = execution.get('native_observation') or {}
            goal = ((current_native['native_goal'] or {}) if 'native_goal' in current_native else
                    (result.get('native_session_observation') or {}).get('native_goal') or
                    (result.get('native_goal_observation') or {}).get('goal') or {})
            phase = goal.get('phase', goal.get('status'))
            if phase == 'paused' and not resume_stopped:
                raise ValueError('Original Goal is paused; explicit owner recovery and --resume-stopped required.')
            if phase in ('complete', 'blocked') and not path:
                raise ValueError('Original Goal is ' + phase + '; an explicit owner instruction is required; its phase is retained.')
    stopped = original['outcome'] == 'stopped' or original['stop_requested']
    if stopped:
        if not resume_stopped:
            raise ValueError('The original stop is still in force; explicit resume and native terminal confirmation are required.')
        if original['outcome'] == 'stopped':
            receipt = runtime.read_receipt(root, original['latest_receipt'])
            if not (receipt.get('result') or {}).get('stop_confirmed'):
                raise ValueError('Observe the confirmed native stop before continuing its Session.')
    if unknown_session:
        with contextlib.closing(tasks._connect(root, True)) as c:
            tasks.session_origin(c, tasks._row(c, 'work_runs', original['id']))
    elif not original['native_identity'] and not worker.confirmed_start_rejection(original,
            allow_stopped=bool(stopped and resume_stopped)):
        raise ValueError('The original native identity is unknown; do not create a replacement.')
    if resume_stopped and not path:
        raise ValueError('Explicit stop recovery needs the original owner instruction; ordinary --continue does not resume stops.')
    if resume_stopped:
        tasks.resume_delegation(root, worker.actor, selected['id'], ref)
    _program_turn(root, worker, request, selected, config, previous=original,
        technical_recovery={'ref': ref, 'evidence_refs': [original['latest_receipt']] if original['latest_receipt'] else [],
                            **({'resume_stopped_run':original['id']} if stopped else {})})


def programmatic(root, request_id, *, config_path=None, status=False, read_result=False, stop=None,
                 exact_run_id=None, route=None, runtime_factory=None, instructions_path=None, resume_stopped=False,
                 continue_original=False):
    """Execute or observe one original native invocation; Goal owns its work."""
    root = Path(root).resolve(strict=True)
    if instructions_path and continue_original:
        raise ValueError('--continue and --instructions are mutually exclusive.')
    if (instructions_path or resume_stopped or continue_original) and (status or read_result or stop):
        raise ValueError('Continuation starts a native turn; use a separate observation or stop action.')
    if resume_stopped and not instructions_path:
        raise ValueError('Explicit stop recovery needs its original owner instruction.')
    request = tasks.read_request(root, request_id)
    if request['kind'] not in ('plan', 'review', 'finding'):
        raise ValueError('Select an explicit work or review assignment.')
    runs = _program_runs(root, request)
    if status:
        return {'request_id': request_id, 'transport': 'programmatic', 'metering': _program_metering(root, request), 'runs': runs}
    worker = (runtime_factory or runtime.Runtime)(root)
    if read_result or stop:
        run = next((r for r in reversed(runs) if not exact_run_id or r['id'] == exact_run_id), None)
        if exact_run_id and not run:
            raise ValueError('Recover an exact original package or dependent review run.')
        selected = tasks.read_request(root, run['request_id']) if run else request
        return _program_read(root, worker, selected, run, stop=stop, config_path=config_path)
    if continue_original and not exact_run_id:
        raise ValueError('--continue requires the original --run-id.')
    if runs and not instructions_path and not continue_original:
        return _drive_program(root, worker, request, None)
    if continue_original:
        original = next((r for r in runs if r['id'] == exact_run_id), None)
        if original and original['target_host'] == 'deepseek_harness' and original['outcome'] == 'intent':
            from dsh_capacity_handoff import recover_abandoned
            recovered = recover_abandoned(root, worker, original, config_path=config_path, route=route)
            if recovered is not None:
                return {'request_id': request_id, **recovered}
    if not config_path:
        installed = installed_program(root)
        if not installed:
            raise runtime.CapabilityUnavailable(
                'No qualified programmatic worker is selected in .control/runtime.json (programmatic_worker).')
        config_path = installed['path']
    config = runtime.load_config(root, config_path, 'scientific_reviewer' if request['independent'] else request['recipient_role'])
    if not config['programmatic']:
        raise ValueError('Select an explicitly configured programmatic runtime.')
    adapter = config['host']['adapter']
    if route and adapter != ROUTE_ADAPTERS.get(route):
        raise ValueError('The selected program route does not match the original configured host.')
    # The atomic SQLite run-intent owns dispatch exclusion for the author.
    # An outer process may still carry independent reviews after its author ends.
    home = production_review.scientific_path(root, program_home(request, root=root), must_exist=False)
    (home / 'work').mkdir(parents=True, exist_ok=True)
    try:
        if instructions_path or continue_original:
            _continue_session(root, worker, request, config, instructions_path,
                              exact_run_id=exact_run_id, resume_stopped=resume_stopped)
        return _drive_program(root, worker, request, config)
    except (runtime.CapabilityUnavailable, production_review.ReviewCapabilityUnavailable) as exc:
        latest = next(iter(reversed(_program_runs(root, request))), None)
        if latest:
            return {'request_id': request_id, 'outcome': latest['outcome'], 'run_id': latest['id'], 'diagnostic': safe_diagnostic(exc)}
        return {'request_id': request_id, 'state': 'host_unavailable', 'phase': 'host',
                'reason': safe_diagnostic(exc)}
    except tasks.WorkConflict as exc:
        if exc.code != 'dependency_wait':
            raise
        return {'request_id': request_id, 'state': 'waiting_for_dependency',
                'metering': _program_metering(root, request), 'reason': safe_diagnostic(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=entry.ROOT)
    parser.add_argument('--request', required=True)
    parser.add_argument('--route', choices=tuple(ROUTE_ADAPTERS))
    parser.add_argument('--config')
    parser.add_argument('--run-id')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--status', action='store_true')
    actions.add_argument('--read-result', action='store_true')
    actions.add_argument('--stop', metavar='REASON')
    actions.add_argument('--instructions', help='Original owner instruction to continue the same terminal Session.')
    actions.add_argument('--continue', dest='continue_original', action='store_true', help='Technical continuation of --run-id in its original Session, brief and Goal.')
    parser.add_argument('--resume-stopped', action='store_true', help='Explicitly continue an ended assignment or stopped Session with --instructions')
    args = parser.parse_args(argv)
    try:
        if args.stop is not None and not args.stop.strip():
            raise ValueError('Provide the reason for stopping the original run.')
        result = programmatic(args.root, args.request, config_path=args.config, status=args.status,
            read_result=args.read_result, stop=args.stop, exact_run_id=args.run_id, route=args.route,
            instructions_path=args.instructions, resume_stopped=args.resume_stopped, continue_original=args.continue_original)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, entry.ProductionError) as exc:
        print(json.dumps({'error': safe_diagnostic(exc), 'request_id': args.request}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
