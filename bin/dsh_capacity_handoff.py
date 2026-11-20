#!/usr/bin/env python3
"""Maintain the original DSH capacity queue during a protocol handoff.

Fence provably never-started transports for explicit adoption. Retire only the
waiting tickets of proven exited executions, without delivering or replaying work.
"""
import argparse
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

import dsh_capacity as capacity
import execution_lease as execution
import harness_tasks as tasks


def _maintainer(root):
    actor=tasks.verified_actor(root)
    with contextlib.closing(tasks._connect(root,True)) as c:
        if 'maintainer' not in tasks._roles(c,actor):
            raise ValueError('Explicit capacity maintenance requires the current workspace maintainer')
    return actor


def _original(root,entry):
    run=tasks.read_run(root,entry['run_id'])
    if (run['request_id']!=entry['request_id'] or run['outcome']!='intent'
            or run['native_identity'] is not None or run['operation'] not in ('start','resume')
            or run['target_host']!='deepseek_harness'):
        raise ValueError('Only the original never-started DSH intent can be handed over: '+entry['run_id'])
    # Keep stop and task/assignment facts. The fresh runtime checks actual
    # admission again and cannot clear an original stop or paused responsibility.
    if run['stop_requested']:raise ValueError('Original stop remains in force: '+run['id'])
    tasks.evidence_ref(root,run['config_ref'])
    for ref in run['input_refs']:tasks.evidence_ref(root,ref)
    path=Path(entry['execution_path'])
    meta=capacity._read(path,{})
    if (meta.get('run_id')!=run['id'] or meta.get('request_id')!=run['request_id']
            or meta.get('manager')!=entry['manager'] or meta.get('child')
            or meta.get('tools') or meta.get('capacity_wait')
            or meta.get('phase')!='launch_intent'):
        raise ValueError('Original transport has child, native, tools or unverified attribution: '+run['id'])
    if run['operation']=='resume':
        previous,spec,_=_resume_binding(root,run)
        native=tasks.read_run(root,spec['native_parent_run_id'])['native_identity']
        if not native or meta.get('native_identity')!=native or native['id']!=spec.get('requested_session_id'):
            raise ValueError('Queued continuation lost its exact original requested Session: '+run['id'])
        if previous['stop_requested']:
            raise ValueError('Stopped predecessor requires its original explicit recovery, not inferred authorization: '+run['id'])
    elif meta.get('native_identity'):
        raise ValueError('A first start cannot already have a native identity: '+run['id'])
    return run,meta


def _resume_binding(root,run):
    from harness_package import launch_spec
    with contextlib.closing(tasks._connect(root,True)) as c:
        row=c.execute("SELECT payload_json FROM work_run_events WHERE run_id=? AND kind='intent' ORDER BY id LIMIT 1",(run['id'],)).fetchone()
    previous_id=json.loads(row['payload_json']).get('previous_run_id') if row else None
    spec=launch_spec(root,run)
    if not previous_id or not spec.get('native_parent_run_id') or not spec.get('continuation_ref'):
        raise ValueError('Preserve the exact queued continuation and original instruction: '+run['id'])
    previous=tasks.read_run(root,previous_id)
    if previous['target_host']!='deepseek_harness':raise ValueError('Original Session host differs')
    receipts=[event['receipt'] for event in previous['events'] if event.get('receipt')]
    retained=[]
    for ref in run['input_refs']:
        if ref in receipts and ref not in retained:retained.append(ref)
    if len(retained)!=1:
        raise ValueError('Queued continuation must retain its exact predecessor receipt: '+run['id'])
    return previous,spec,retained[0]


def _fixed(path,value):
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    from production_review import _save_fixed
    return _save_fixed(path.parent,path,raw)


def _spawn(root,entry):
    """A handoff ticket remains a FIFO barrier until its exact child claims it."""
    from agent_base import child_environment
    directory=Path(entry['execution_path']).parent
    with (directory/'transport-handoff.stdout').open('a') as out,(directory/'transport-handoff.stderr').open('a') as err:
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--root',str(root),
            '--run-id',entry['run_id'],'--token',entry['handoff_token']],cwd=root,
            env=child_environment(),stdin=subprocess.DEVNULL,stdout=out,stderr=err,
            start_new_session=True,close_fds=True)
    identity=execution.process_identity(child.pid)
    threading.Thread(target=child.wait,name='capacity-handoff-reaper',daemon=True).start()
    return identity


def _complete_observation(root):
    for attempt in range(3):
        observed = capacity._observe(root, cached=False)
        if not observed['attribution_issues']:
            return observed
        if attempt < 2:
            time.sleep(.05)
    raise ValueError('OS attribution is incomplete; preserve the original queue and retry observation')


def adopt(root,source,*,dry_run=False):
    root=Path(root).resolve();actor=_maintainer(root)
    from harness_sessions import reference
    authority=reference(root,source)
    result={'policy':capacity.POLICY,'limits':capacity.LIMITS,'dry_run':dry_run,'transports':[]}
    with capacity._lock(root):
        queue=capacity._queue(root)
        if queue.get('policy')==capacity.PILOT_POLICY:
            raise ValueError('Drain the provider pilot before restoring the six-seat role policy.')
        observed = _complete_observation(root)
        processes={p['pid']:p for p in observed['processes']}
        old=[e for e in queue['entries'] if e['phase']=='queued' and not e.get('wait_token') and
            (not capacity.current_ticket(e) or e.get('handoff_token') and
             execution.process_identity(e['manager']['pid'])['exists'] is False)]
        candidates=[]
        # Preflight every legacy transport before any signal. Shared managers,
        # marked descendants and unknown ownership cannot be fenced as a queue.
        for entry in old:
            run,meta=_original(root,entry)
            dead_handoff=bool(entry.get('handoff_token') and capacity.current_ticket(entry)
                and execution.process_identity(entry['manager']['pid'])['exists'] is False)
            if (entry['manager']['pid']==os.getpid()
                    or not dead_handoff and capacity._matches(entry['manager'],processes,observed['boot']) is not True
                    or run['id'] in observed['marked_runs']
                    or any(r['run_id']==run['id'] and r['reason']!='original_manager_starting' for r in observed['occupied'])
                    or any(e['run_id']!=run['id'] and e.get('manager')==entry['manager'] for e in queue['entries'])):
                raise ValueError('Queued manager is not an exclusive, proven never-started transport: '+run['id'])
            candidates.append((entry,run,meta,dead_handoff))
            result['transports'].append({'run_id':run['id'],'request_id':run['request_id'],
                'sequence':entry['sequence'],'original_manager':entry['manager'],'native_identity':None})
        legacy_live=[e for e in queue['entries'] if e not in old and not capacity.current_ticket(e)]
        protocol=(capacity.PREVIOUS_POLICY if queue.get('policy')==capacity.PREVIOUS_POLICY and legacy_live else capacity.POLICY)
        result['protocol_policy']=protocol
        result['legacy_runtime_tickets_preserved']=[e['run_id'] for e in legacy_live]
        if dry_run:return {**result,'started_executions_preserved':len(capacity._effective(queue,observed)[0])}
        for entry,run,meta,dead_handoff in candidates:
            token=uuid.uuid4().hex;directory=Path(entry['execution_path']).parent
            source_bytes=Path(authority['path']).read_bytes()
            from production_review import _save_fixed
            frozen_source=_save_fixed(root,directory/('handoff-authority-'+authority['sha256']+Path(authority['path']).suffix),source_bytes)
            before={'source':'explicit_maintainer_transport_handoff','policy':capacity.POLICY,
                'authority':frozen_source,'actor':actor,'original_ticket':dict(entry),'original_execution':meta,
                'run_id':run['id'],'request_id':run['request_id'],'config_ref':run['config_ref'],
                'input_refs':run['input_refs'],'stop_requested':run['stop_requested'],'token':token}
            intent=_fixed(directory/('transport-handoff-'+token+'-intent.json'),before)
            # Still under the same capacity lock: the old admission loop cannot
            # pass between this final identity check and exact-PID termination.
            if not dead_handoff:
                if execution.matches(entry['manager']) is not True:
                    raise ValueError('Original queued manager identity changed before fencing: '+run['id'])
                os.kill(entry['manager']['pid'],signal.SIGTERM)
            until=time.monotonic()+5
            while execution.matches(entry['manager']) is True and time.monotonic()<until:time.sleep(.03)
            if execution.matches(entry['manager']) is not False:
                raise ValueError('Original queued manager exit unproven; preserve handoff intent: '+intent['path'])
            # No native/child could have launched while this lock was held.
            _original(root,entry)
            fenced=_fixed(directory/('transport-handoff-'+token+'-fenced.json'),
                {'intent_ref':intent,'old_manager_exited':True,'native_started':False,'fenced_at':time.time()})
            entry.update(phase='handoff',handoff_token=token,handoff_ref=fenced,policy=protocol,admission_policy=capacity.POLICY)
            execution.atomic_json(capacity._paths(root)[0],queue)
            tasks.mutate(root,'run-capacity',actor,{'id':run['id'],'phase':'transport_handoff',
                'kind':entry['kind'],'sequence':entry['sequence'],'queue_source':str(capacity._paths(root)[0])})
        # Existing started legacy invocations remain conservatively counted.
        queue.update(policy=protocol,admission_policy=capacity.POLICY)
        for entry in queue['entries']:
            if capacity.current_ticket(entry):entry['policy']=protocol
        execution.atomic_json(capacity._paths(root)[0],queue)
    # Durable barriers and receipts precede child creation. A failed spawn does
    # not lose or advance the original ticket; this same command recovers it.
    resumed=[]
    with capacity._lock(root):
        queue=capacity._queue(root)
        for entry in list(queue['entries']):
            if entry['phase']!='handoff' or entry.get('owner_recovery'):continue
            run=tasks.read_run(root,entry['run_id'])
            if run['stop_requested'] and run['outcome']=='intent' and run['native_identity'] is None:
                queue['entries'].remove(entry)
                tasks.mutate(root,'run-capacity',actor,{'id':run['id'],'phase':'cancelled_before_start',
                    'kind':entry['kind'],'sequence':entry['sequence'],'queue_source':str(capacity._paths(root)[0])})
                continue
            helper=entry.get('handoff_child')
            if helper and execution.matches(helper) is not False:continue
            identity=_spawn(root,entry);entry['handoff_child']=identity
            resumed.append({'run_id':entry['run_id'],'sequence':entry['sequence'],'manager':identity})
        execution.atomic_json(capacity._paths(root)[0],queue)
    return {**result,'reattached':resumed,'snapshot':capacity.snapshot(root)}


def provider_pilot(root,source,provider=None,*,dry_run=False):
    """Change the existing queue protocol only in a fully observed empty window."""
    from harness_sessions import reference
    root=Path(root).resolve()
    actor=_maintainer(root)
    authority=reference(root,source)
    if provider is not None and (not provider or provider.strip()!=provider or any(c.isspace() for c in provider)):
        raise ValueError('Provide the exact configured DSH provider identifier.')
    with capacity._lock(root):
        queue=capacity._queue(root)
        observed=_complete_observation(root)
        active,counts,_=capacity._effective(queue,observed)
        before=queue.get('pilot_provider')
        live_managers=[]
        if not queue['entries'] and not active and not observed['marked_runs']:
            for binding in observed['catalog'].values():
                path=binding.get('execution_path')
                original=capacity._read(Path(path),{}) if path else {}
                manager=original.get('manager')
                if original.get('phase') not in (None,'settled','manager_released') and manager and execution.matches(manager) is not False:
                    live_managers.append(binding['run_id'])
        ready=(capacity.policy_ready(queue) and not queue['entries'] and not active
               and not observed['marked_runs'] and not live_managers)
        change={'provider':provider,'previous_provider':before,'eligible':ready,'dry_run':dry_run,
                'policy':capacity.PILOT_POLICY if provider else capacity.POLICY,
                'limits':capacity.PILOT_LIMITS if provider else capacity.LIMITS,
                'existing_tickets':len(queue['entries']),'occupied':counts,'live_pre_admission_managers':live_managers}
        if dry_run:return change
        if not ready:
            raise ValueError('Wait for all original DSH tickets, executions and reservations to exit before switching the queue protocol.')
        queue.update(policy=change['policy'],pilot_provider=provider,
                     pilot_change={'authority':authority,'actor':actor,'observed_at':observed['at']})
        queue.pop('admission_policy',None)
        execution.atomic_json(capacity._paths(root)[0],queue)
        return change


def retire_exited_waits(root, source, *, dry_run=False):
    """Apply the same exit proof for managers that already loaded older code."""
    from harness_sessions import reference
    root = Path(root).resolve()
    actor, authority = _maintainer(root), reference(root, source)
    with capacity._lock(root):
        queue = capacity._queue(root)
        observed = _complete_observation(root)
        waits = capacity._exited_review_waits(root, queue, observed)
        if waits and not dry_run:
            capacity._retire_exited_waits(root, queue, actor, waits, authority=authority)
            capacity._finish_protocol_handoff(queue)
            execution.atomic_json(capacity._paths(root)[0], queue)
        return {'dry_run': dry_run, 'authority_ref': authority, 'observed_at_unix': observed['at'],
                'exited_waits': sorted(waits.values(), key=lambda item: item['sequence']),
                'retired': [] if dry_run else list(waits), 'protocol_policy': queue.get('policy'),
                'business_outcome_unchanged': True}


def abandoned_transport(root, run, *, queue=None):
    """Read the original FIFO coordinates only for a proven unadmitted intent."""
    if (run['target_host'] != 'deepseek_harness' or run['outcome'] != 'intent' or run['stop_requested']
            or run['native_identity'] or run['latest_receipt']
            or [e['kind'] for e in run['events']] != ['intent', 'capacity', 'capacity']):
        return None
    _, queued, abandoned = run['events']
    before, after = queued['payload'], abandoned['payload']
    if (before.get('phase') != 'queued' or after != {**before, 'phase': 'abandoned'}
            or before.get('queue_source') != str(capacity._paths(root)[0])):
        return None
    from harness_package import launch_spec
    try:
        queue = queue if queue is not None else capacity._queue(root)
        sequence = before['sequence']
        if (not capacity.policy_ready(queue) or sequence >= queue['next_sequence']
                or any(e['run_id'] == run['id'] or e['sequence'] == sequence for e in queue['entries'])):
            return None
        spec = launch_spec(root, run)
        path = execution.metadata_path(spec['evidence_home'], run['id'])
        meta = capacity._read(path, {})
        manager, session = meta['manager'], spec.get('requested_session_id')
        pilot=queue.get('policy')==capacity.PILOT_POLICY
        provider=None
        if pilot:
            request=tasks.read_request(root,run['request_id'])
            role='scientific_reviewer' if request['independent'] else request['recipient_role']
            provider=capacity._provider(root,run['config_ref'],role)
            if not provider:
                return None
        entry = {'run_id': run['id'], 'request_id': run['request_id'], 'sequence': sequence,
                 'kind': before['kind'], 'phase': 'queued',
                 'queued_at': datetime.fromisoformat(queued['created_at']).timestamp(), 'manager': manager,
                 'execution_path': str(path), 'native_session': session,
                 'policy': queue.get('policy'), 'admission_policy': capacity.PILOT_POLICY if pilot else capacity.POLICY,
                 **({'provider':provider} if pilot else {})}
        _original(root, entry)
        observed = _complete_observation(root)
        if (not manager.get('start') or not manager.get('boot') or not manager.get('pgid') or not observed['boot']
                or execution.matches(manager) is not False or run['id'] in observed['marked_runs']
                or manager['boot'] == observed['boot'] and any(p['pgid'] == manager['pgid'] for p in observed['processes'])
                or any(r['run_id'] == run['id'] or session and r.get('native_session') == session
                       for r in observed['occupied'])
                or any(capacity._writer(p) is not False for p in capacity._writer_paths(root, run['config_ref'], session))):
            return None
        if run['operation'] == 'resume':
            previous, _, _ = _resume_binding(root, run)
            if execution.observe_run(root, previous)['state'] != 'exited':
                return None
        return entry
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return None


def recover_abandoned(root, worker, original, *, config_path=None, route=None):
    """Respond to an explicit owner continuation of this same launch intent."""
    if original['target_host'] != 'deepseek_harness' or original['outcome'] != 'intent':
        return None
    from harness_sessions import reference
    from harness_hosts import ROUTE_ADAPTERS
    from run_worker import _selected_run
    run_id = original['id']
    request = tasks.read_request(root, original['request_id'])
    with capacity._lock(root):
        queue = capacity._queue(root)
        latest = _selected_run(root, request['id'])
        if not latest or latest['id'] != run_id or latest['stop_requested'] or latest['outcome'] != 'intent':
            return None
        current = tasks.read_run(root, run_id)
        entry = next((e for e in queue['entries'] if e['run_id'] == run_id
                      and e['phase'] == 'handoff' and e.get('owner_recovery')), None)
        staged = entry is not None
        if entry is None:
            entry = abandoned_transport(root, current, queue=queue)
        if entry is None:
            return None
        task = tasks.read_task(root, request['task_id'])['task']
        owners = (current['dispatcher_actor'], task['owner_actor'], request['sender_actor'], request['recipient_actor'])
        if tasks.native_key(worker.actor) not in {tasks.native_key(a) for a in owners if a}:
            raise ValueError('Only the original responsible actor can continue this abandoned transport.')
        tasks.admission(root, task['id'], worker.actor, request_id=request['id'], side_effect=True)
        if (config_path and reference(root, config_path) != current['config_ref']
                or route and ROUTE_ADAPTERS.get(route) != 'dsh-headless'):
            raise ValueError('The abandoned transport retains its original DSH configuration and input.')
        if not staged:
            _, meta = _original(root, entry)
            token = uuid.uuid4().hex
            directory = Path(entry['execution_path']).parent
            intent = _fixed(directory / ('transport-handoff-' + token + '-intent.json'), {
                'source': 'owner_continue_abandoned_transport', 'actor': worker.actor,
                'run_id': run_id, 'request_id': request['id'], 'original_ticket': dict(entry),
                'original_execution': meta, 'config_ref': current['config_ref'], 'input_refs': current['input_refs'],
                'original_queue_events': current['events'][1:], 'token': token})
            fenced = _fixed(directory / ('transport-handoff-' + token + '-fenced.json'), {
                'intent_ref': intent, 'old_manager_exited': True, 'native_started': False, 'fenced_at': time.time()})
            entry.update(phase='handoff', handoff_token=token, handoff_ref=fenced, owner_recovery=True)
            queue['entries'].append(entry)
            execution.atomic_json(capacity._paths(root)[0], queue)
            tasks.mutate(root, 'run-capacity', worker.actor, {'id': run_id, 'phase': 'transport_handoff',
                'kind': entry['kind'], 'sequence': entry['sequence'], 'queue_source': str(capacity._paths(root)[0])})
    return run(root, run_id, entry['handoff_token'], worker=worker)


def claim(root,run,lease,token):
    """Claim an explicitly fenced original transport under its execution lease."""
    with capacity._lock(root):
        queue=capacity._queue(root)
        entry=next((e for e in queue['entries'] if e['run_id']==run['id']),None)
        if (not capacity.policy_ready(queue) or not entry or entry['phase']!='handoff'
                or entry.get('handoff_token')!=token):
            raise ValueError('Original fenced transport token is missing or already claimed')
        current,_=_original(root,entry)
        fenced=json.loads(Path(tasks.evidence_ref(root,entry['handoff_ref'])['path']).read_bytes())
        intent=json.loads(Path(tasks.evidence_ref(root,fenced['intent_ref'])['path']).read_bytes())
        if (fenced.get('native_started') is not False or not fenced.get('old_manager_exited')
                or intent['token']!=token or intent['original_ticket']['manager']!=entry['manager']
                or intent['input_refs']!=current['input_refs'] or intent['config_ref']!=current['config_ref']
                or execution.matches(entry['manager']) is not False):
            raise ValueError('Original handoff no longer proves a never-started transport')
        entry.update(phase='queued',manager=lease.value['manager'],manager_attached_at=time.time(),claimed_handoff=token)
        execution.atomic_json(capacity._paths(root)[0],queue)


def run(root,run_id,token,*,worker=None):
    import agent_runtime
    from harness_package import launch_spec
    root=Path(root).resolve();original=tasks.read_run(root,run_id)
    request=tasks.read_request(root,original['request_id'])
    role='scientific_reviewer' if request['independent'] else request['recipient_role']
    parent=tasks.read_request(root,request['parent_request_id']) if request['independent'] else request
    config=agent_runtime.load_config(root,original['config_ref']['path'],role)
    spec=launch_spec(root,original)
    if not spec.get('evidence_home'):raise ValueError('Original launch home missing; preserve transport')
    context=({'parent_brief_ref':parent['input_ref']} if request['independent'] else {}) if config['programmatic'] else None
    previous_id=None
    if original['operation']=='resume':
        previous,spec,prior_receipt=_resume_binding(root,original);previous_id=previous['id']
        if context is None:raise ValueError('Original continuation must use its programmatic recovery context')
        context['technical_recovery']={'ref':spec['continuation_ref'],
            'evidence_refs':[prior_receipt]}
        context['prior_receipt_ref']=prior_receipt
    return (worker or agent_runtime.Runtime(root)).execute(config_path=original['config_ref']['path'],role=role,
        task_id=request['task_id'],request_id=request['id'],run_id=run_id,
        input_path=original['input_refs'][0]['path'],input_sha256=original['input_refs'][0]['sha256'],
        evidence_home=spec['evidence_home'],operation=original['operation'],previous_run_id=previous_id,
        program_context=context,
        queued_handoff=token)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('root','run-id','token'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args()
    try:print(json.dumps(run(args.root,args.run_id,args.token),ensure_ascii=False))
    except Exception as exc:
        from safe_diagnostics import retain
        # Startup/claim can fail before Runtime installs its diagnostic context.
        # Keep the complete safe cause beside the exact original transport.
        try:
            entry=next((e for e in capacity._queue(Path(args.root).resolve())['entries']
                        if e['run_id']==args.run_id),None)
        except (OSError,ValueError):entry=None
        directory=Path(entry['execution_path']).parent/'diagnostics' if entry else None
        print(json.dumps(retain(str(exc),directory=directory,source='capacity_handoff',code=type(exc).__name__)),file=sys.stderr)
        raise SystemExit(1)
