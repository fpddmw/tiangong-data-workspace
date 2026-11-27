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
from task_ledger import maintenance as harness_maintenance
from harness_hosts import ROUTE_ADAPTERS, installed_program, supports
from task_ledger import sessions as harness_sessions
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
    if not delivery.get('return_id'):
        return readback
    returned=tasks.read_return(root,delivery['return_id'])
    if effects:=readback.get('delivery_effects'):
        returned['delivery_effects']=effects
    return returned


def return_result(root, actor, path=None, *, request_id=None, run_id=None, scope_ids=(),artifact_paths=(),candidate_paths=()):
    from delivery_evidence import explicit_files, read_handoff_effects, selected_files

    def projected(request, returned, run=None):
        effects=read_handoff_effects(root,request,run=run,return_id=returned['id'])
        return {**returned,**({'delivery_effects':effects} if effects else {})}

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
        request=tasks.read_request(root,run['request_id'])
        returned=_return_from_original_run(root, actor, run, request)
        if not scope_ids and not artifact_paths and not candidate_paths:return returned
        if 'result_ref' not in returned:raise ValueError('Original run has no complete report to index; partial reports can always use --result.')
        from harness_scope import _returns
        with contextlib.closing(tasks._connect(root,True)) as c:
            current=next((r for r in _returns(c,tasks.read_request(root,run['request_id'])['task_id']) if r['id']==returned['id']),returned)
        artifacts,linked=associate(Path(run['latest_receipt']['path']).parent,current['artifact_refs'])
        registered=tasks.mutate(root,'return',actor,{'id':'return-association:'+runtime.digest(tasks._json([returned['id'],list(scope_ids),linked]).encode()),
            'request_id':run['request_id'],'run_id':run_id,'native_receipt':run['latest_receipt'],
            'result_ref':returned['result_ref'],'summary':returned['summary'],
            **({'artifact_refs':artifacts} if linked else {}),
            **({'coverage':list(scope_ids)} if scope_ids else {})})
        return projected(request,registered,run)
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
    registered=tasks.mutate(root, 'return', actor, {'id':ident, **contribution,
        **({'coverage':list(scope_ids)} if scope_ids else {})})
    return projected(request,registered)


def _program_metering(root, request):
    with contextlib.closing(tasks._connect(root, True)) as c:
        task = tasks._row(c, 'work_tasks', request['task_id'])
        row = tasks._row(c, 'work_requests', request['id'])
        return tasks.finite.usage_view(c, task, row)


def _review_subject(request_id,packet):
    # Location is not scientific identity. This also recognizes original
    # frozen packets retained under their historical archive paths.
    if packet.get('request_identity'):
        return packet['request_identity']
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
        from harness_scope import contract, contract_refs
        from scientific_review.delta import prior_candidates
        with contextlib.closing(tasks._connect(root,True)) as connection:
            agreement = contract(connection, 'request', request['id'])
            body = (agreement or {}).get('body', {})
            candidate_consumers = contract_refs(body)
        purpose = body.get('purpose') or request['input_ref']['sha256']
        packet = {**packet, 'request_identity': _review_subject(request['id'],packet),
                  'scientific_purpose': purpose}
        if not packet.get('prior_review'):
            prior, choices = prior_candidates(root,request,packet)
            if prior:
                packet['prior_review'] = prior
            elif choices:
                packet['prior_review_candidates'] = choices
        frozen=[_fix_artifact(root,directory,artifact) for artifact in packet['artifact_refs']]
        replacements={a['path']:{'path':b['path'],'sha256':b['sha256']} for a,b in zip(packet['artifact_refs'],frozen)}
        packet={**packet,'artifact_refs':frozen,
            'evidence_refs':[{**ref,**replacements.get(ref['path'],{})} for ref in packet['evidence_refs']]}
        if prior := packet.get('prior_review'):
            from scientific_review.delta import freeze
            candidates = [ref for ref in candidate_consumers if Path(ref['path']).suffix in ('.json','.jsonl')]
            packet['consumer_refs'] = [_fix_artifact(root,directory,ref) for ref in candidates
                                       if not tasks.check_inputs(root,[ref])]
            packet['focus'] = freeze(root,directory,packet)
            packet['evidence_refs'] += [prior['submission_ref'],prior['opinion_ref'],
                                       packet['focus']['index_ref'],packet['focus']['delta_ref'],*packet['consumer_refs']]
        elif candidates := packet.get('prior_review_candidates'):
            packet['evidence_refs'] += [ref for candidate in candidates for ref in (candidate['submission_ref'],candidate['opinion_ref'])]
        else:
            from scientific_review.facts import collect
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
        'input_ref': ref, 'summary': packet['review_scope'][:240], 'plan_revision': request['plan_revision'],
        'required_scope': request['required_scope'], 'independent': True, 'parent_request_id': request['id'],
        'delivery_contract':{'purpose':packet['review_scope'],'endpoint':'evidence',
            'scope_boundary':{'in_scope':'Read the frozen exact candidates and original author purpose/question.',
                              'out_of_scope':'No data writes, adoption, submission or author Session reuse.'},
            'acceptance':{'text':'Independent scientific conclusion, basis, actionable issues and limits for the original purpose.','refs':packet['evidence_refs']},
            'authority_refs':[],'interfaces':{'text':'Return the original opinion directly to the author.','refs':[]}}})


def _program_turn(root, worker, parent, request, config, *, previous=None, technical_recovery=None, writer_replacement=None):
    role = 'scientific_reviewer' if request['independent'] else request['recipient_role']
    context = {'parent_brief_ref': parent['input_ref']} if request['independent'] else {}
    if technical_recovery:
        context['technical_recovery'] = technical_recovery
        context['prior_receipt_ref'] = previous['latest_receipt']
    if writer_replacement:
        context['writer_replacement']=writer_replacement
    run_id = _program_run_id(request,config,previous['id'] if previous else None)

    input_ref = technical_recovery['ref'] if technical_recovery else request['input_ref']
    result = worker.execute(config_path=config['config_ref']['path'], role=role, task_id=request['task_id'],
        request_id=request['id'], run_id=run_id,
        input_path=input_ref['path'], input_sha256=input_ref['sha256'],
        evidence_home=str(program_home(parent, root=root)), operation='resume' if previous else 'start',
        previous_run_id=previous['id'] if previous else None,
        program_context=context if config.get('programmatic') else None)
    return _after_program_turn(root, worker, parent, request, config, run_id, result)


def _after_program_turn(root, worker, parent, request, config, run_id, result):
    if config['host']['adapter'] == 'dsh-headless' and isinstance(result, dict):
        from dsh_capacity import recovery_enabled, recovery_managed
        provider = config['provider'].get('dsh_provider')
        recovery = None
        if result.get('outcome') in ('failed', 'unknown') and recovery_enabled(root, provider):
            if (result['outcome'] == 'unknown' and result.get('native_identity')
                    and (result.get('transport_diagnostic') or {}).get('source') != 'original_model_watchdog'):
                original = tasks.read_run(root, run_id)
                observed = _program_read(root, worker, request, original)
                if observed.get('outcome') in TERMINAL:
                    result = observed
            if result.get('outcome') in ('failed', 'unknown'):
                recovery = _automatic_continuation(root,worker,parent,request,config,run_id,
                                                   delivery_effects=result.get('delivery_effects'))
                if recovery:
                    result['automatic_continuation'] = recovery
        if recovery_managed(root, provider):
            from dsh_recovery_host import record_result
            record_result(root, worker.actor, tasks.read_run(root, run_id), recovery)
    return result


def _program_run_id(request,config,previous):
    return 'program:' + runtime.digest(tasks._json([request['id'],config['config_ref'],previous]).encode())[:32]


def _automatic_continuation(root, worker, parent, request, config, run_id, *, recover_claim=False, delivery_effects=None):
    from dsh_auto_resume import consider, finish
    from execution_lease import ExecutionBusy, FileLock
    original = tasks.read_run(root,run_id)
    claim = None
    try:
        with FileLock(root, ['dsh-recovery', run_id]):
            child = _program_run_id(request,config,run_id)
            claim = consider(root,worker.actor,request,original,config,child,recover_claim=recover_claim,
                             delivery_effects=delivery_effects)
            if not claim or claim['state'] != 'claimed':
                return claim
            receipt = original['latest_receipt']
            evidence_refs=[receipt]
            instruction = ('Continue the original independent review of the same frozen input. '
                           'Complete the opinion for the existing review request.' if request['independent'] else
                           'Continue the original active Goal within its remaining budget. '
                           'Use prior results and review IDs. Advance only the next authorized unfinished action.')
            instruction += '\nOriginal receipt: ' + receipt['path'] + '\n'
            if not request['independent']:
                from delivery_evidence import read_handoff_effects
                effects=delivery_effects or read_handoff_effects(root,request,run=original)
                if effects and effects.get('state')!='original_effect_evidence_unavailable':
                    raw=(tasks._json(effects)+'\n').encode()
                    proof=_save_fixed(root,Path(receipt['path']).parent/('delivery-effects-'+runtime.digest(raw)+'.json'),raw)
                    evidence_refs.append(proof)
                    instruction += ('\nRead the exact existing write/readback evidence and candidate-only members before choosing the next action: '
                                    +proof['path']+'\nUse the original attempt for uncertain effects. Preserve the author’s scientific part of the delivery.\n')
            instruction_raw=instruction.encode()
            ref = _save_fixed(root,Path(receipt['path']).parent/('automatic-continuation-'+runtime.digest(instruction_raw)+'.md'),instruction_raw)
            result = _program_turn(root,worker,parent,request,config,previous=original,
                technical_recovery={'ref':ref,'evidence_refs':evidence_refs,'automatic':True,'probe':claim['probe']})
            return {'state':'attempted','run_id':child,'outcome':result.get('outcome'),'receipt':result.get('receipt'),
                    'probe':claim['probe']}
    except ExecutionBusy:
        return {'state':'already_processing','run_id':run_id}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, runtime.CapabilityUnavailable,
            tasks.WorkConflict) as exc:
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
        result = worker.readresult(**options)
        if run.get('target_host') == 'deepseek_harness' and result.get('outcome') in ('failed', 'unknown'):
            from dsh_auto_resume import recovery_view
            if recovery := recovery_view(tasks.read_run(root, run['id'])):
                result['automatic_continuation'] = recovery
        return result
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
    review = tasks.read_request(root, run['request_id'])
    from scientific_review.rounds import opinion_view
    with contextlib.closing(tasks._connect(root,True)) as connection:
        return opinion_view(connection, root, review, run_id=run['id'],
                            native_text=result.get('result'), source=run['latest_receipt'])


def _direct_return_id(root, request_id):
    with contextlib.closing(tasks._connect(root, True)) as c:
        row = c.execute('''SELECT id FROM work_returns WHERE request_id=? AND run_id IS NULL
                        ORDER BY created_at DESC,id DESC LIMIT 1''', (request_id,)).fetchone()
    return row['id'] if row else None


def _program_result_view(root, worker, request, run, direct_id, *, config_path=None):
    result = {'request_id': request['id']}
    direct = worker._registered_delivery(direct_id) if direct_id else None
    if run:
        result.update(_program_read(root, worker, request, run, config_path=config_path))
        if direct:
            result['request_delivery'] = direct
    elif direct:
        result.update(state='returned', management_return=direct)
        if request['independent']:
            from scientific_review.rounds import opinion_view
            with contextlib.closing(tasks._connect(root,True)) as connection:
                result['original_opinion'] = opinion_view(connection, root, request, return_id=direct_id)
    else:
        result['state'] = 'not_started'
    return result


def _drive_program(root, worker, request, config):
    run = _selected_run(root, request['id'])
    direct_id = _direct_return_id(root, request['id'])
    if run or direct_id:
        return _program_result_view(root, worker, request, run, direct_id)
    from harness_package import missing_devin_writer, predecessor
    previous=predecessor(root,request)
    replacement=None
    if previous and previous['target_host']=='devin_cli':
        with contextlib.closing(tasks._connect(root,True)) as c:
            replacement=missing_devin_writer(c,root,request,previous['request_id'])
    return _program_turn(root,worker,request,request,config,previous=None if replacement else previous,
                         writer_replacement=replacement)


def _continue_session(root, worker, request, config, path=None, *, exact_run_id=None, resume_stopped=False,
                      goal_max_rounds=None, basis=None):
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
    selected = tasks.read_request(root, original['request_id'])
    if original and path:
        with contextlib.closing(tasks._connect(root, True)) as c:
            children = {row[0] for row in c.execute("SELECT run_id FROM work_run_events WHERE kind='intent' AND json_extract(payload_json,'$.previous_run_id')=?", (original['id'],))}
        duplicate = next((r for r in reversed(runs) if r['id'] in children
                          and any(item['sha256'] == ref['sha256'] for item in r['input_refs'])
                          and not worker.confirmed_start_rejection(r)), None)
        if duplicate:
            if goal_max_rounds is not None:
                from harness_package import launch_spec
                budget=launch_spec(root,duplicate).get('goal_budget') or {}
                if budget.get('max_rounds')!=goal_max_rounds:
                    raise ValueError('This original instruction already has a different continuation. Read its exact result.')
            return selected, {'run_id': duplicate['id']}
    if not original or original['outcome'] not in TERMINAL and not unknown_session:
        raise ValueError('Observe the original running or unknown attempt before continuing it.')
    basis_ref=None
    if path:
        from harness_scope import continuation
        with contextlib.closing(tasks._connect(root,True)) as c:
            original_task=tasks._row(c,'work_tasks',selected['task_id'])
            tasks._admit_request(c,original_task,tasks._row(c,'work_requests',selected['id']))
            resolution=continuation(c,root,selected,basis)
        if resolution and not basis:
            return selected,resolution
        basis_ref=resolution
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
    goal_budget=None
    if goal_max_rounds is not None:
        from dsh_headless import query_session
        from harness_scope import _owner
        with contextlib.closing(tasks._connect(root,True)) as c:
            _owner(c,tasks._row(c,'work_tasks',selected['task_id']),worker.actor)
        native=original['native_identity'] or {}
        if (config['host']['adapter']!='dsh-headless' or selected['independent'] or selected['recipient_role']!='executor'
                or native.get('host')!='deepseek_harness' or not native.get('turn_id')):
            raise ValueError('Apply the new total to the exact original DSH author Session.')
        persisted=query_session(config['host'],native,root)
        if (persisted.get('session_id')!=native['id'] or str(persisted.get('latest_turn'))!=native['turn_id']
                or not isinstance(persisted.get('native_goal'),dict)):
            raise ValueError('Read the exact current Goal and native turn before applying its owner-approved budget.')
        goal=persisted['native_goal']
        ceiling=goal.get('maxGoalRounds')
        if (not goal.get('id') or type(goal.get('revision')) is not int or goal.get('phase')!='blocked'
                or (goal.get('blockedReason') or {}).get('code')!='round-limit'
                or type(ceiling) is not int or goal.get('roundsStarted')!=ceiling or goal_max_rounds<=ceiling):
            raise ValueError('Apply an increased total only to the exact exhausted author Goal with its retained native revision.')
        goal_budget={'original_run_id':original['id'],'session_id':native['id'],'id':goal['id'],
                     'revision':goal['revision'],'previous_max':ceiling,'max_rounds':goal_max_rounds}
    attempt = _program_turn(root, worker, request, selected, config, previous=original,
        technical_recovery={'ref': ref, 'evidence_refs': [item for item in (original['latest_receipt'],basis_ref) if item],
                            **({'ordinary_continuation':{'basis':basis}} if path else {}),
                            **({'resume_stopped_run':original['id']} if stopped else {}),
                            **({'goal_budget':goal_budget} if goal_budget else {})})
    return selected, attempt


def programmatic(root, request_id, *, config_path=None, status=False, read_result=False, stop=None,
                 exact_run_id=None, route=None, runtime_factory=None, instructions_path=None, resume_stopped=False,
                 continue_original=False, goal_max_rounds=None, basis=None):
    """Execute or observe one original native invocation; Goal owns its work."""
    root = Path(root).resolve(strict=True)
    if instructions_path and continue_original:
        raise ValueError('--continue and --instructions are mutually exclusive.')
    if (instructions_path or resume_stopped or continue_original) and (status or read_result or stop):
        raise ValueError('Continuation starts a native turn; use a separate observation or stop action.')
    if resume_stopped and not instructions_path:
        raise ValueError('Explicit stop recovery needs its original owner instruction.')
    if basis and not instructions_path:
        raise ValueError('--basis accompanies the original --instructions.')
    if goal_max_rounds is not None and (type(goal_max_rounds) is not int or not 0<goal_max_rounds<=2**53-1
            or not instructions_path or not exact_run_id or resume_stopped):
        raise ValueError('An explicit Goal total requires --instructions and the exact --run-id for an unstopped author.')
    request = tasks.read_request(root, request_id)
    if request['kind'] not in ('plan', 'review', 'finding'):
        raise ValueError('Select an explicit work or review assignment.')
    runs = _program_runs(root, request)
    if status:
        return {'request_id': request_id, 'transport': 'programmatic', 'metering': _program_metering(root, request), 'runs': runs}
    worker = (runtime_factory or runtime.Runtime)(root)
    latest = next((r for r in reversed(runs) if r['request_id'] == request_id), None)
    exact = next((r for r in runs if r['id'] == exact_run_id), None) if exact_run_id else None
    if exact_run_id and not exact and not (instructions_path or continue_original):
        raise ValueError('Recover an exact original package or dependent review run.')
    if read_result or stop:
        run = exact or (runs[-1] if stop and runs else latest)
        selected = tasks.read_request(root, run['request_id']) if run and run['request_id'] != request_id else request
        if stop:
            return _program_read(root, worker, selected, run, stop=stop, config_path=config_path)
        return _program_result_view(root, worker, selected, run, _direct_return_id(root, selected['id']), config_path=config_path)
    if continue_original and not exact_run_id:
        raise ValueError('--continue requires the original --run-id.')
    if not instructions_path and not continue_original:
        latest = exact or latest
        selected = tasks.read_request(root, latest['request_id']) if latest and latest['request_id'] != request_id else request
        direct_id = _direct_return_id(root, selected['id'])
        if latest or direct_id:
            return _program_result_view(root, worker, selected, latest, direct_id, config_path=config_path)
    if continue_original:
        original = exact
        if original and original['target_host'] == 'deepseek_harness' and original['outcome'] == 'intent':
            from dsh_capacity_handoff import recover_abandoned
            recovered = recover_abandoned(root, worker, original, config_path=config_path, route=route)
            if recovered is not None:
                return {'request_id': request_id, **recovered}
    if not config_path:
        installed = installed_program(root)
        if not installed:
            raise runtime.CapabilityUnavailable(
                'Set programmatic_worker in .control/runtime.local.json or select a host with --config.')
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
    selected = tasks.read_request(root, exact['request_id']) if exact and exact['request_id'] != request_id else request
    try:
        if instructions_path or continue_original:
            selected, attempt = _continue_session(root, worker, request, config, instructions_path,
                                                   exact_run_id=exact_run_id, resume_stopped=resume_stopped,
                                                   goal_max_rounds=goal_max_rounds,basis=basis)
            if attempt.get('state')=='original_delivered':
                return attempt
            run = tasks.read_run(root, attempt['run_id'])
            view = _program_result_view(root, worker, selected, run, _direct_return_id(root, selected['id']))
            if recovery := attempt.get('automatic_continuation'):
                view['automatic_continuation'] = recovery
            return view
        return _drive_program(root, worker, request, config)
    except (runtime.CapabilityUnavailable, production_review.ReviewCapabilityUnavailable) as exc:
        latest = _selected_run(root, selected['id'])
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
    parser.add_argument('--basis',help='Original independent review return or revise decision for an already delivered unit')
    parser.add_argument('--goal-max-rounds',type=int,help='Owner-approved total for the exact exhausted DSH Goal')
    args = parser.parse_args(argv)
    try:
        if args.stop is not None and not args.stop.strip():
            raise ValueError('Provide the reason for stopping the original run.')
        result = programmatic(args.root, args.request, config_path=args.config, status=args.status,
            read_result=args.read_result, stop=args.stop, exact_run_id=args.run_id, route=args.route,
            instructions_path=args.instructions, resume_stopped=args.resume_stopped, continue_original=args.continue_original,
            goal_max_rounds=args.goal_max_rounds,basis=args.basis)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, entry.ProductionError) as exc:
        print(json.dumps({'error': safe_diagnostic(exc), 'request_id': args.request}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
