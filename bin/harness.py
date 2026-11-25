#!/usr/bin/env python3
"""Workspace task facts and explicit actions. Native tools own actual data work."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import harness_tasks as tasks
from task_ledger import decisions

ROOT = Path(__file__).resolve().parent.parent
HISTORY_TABLES = ('handoffs', 'handoff_executors', 'engineering_routes', 'engineering_executors')


def task_command(root, args):
    action = args.command.removeprefix('work-')
    if action == 'capacity':
        import dsh_capacity
        if args.host == 'devin':
            if args.provider_pilot is not None or any((args.end_provider_pilot, args.adopt_policy,
                    args.retire_exited_waits, args.source, args.dry_run)):
                raise ValueError('Select the DSH pool for DSH queue maintenance actions.')
            return dsh_capacity.snapshot(root, dsh_capacity.DEVIN)
        if args.provider_pilot is not None or args.end_provider_pilot:
            if not args.source:raise ValueError('Capacity maintenance requires the original --source')
            from dsh_capacity_handoff import provider_pilot
            return provider_pilot(root,args.source,args.provider_pilot,dry_run=args.dry_run)
        if args.adopt_policy or args.retire_exited_waits:
            if not args.source:raise ValueError('Capacity maintenance requires the original --source')
            from dsh_capacity_handoff import adopt, retire_exited_waits
            action = adopt if args.adopt_policy else retire_exited_waits
            return action(root,args.source,dry_run=args.dry_run)
        if args.source or args.dry_run:raise ValueError('--source/--dry-run require a capacity maintenance action')
        return dsh_capacity.snapshot(root)
    if action == 'check':
        import harness_scope
        kind,ident=('task',args.task) if args.task else ('request',args.request)
        if args.summary and args.for_action != 'handoff':
            raise ValueError('Use --summary with --for handoff.')
        return harness_scope.check(root,kind,ident,args.for_action,summary=args.summary)
    if action == 'review-record':
        import harness_scope
        return harness_scope.review_record(root,tasks.verified_actor(root),args.return_id,args.conclusion,args.basis,
                                           supersedes=args.supersedes,recurs_after=args.recurs_after)
    if action == 'submit-batch':
        from platform_review.submission import submit
        return submit(root,args.task,args.input,adoption=args.adoption,request=args.request,use=args.use,execute=args.execute)
    if action == 'scope':
        import harness_scope
        if args.dry_run and not args.apply:raise ValueError('--dry-run requires --apply FILE.')
        if args.apply:
            return harness_scope.apply_scope(root,args.task,json.loads(Path(args.apply).read_bytes()),tasks.verified_actor(root),dry_run=args.dry_run)
        return harness_scope.read_scope(root,args.task)
    if action == 'contract':
        import harness_scope
        kind,ident=('task',args.task) if args.task else ('request',args.request)
        if args.set:
            if not args.source:raise ValueError('--set requires the original --source.')
            return harness_scope.save_contract(root,tasks.verified_actor(root),kind,ident,args.set,args.source)
        if args.source:raise ValueError('--source is used with --set.')
        return harness_scope.read_contract(root,kind,ident)
    if action == 'end':
        return tasks.end_delegation(root,tasks.verified_actor(root),args.request,args.note)
    if action == 'packages':
        import harness_package
        result=harness_package.snapshot(root,args.task,detail=args.detail)
        if args.limit is not None:
            if not 1<=args.limit<=100 or args.offset<0:raise ValueError('Use limit 1..100 and nonnegative offset.')
            all_packages=result['packages'];result['packages']=all_packages[args.offset:args.offset+args.limit]
            next_offset=args.offset+args.limit if args.offset+args.limit<len(all_packages) else None
            result['pagination']={'total':len(all_packages),'shown':len(result['packages']),'offset':args.offset,'next_offset':next_offset,
                'next_page_command':'python3 bin/harness.py work-packages'+(' --task '+args.task if args.task else '')+' --limit '+str(args.limit)+' --offset '+str(next_offset) if next_offset is not None else None,
                'scope_denominator':'task_scopes contains complete scope independently of this package page'}
        elif args.offset:raise ValueError('--offset requires an explicit --limit for package pages.')
        return result
    if action in ('whoami', 'assignments', 'enroll', 'accept', 'dispatch'):
        from task_ledger import sessions as harness_sessions
        actor = tasks.verified_actor(root)
        if action in ('whoami', 'assignments'):
            return harness_sessions.overview(root, actor, limit=args.limit, offset=args.offset,
                                             assignments_only=action == 'assignments')
        if action == 'enroll':
            return harness_sessions.enrollment(root, actor, args.authorization, args.role)
        if action == 'accept':
            return harness_sessions.accept(root, actor, args.request, task_id=args.task)
        return harness_sessions.dispatch(root, actor, args.request, task_id=args.task, config=args.config,
                                         execute=args.execute, instructions=args.instructions, run_id=args.run_id,
                                         resume_stopped=args.resume_stopped, continue_original=args.continue_original,
                                         goal_max_rounds=args.goal_max_rounds,basis=args.basis)
    if action in ('maintenance', 'maintenance-delegate', 'claim-finding', 'finding'):
        from task_ledger import maintenance as harness_maintenance
        if action == 'maintenance':
            return harness_maintenance.queue(root, limit=args.limit, offset=args.offset)
        actor = tasks.verified_actor(root)
        if action == 'maintenance-delegate':
            return harness_maintenance.delegate(root, actor, args.request, args.brief, recipient=args.recipient)
        if action == 'claim-finding':
            return harness_maintenance.claim(root, actor, args.request, authorization=args.authorization)
        return harness_maintenance.report(root, actor, args.task, args.report, finding_id=args.finding)
    if action == 'list':
        return tasks.current_view(root, tasks.verified_actor(root) if args.mine else None,
                            limit=args.limit, offset=args.offset, include_closed=args.all)
    if action == 'show':
        if args.wait_seconds is not None or args.after is not None or args.mine or len(args.id or ()) > 1:
            import harness_wave
            if args.detail or args.history or args.wave or args.scope:
                raise ValueError('Waiting uses the current responsibility view.')
            return harness_wave.wait(root, args.id or (), args.after, args.wait_seconds or 0, mine=args.mine,
                                     limit=args.limit, offset=args.offset)
        if not args.id:
            raise ValueError('Select --id TASK or --mine.')
        ident, = args.id
        import harness_snapshot
        try:
            if args.detail:return tasks.read_task(root,ident)
            return harness_snapshot.read(root,task_id=ident,limit=args.limit,offset=args.offset,
                                         history=args.history,wave=args.wave,scope_id=args.scope)['work']
        except tasks.WorkConflict as missing_task:
            if missing_task.code != 'not_found':
                raise
            if args.scope is not None:
                raise
            if not args.detail and not args.history and not args.wave:
                try:
                    return harness_snapshot.read(root,request_id=ident,limit=args.limit,offset=args.offset)['work']
                except tasks.WorkConflict as missing_request:
                    if missing_request.code!='not_found':
                        raise
            try:
                run = tasks.read_run(root, ident)
            except tasks.WorkConflict as missing_run:
                if missing_run.code != 'not_found':
                    raise
                raise missing_task from None
        with contextlib.closing(tasks._connect(root,True)) as c:
            request = tasks._row(c,'work_requests',run['request_id'])
            run['commitment'] = harness_snapshot.commitment(c,root,request['task_id'],run['plan_revision'])
        if args.detail:
            return run
        return {key:run[key] for key in ('id','request_id','input_refs','config_ref','plan_revision','commitment',
                                       'input_bindings','execution_basis') if key in run}
    actor = tasks.verified_actor(root)
    if action=='wait':
        import harness_wave
        return harness_wave.defer(root,args.task,args.item,until=args.until,request=args.for_request,
                                  path=args.for_file,reason=args.reason,external=args.external)
    if action=='wave':
        import harness_wave
        return harness_wave.bookmark(root,args.task,args.decision)
    if action == 'create' and args.brief:
        from task_ledger import sessions as harness_sessions
        return harness_sessions.create(root, actor, args.brief, args.owner, task_id=args.id,contract_path=args.contract)
    if action == 'request' and args.brief:
        if not args.task:
            raise ValueError('Normal assignment requires the original --task')
        from task_ledger import sessions as harness_sessions
        return harness_sessions.request(root, actor, args.task, args.brief, review_of=args.review_of,
                                        contract_path=args.contract,scope_ids=args.scope,continues=args.continues)
    if action in ('revise','reopen','pause','resume','cancel','complete','transfer') and args.note:
        if not args.task:
            raise ValueError('Select the original --task for this decision.')
        from task_ledger import sessions as harness_sessions
        task = tasks._read_one(root,'work_tasks',args.task)
        note = harness_sessions.reference(root,args.note)
        ref=note
        contract_body=None
        if action=='revise':
            ref=(harness_sessions.reference(root,args.reason_file) if args.reason_file else
                 tasks.save_note(root,Path(task['plan_ref']['path']).parent,'revision-reason',args.reason))
        key = [action,args.task,ref['sha256'] if action=='revise' else ref]
        if action == 'revise':
            if len(args.base_sha256) != 64 or any(ch not in '0123456789abcdef' for ch in args.base_sha256):
                raise ValueError('Use the SHA-256 of the accepted commitment returned by work-show.')
            key.extend((args.base_sha256, note['sha256'],sorted(args.affected_scope),sorted(args.affected_request)))
            if args.contract:
                import harness_scope
                contract_ref = harness_sessions.reference(root,args.contract)
                contract_body=harness_scope.validate_contract(root,json.loads(Path(contract_ref['path']).read_bytes()))
                key.append(contract_body)
        ident = 'task-action:'+hashlib.sha256(tasks._json(key).encode()).hexdigest()
        with contextlib.closing(tasks._connect(root,True)) as c:
            prior = c.execute("SELECT actor_json,payload_json FROM work_task_events WHERE task_id=? AND kind=? AND json_extract(payload_json,'$.id')=?", (args.task,action,ident)).fetchone() if action!='complete' else None
            if action=='revise' and not prior:
                # Preserve readback of the old path-keyed revision receipts.
                for event in c.execute("SELECT actor_json,payload_json FROM work_task_events WHERE task_id=? AND kind='revise' ORDER BY id DESC",(args.task,)):
                    saved=json.loads(event['payload_json']).get('action_payload',{})
                    if (saved.get('base_sha256')==args.base_sha256
                            and saved.get('plan_ref',{}).get('sha256')==note['sha256']
                            and saved.get('evidence_ref',{}).get('sha256')==ref['sha256']
                            and sorted(saved.get('affected_scope',[]))==sorted(args.affected_scope)
                            and sorted(saved.get('affected_request_ids',[]))==sorted(args.affected_request)
                            and saved.get('delivery_contract')==contract_body):
                        prior=event
                        break
            if prior and tasks.native_key(prior['actor_json']) == tasks.native_key(actor):
                return json.loads(prior['payload_json'])['result']
        payload = {'id':ident,
                   'task_id':args.task,'expected_task_revision':task['revision'],
                   'plan_revision':task['plan_revision'],'evidence_ref':ref,
                   'summary':harness_sessions.summary(ref['path'], 'See the original decision')}
        if action in ('revise','reopen'):
            payload['plan_ref'] = note
        if action == 'revise':
            payload.pop('expected_task_revision')
            payload.pop('plan_revision')
            payload.update(base_sha256=args.base_sha256, affected_scope=args.affected_scope,
                           affected_request_ids=args.affected_request)
            if contract_body is not None:
                payload['delivery_contract']=contract_body
        if action == 'revise' and args.authorization:
            payload['human_authorization_ref'] = harness_sessions.reference(root,args.authorization)
        if action == 'transfer':
            payload.update(new_owner_actor=harness_sessions.target(root,args.owner),recovery_ref=ref)
        return tasks.mutate(root,action,actor,payload)
    if action == 'maintenance-note':
        from task_ledger import maintenance as harness_maintenance
        return harness_maintenance.record_note(root, actor, args.request, args.note, args.status, duplicate_of=args.duplicate_of)
    if action == 'init':
        from task_ledger.sessions import reference
        ref = reference(root,args.authorization)
        return tasks.initialize(root,actor,ref,'init:'+ref['sha256'])
    if action == 'return' and (args.result or args.run):
        import run_worker
        return run_worker.return_result(root, actor, args.result, request_id=args.request, run_id=args.run,
                                        scope_ids=args.scope,artifact_paths=args.artifact,candidate_paths=args.candidate)
    if action == 'dispose' and args.decision:
        return tasks.dispose_result(root, actor, args.return_id, args.decision, disposition=args.use,
                                    scope_ids=args.scope,acceptance=args.acceptance,checks_path=args.checks,
                                    apply_review_ids=args.apply_review,handling_paths=args.handling,wave=args.wave)
    if action == 'business-bind' and args.package:
        from task_ledger import business as harness_business
        return harness_business.bind_package(root, actor, args.task, args.package)
    if action == 'business-observe' and args.capture:
        from task_ledger import business as harness_business
        return harness_business.observe_package(root, actor, args.task, args.capture, args.package_id, args.expected_account_user_id)
    raise ValueError('Select the original request/run and report for this action.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    tasks.add_parsers(sub)
    decisions.add_parsers(sub)
    history = sub.add_parser('history', help='Read retained records from retired protocols; never execute them.')
    history.add_argument('--table', choices=HISTORY_TABLES, required=True)
    history.add_argument('--id')
    history.add_argument('--limit', type=int, default=20)
    history.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == 'history':
        if not 1 <= args.limit <= 100 or args.offset < 0:
            raise ValueError('Use limit 1..100 and nonnegative offset.')
        c = tasks._connect(root, True)
        try:
            if not tasks._exists(c, args.table):
                result = []
            elif args.id:
                result = [dict(row) for row in c.execute(f'SELECT * FROM {args.table} WHERE id=?', (args.id,))]
            else:
                result = [dict(row) for row in c.execute(f'SELECT * FROM {args.table} ORDER BY id LIMIT ? OFFSET ?', (args.limit,args.offset))]
        finally:
            c.close()
    else:
        result = decisions.dispatch(root, args)
        if result is None:
            result = task_command(root, args)
    if getattr(args,'output',None):
        import harness_snapshot
        result=harness_snapshot.export(root,args.output,result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        result = {'error':str(exc)}
        if isinstance(exc,tasks.WorkConflict):
            result.update(code=exc.code, record_id=exc.record_id)
            if hasattr(exc,'review_request'):
                result['review_request']=exc.review_request
                result['objects']=sorted(exc.review_units)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
