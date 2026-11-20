"""Workspace DSH admission and the optional provider-specific four-seat policy.

The queue owns waiting/reservations, never business or native execution state.
Original run metadata and OS ownership determine occupancy, including old runs.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import execution_lease as execution
import harness_tasks as tasks
from agent_base import StopRequested

LIMITS = {'author': 6, 'review': 6, 'total': 12}
POLICY = 'six-per-role-fifo-v1'
PREVIOUS_POLICY = 'unified-four-fifo-v1'
PILOT_POLICY = 'provider-four-eligible-fifo-v1'
PILOT_LIMITS = {'author':3,'review':3,'total':4}
POLL_SECONDS = .2
OBSERVATION_SECONDS = 1.
OBSERVATION_SCHEMA = 'htam.dsh-execution-observation.v3'
SCHEMA = 'htam.dsh-capacity.v1'


def _paths(root):
    control = Path(root) / '.control'
    return (control / 'dsh-capacity.json', control / 'dsh-capacity.lock',
            control / 'dsh-capacity-observed.json')


def _read(path, default=None):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def _queue(root):
    value = _read(_paths(root)[0], {'schema': SCHEMA, 'policy': POLICY, 'next_sequence': 1, 'entries': []})
    if value.get('schema') != SCHEMA or not isinstance(value.get('entries'), list):
        raise ValueError('Original DSH capacity queue is unreadable; preserve it for recovery.')
    if value.get('policy')==PILOT_POLICY and (not isinstance(value.get('pilot_provider'),str) or not value['pilot_provider']):
        raise ValueError('The pilot queue is missing its original provider identity.')
    return value


def policy_ready(queue):
    return queue.get('policy') in (POLICY,PILOT_POLICY) or (queue.get('policy')==PREVIOUS_POLICY
        and queue.get('admission_policy')==POLICY)


def is_pilot_provider(root, provider):
    queue = _queue(root)
    return bool(provider and queue.get('policy') == PILOT_POLICY and queue.get('pilot_provider') == provider)


def current_ticket(entry):
    return entry.get('policy') in (POLICY,PILOT_POLICY) or entry.get('admission_policy') in (POLICY,PILOT_POLICY)


def _finish_protocol_handoff(queue):
    # Already-loaded four-seat managers still read the original wire tag.
    # Retire it only after their tickets have naturally gone; do not reload or
    # interrupt their native executions. All new admissions use role limits.
    if (queue.get('policy')==PREVIOUS_POLICY and policy_ready(queue)
            and all(current_ticket(e) for e in queue['entries'])):
        queue['policy']=POLICY
        for entry in queue['entries']:entry['policy']=POLICY
        return True
    return False


@contextlib.contextmanager
def _lock(root, guard=lambda: None):
    path = _paths(root)[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    os.set_inheritable(fd, False)
    try:
        while True:
            guard()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(POLL_SECONDS)
        yield
    finally:
        os.close(fd)


def _processes():
    command = ['ps', '-axo', 'uid=,pid=,ppid=,pgid=,stat=,lstart=']
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as helper:
        try:
            stdout, stderr = helper.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            helper.kill(); helper.communicate()
            raise
        if helper.returncode:
            raise subprocess.CalledProcessError(helper.returncode, command, stderr=stderr)
    result = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) != 10:
            raise ValueError('Incomplete OS capacity observation')
        if int(parts[0]) == os.getuid() and not parts[4].startswith('Z'):
            result[int(parts[1])] = {'pid': int(parts[1]), 'ppid': int(parts[2]),
                                   'pgid': int(parts[3]), 'start': ' '.join(parts[5:])}
    result.pop(helper.pid, None)
    return result


def _matches(record, processes, boot):
    if not record or not record.get('pid'):
        return False
    current = processes.get(record['pid'])
    if not current:
        return False
    if not record.get('start') or not record.get('boot') or not boot:
        return None
    return (record['start'], record['boot']) == (current['start'], boot)


def _writer_paths(root, config_ref, native_id):
    if not native_id or not all(c.isalnum() or c in '-_' for c in native_id):
        return []
    config = json.loads(Path(tasks.evidence_ref(root, config_ref)['path']).read_bytes())
    return [str(path) for host in config.get('hosts', {}).values()
            if host.get('adapter') == 'dsh-headless' and host.get('dsh_home')
            for path in (Path(host['dsh_home']) / 'sessions').glob('*/' + native_id + '/session.lock')]


def _provider(root, config_ref, role):
    config=json.loads(Path(tasks.evidence_ref(root,config_ref)['path']).read_bytes())
    key=config.get('roles',{}).get(role,{}).get('provider')
    return config.get('providers',{}).get(key,{}).get('dsh_provider')


def _catalog(root, prior):
    """Cache only exact original launch coordinates, never live occupancy."""
    from harness_package import launch_spec
    result = {}
    with contextlib.closing(tasks._connect(root, True)) as c:
        rows = c.execute('''SELECT r.*,q.independent,q.recipient_role FROM work_runs r
            JOIN work_requests q ON q.id=r.request_id WHERE r.target_host='deepseek_harness'
            ORDER BY r.created_at,r.id''').fetchall()
    for raw in rows:
        run = tasks._decode(raw)
        signature = hashlib.sha256(tasks._json([run['input_refs'], run.get('native_identity'),
                                                 run['independent'],run['recipient_role']]).encode()).hexdigest()
        old = prior.get(run['id'])
        if old and old['signature'] == signature and 'writer_paths' in old and 'provider' in old:
            result[run['id']] = old
            continue
        # File names narrow reading cost only; launch_spec still verifies bytes
        # and the exact original run/request binding.
        narrowed = [r for r in run['input_refs'] if Path(r.get('path', '')).name.startswith('launch')]
        spec = launch_spec(root, {**run, 'input_refs': narrowed or run['input_refs']})
        if not spec and narrowed:
            spec = launch_spec(root, run)
        native = run.get('native_identity') or {}
        native_id = native.get('id') or spec.get('requested_session_id')
        try:
            writer_paths = _writer_paths(root, run['config_ref'], native_id)
            provider = _provider(root,run['config_ref'],'scientific_reviewer' if run['independent'] else run['recipient_role'])
        except (OSError, ValueError):
            writer_paths = []
            provider = None  # Unknown occupancy still consumes a pilot reservation.
        result[run['id']] = {'signature': signature, 'run_id': run['id'], 'request_id': run['request_id'],
            'kind': 'review' if run['independent'] else 'author', 'provider':provider,
            'native_session': native_id, 'writer_paths': writer_paths,
            'execution_path': str(execution.metadata_path(spec['evidence_home'], run['id'])) if spec.get('evidence_home') else None}
    return result


def _writer(path):
    return execution.native_writer(path)


def _observe(root, *, cached=True, save=False):
    """One shared short-lived OS scan. No native query or model invocation."""
    path = _paths(root)[2]
    old = _read(path, {})
    if cached and old.get('schema')==OBSERVATION_SCHEMA and old.get('boot') == execution.boot_identity() and 0 <= time.time() - old.get('at', 0) < OBSERVATION_SECONDS:
        return old
    processes, boot = _processes(), execution.boot_identity()
    by_session, by_run, checked, markers_by_pid = {}, {}, {}, {}
    for _ in range(3):
        for pid, process in processes.items():
            if checked.get(pid) == process:
                continue
            try:
                markers_by_pid[pid] = execution._process_markers(pid)
                checked[pid] = process
            except (OSError, ValueError):
                markers_by_pid.pop(pid, None)
        current = _processes()
        issues = [pid for pid in current if checked.get(pid) != current[pid]]
        processes = current
        if not issues:
            break
    for pid, process in processes.items():
        if checked.get(pid) != process:
            continue
        markers = markers_by_pid[pid]
        session = markers.get('DSH_SESSION_ID')
        for ident in [session, *markers.get('session_arguments', [])]:
            if ident:
                by_session.setdefault(ident, []).append(pid)
        if markers.get('HTAM_DSH_CAPACITY_ROOT') == str(Path(root).resolve()):
            if ident := markers.get('HTAM_DSH_CAPACITY_RUN_ID'):
                by_run.setdefault(ident, []).append(pid)
    catalog = _catalog(root, old.get('catalog', {}))
    occupied, finished = [], []
    for record in catalog.values():
        try:
            meta = _read(Path(record['execution_path']), {}) if record['execution_path'] else {}
        except (OSError, ValueError):
            meta = {}  # A broken legacy receipt alone is not actual execution.
        child, manager = meta.get('child'), meta.get('manager')
        native = record['native_session']
        reason = None
        if _matches(child, processes, boot) is True:
            reason = 'original_native_process'
        elif child and child.get('boot') == boot and any(p['pgid'] == child.get('pgid') for p in processes.values()):
            reason = 'launch_ownership_unverified' if child['pid'] in processes else 'original_process_group'
        elif by_run.get(record['run_id']) or native and by_session.get(native):
            reason = 'original_session_or_run_process'
        elif not child and meta.get('phase') != 'settled' and _matches(manager, processes, boot) is not False:
            reason = 'original_manager_starting' if _matches(manager,processes,boot) is True else 'launch_ownership_unverified'
        else:
            writers=[_writer(path) for path in record.get('writer_paths',[])]
            if True in writers:reason='native_writer_owner_unattributed'
            elif None in writers:reason='native_writer_ownership_unverified'
        if reason:
            occupied.append({**record, 'reason': reason})
        elif child or meta.get('phase') == 'settled':
            finished.append(record['run_id'])
    value = {'schema':OBSERVATION_SCHEMA,'at': time.time(), 'boot': boot, 'occupied': occupied, 'catalog': catalog,
             'processes': list(processes.values()), 'marked_runs': sorted(by_run),
             'finished_runs': finished, 'attribution_issues': issues}
    if save:
        execution.atomic_json(path, value)
    return value


def _pending_review(entry):
    return entry['phase'] in ('waiting_review', 'queued') and bool(entry.get('wait_token'))


def _wait_metadata(queue, entry):
    if entry.get('policy') != queue.get('policy') or entry['kind'] != 'author':
        return None
    try:
        meta = _read(Path(entry['execution_path']), {})
        wait = meta.get('capacity_wait') or {}
        if (meta.get('run_id') == entry['run_id'] and meta.get('request_id') == entry['request_id']
                and meta.get('manager') == entry['manager'] and wait.get('source') == 'controlled_review_result'
                and wait.get('token') == entry['wait_token'] and wait.get('review_id') == entry.get('review_id')):
            return meta
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _controlled_waits(queue, observed):
    """Only a live trusted manager plus its matching queue/lease wait can yield."""
    if not policy_ready(queue):
        return {}
    processes = {p['pid']: p for p in observed['processes']}
    return {e['run_id']: {**e, 'execution_protected': True, 'seat_held': False}
            for e in queue['entries'] if _pending_review(e)
            and _matches(e['manager'], processes, observed['boot']) is True
            and _wait_metadata(queue, e) and execution.matches(e['manager']) is True}


def _exited_review_waits(root, queue, observed):
    """Prove that a wait cannot return to its original execution owner."""
    if not policy_ready(queue) or not observed['boot'] or observed['attribution_issues']:
        return {}
    processes = {p['pid']: p for p in observed['processes']}
    finished = set(observed['finished_runs']) - set(observed['marked_runs'])
    occupied_sessions = {r.get('native_session') for r in observed['occupied']}
    exited = {}
    for entry in queue['entries']:
        run_id, session = entry['run_id'], entry.get('native_session')
        if (not _pending_review(entry) or run_id not in finished or not session or session in occupied_sessions
                or max(entry['queued_at'], entry.get('manager_attached_at', 0)) >= observed['at']
                or _matches(entry['manager'], processes, observed['boot']) is not False):
            continue
        record = observed['catalog'].get(run_id, {})
        if (record.get('execution_path') != entry['execution_path'] or record.get('native_session') != session
                or record.get('request_id') != entry['request_id'] or record.get('kind') != entry['kind']):
            continue
        meta = _wait_metadata(queue, entry)
        if not meta or not all(isinstance(record, dict) and all(record.get(key) for key in ('pid', 'boot', 'start', 'pgid'))
                               for record in (entry['manager'], meta.get('child'))):
            continue
        if execution.matches(entry['manager']) is not False:
            continue
        try:
            run = tasks.read_run(root, run_id)
            native = run.get('native_identity') or {}
            if native != meta.get('native_identity') or native.get('id') != session:
                continue
            # A cached catalog may predate creation of the native writer lock.
            if any(_writer(p) is not False for p in _writer_paths(root, run['config_ref'], session)):
                continue
            fact = execution.execution_observation(entry['execution_path'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if fact['state'] == 'exited':
            exited[run_id] = {'run_id': run_id, 'request_id': entry['request_id'], 'native_session': session,
                              'sequence': entry['sequence'], 'stop_requested': run['stop_requested'],
                              'run_outcome': run['outcome'], 'execution': fact}
    return exited


def _retire_exited_waits(root, queue, actor, waits, *, authority=None):
    # The caller holds the original capacity lock. Keep business and execution
    # receipts intact, including an unknown outcome or an original stop.
    for entry in sorted(queue['entries'], key=lambda e: e['sequence']):
        if entry['run_id'] not in waits:
            continue
        payload = {'id': entry['run_id'], 'phase': 'abandoned', 'kind': entry['kind'],
                   'sequence': entry['sequence'], 'queue_source': str(_paths(root)[0]),
                   'reason': 'review_wait_execution_exited'}
        if authority is not None:
            payload['authority_ref'] = authority
        tasks.mutate(root, 'run-capacity', actor, payload)
        queue['entries'].remove(entry)


def _effective(queue, observed, *, suspended=None, exited_waits=()):
    processes = {p['pid']: p for p in observed['processes']}
    boot, items, stale = observed['boot'], {}, []
    queued = {e['run_id'] for e in queue['entries'] if e['phase'] in ('queued','handoff')}
    if suspended is None:
        suspended = _controlled_waits(queue, observed)
    suspended_sessions={e['native_session'] for e in suspended.values() if e.get('native_session')}
    for row in observed['occupied']:
        if row['run_id'] in suspended or row.get('native_session') in suspended_sessions:continue
        if row['run_id'] in queued and row['reason'] == 'original_manager_starting':
            continue
        key = row['native_session'] or row['run_id']
        items[key] = row
    for entry in queue['entries']:
        owner = _matches(entry['manager'], processes, boot)
        if max(entry['queued_at'],entry.get('manager_attached_at',0)) >= observed['at']:
            owner = True  # This ticket was born after the shared OS snapshot.
        key = entry.get('native_session') or entry['run_id']
        if entry['run_id'] in suspended or entry['phase']=='handoff':continue
        if _pending_review(entry):
            if entry['run_id'] in exited_waits:
                stale.append(entry)
            elif key not in items and not any(row['run_id'] == entry['run_id'] for row in items.values()):
                items[key] = {**entry, 'reason': 'launch_ownership_unverified'}
            continue
        if entry['phase'] == 'queued':
            if owner is False:
                if policy_ready(queue) and current_ticket(entry) and not entry.get('handoff_token'):
                    stale.append(entry)
            continue
        if key in items or any(row['run_id'] == entry['run_id'] for row in items.values()):
            continue
        if entry['run_id'] in observed.get('finished_runs', []):
            stale.append(entry)
            continue
        if owner is not False or entry['run_id'] in observed['marked_runs']:
            items[key] = {**entry, 'reason': 'admitted_launch_reservation'}
        elif observed['attribution_issues'] and entry['manager'].get('boot') == boot:
            items[key] = {**entry, 'reason': 'launch_ownership_unverified'}
        else:
            stale.append(entry)
    rows = [{**r,'occupancy':'unknown_reservation' if r['reason'] in (
        'native_writer_owner_unattributed','native_writer_ownership_unverified',
        'original_native_writer_or_unverified_lock','original_native_writer','launch_ownership_unverified') else
        'launch_reservation' if r['reason'] in ('admitted_launch_reservation','original_manager_starting') else
        'confirmed_execution'} for r in items.values()]
    counts = {kind: sum(r['kind'] == kind for r in rows) for kind in ('author', 'review')}
    counts['total'] = len(rows)
    return rows, counts, stale


def _refresh_identities(queue, observed):
    """Link first-Session reservations to the already observed original run."""
    changed = False
    for entry in queue['entries']:
        native = observed['catalog'].get(entry['run_id'], {}).get('native_session')
        if native and not entry.get('native_session'):
            entry['native_session'] = native
            changed = True
        elif native and entry.get('native_session') != native:
            raise ValueError('Original capacity ticket and native Session binding disagree')
    return changed


def _pilot_counts(active, provider):
    rows=[row for row in active if row.get('provider') in (None,provider)]
    return {**{kind:sum(row['kind']==kind for row in rows) for kind in ('author','review')},'total':len(rows)}


def _waiting_reason(entry, counts, queue, suspended, active=()):
    if not policy_ready(queue):return 'policy_handoff_required'
    if entry['phase']=='handoff':return 'transport_handoff_pending'
    if _pending_review(entry) and entry['run_id'] not in suspended:
        return 'review_reentry_ownership_unverified'
    if queue.get('policy')==PILOT_POLICY:
        if entry.get('admission_policy')!=PILOT_POLICY:
            return 'legacy_manager_cannot_use_pilot'
        if counts['total']>=LIMITS['total']:return 'workspace_total_capacity'
        if counts[entry['kind']]>=LIMITS[entry['kind']]:return entry['kind']+'_capacity'
        provider=queue['pilot_provider']
        is_pilot=entry.get('provider')==provider
        usage=_pilot_counts(active,provider) if is_pilot else counts
        limits=PILOT_LIMITS if is_pilot else LIMITS
        if usage['total']>=limits['total']:return 'provider_total_capacity' if is_pilot else 'workspace_total_capacity'
        if usage[entry['kind']]>=limits[entry['kind']]:return entry['kind']+'_capacity'
        for ahead in queue['entries']:
            if (ahead['sequence']>=entry['sequence'] or ahead['phase']!='queued'
                    or ahead.get('admission_policy')!=PILOT_POLICY
                    or counts[ahead['kind']]>=LIMITS[ahead['kind']]
                    or _pending_review(ahead) and ahead['run_id'] not in suspended):
                continue
            if is_pilot and ahead.get('provider')==provider and usage[ahead['kind']]<limits[ahead['kind']]:
                return 'earlier_eligible_provider_request'
            if not is_pilot and ahead.get('provider')!=provider and ahead['kind']==entry['kind']:
                return 'earlier_same_role_request'
        return None
    if not current_ticket(entry):
        # A live old Python invocation keeps its already-loaded four-seat
        # decision until it returns. Show that real limit, never claim a reload.
        if counts['total']>=4:return 'legacy_runtime_total_capacity'
        if any(e['phase'] in ('queued','handoff') and e['sequence']<entry['sequence'] for e in queue['entries']):
            return 'legacy_runtime_earlier_request'
        return None
    if counts['total']>=LIMITS['total']:return 'workspace_total_capacity'
    if counts[entry['kind']]>=LIMITS[entry['kind']]:return entry['kind']+'_capacity'
    if any(e['phase'] in ('queued','handoff') and e['kind']==entry['kind']
            and e['sequence']<entry['sequence'] for e in queue['entries']):
        return 'earlier_same_role_request'
    # The loaded four-seat loop also waits for every earlier ticket. Let earlier
    # work through and keep later arrivals from continuously refilling its seats.
    if any(not current_ticket(e) and e['phase'] == 'queued' and e['sequence'] < entry['sequence']
           for e in suspended.values()):
        return 'legacy_reentry_draining'
    return None


def snapshot(root):
    """Read-only global query; no queue cleanup or business mutation."""
    root = Path(root).resolve()
    queue = _queue(root)
    try:
        observed = _observe(root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        waiting = [{**e, 'waiting_reason': 'execution_ownership_unavailable'}
                   for e in queue['entries'] if e['phase'] in ('queued','handoff')]
        return {'schema': SCHEMA, 'workspace': str(root), 'limits': LIMITS,
                'counts': {'occupied': None, 'queued': {k: sum(e['kind'] == k for e in waiting) for k in ('author', 'review')}},
                'active': None, 'waiting': waiting, 'observation_status': 'unknown',
                'reason': type(exc).__name__ + ': execution ownership could not be observed; no new admission',
                'queue_source': str(_paths(root)[0])}
    suspended = _controlled_waits(queue, observed)
    exited_waits = _exited_review_waits(root, queue, observed)
    active, counts, stale = _effective(queue, observed, suspended=suspended, exited_waits=exited_waits)
    stale_ids = {e['run_id'] for e in stale}
    pending_queue = {**queue, 'entries': [e for e in queue['entries'] if e['run_id'] not in stale_ids]}
    waiting = [{**e, 'waiting_reason': _waiting_reason(e, counts, pending_queue, suspended, active) or 'awaiting_fifo_admission'}
               for e in pending_queue['entries'] if e['phase'] in ('queued', 'handoff')]
    pilot=queue.get('pilot_provider') if queue.get('policy')==PILOT_POLICY else None
    return {'schema': SCHEMA, 'workspace': str(root), 'limits': LIMITS,
            **({'provider_pilot':{'provider':pilot,'limits':PILOT_LIMITS,
                                 'occupied':_pilot_counts(active,pilot)}} if pilot else {}),
            'policy':queue.get('admission_policy',queue.get('policy')),'protocol_policy':queue.get('policy'),
            'policy_ready':policy_ready(queue),
            'legacy_runtime_tickets':[e for e in queue['entries'] if not current_ticket(e)],
            'waiting_review':list(suspended.values()), 'observed_at_unix': observed['at'],
            'counts': {'occupied': counts, 'queued': {k: sum(e['kind'] == k for e in waiting) for k in ('author', 'review')}},
            'occupancy_counts': {category:{kind:sum(r['kind']==kind and r['occupancy']==category for r in active) for kind in ('author','review')}
                for category in ('confirmed_execution','launch_reservation','unknown_reservation')},
            'active': active, 'waiting': waiting, 'stale_wait_or_reservations': stale,
            'scope': ('The selected provider uses four total seats, at most three per role, and the earliest eligible original ticket. '
                      if pilot else 'Six author and six reviewer seats, twelve total, with FIFO within each role. ') +
                     'Verified review waits yield the model seat. The same invocation takes a new ticket to reenter. '
                     'Execution ownership and original stops remain protected. Devin is outside these pools.',
            'queue_source': str(_paths(root)[0]), 'observation_source': 'original execution metadata and OS process ownership'}


def waiting_run(root, run):
    """Project an existing ticket; observing it never admits or restarts work."""
    if run.get('target_host') != 'deepseek_harness':
        return None  # Other hosts do not depend on this service's queue or quota.
    run_id = run['id']
    if not any(e['run_id'] == run_id and e['phase'] in ('queued','handoff')
               for e in _queue(Path(root).resolve())['entries']):
        return None
    view = snapshot(root)
    for entry in view['waiting']:
        if entry['run_id'] == run_id:
            return {**entry, 'limits': view['limits'], 'counts': view['counts'],
                    'observed_at_unix': view.get('observed_at_unix'),
                    'queue_source': view['queue_source'],
                    'observation_status': view.get('observation_status', 'observed')}
    return None


class Capacity:
    def __init__(self, root, lease, kind, actor, *, handoff=None, provider=None):
        if kind not in ('author','review'):raise ValueError('DSH role must be author or review')
        self.root,self.lease,self.kind,self.actor=Path(root).resolve(),lease,kind,actor
        self.run_id=lease.value['run_id'];self.handoff=handoff
        self.provider=provider
        self.launched=self.enqueued=False

    @property
    def environment(self):
        return {'HTAM_DSH_CAPACITY_ROOT':str(self.root),'HTAM_DSH_CAPACITY_RUN_ID':self.run_id}

    def _event(self,entry,phase):
        tasks.mutate(self.root,'run-capacity',self.actor,{'id':entry['run_id'],'phase':phase,
            'kind':entry['kind'],'sequence':entry['sequence'],'queue_source':str(_paths(self.root)[0])})

    def _remove(self,phase,*,launched=False):
        if not self.enqueued:return
        with _lock(self.root):
            queue=_queue(self.root)
            entry=next((e for e in queue['entries'] if e['run_id']==self.run_id),None)
            if entry is None:return
            if launched:
                try:observed=_observe(self.root,cached=False,save=True)
                except (OSError,ValueError,subprocess.SubprocessError):return
                active,_,_=_effective(queue,observed)
                if any(r['run_id']==self.run_id and r['reason']!='admitted_launch_reservation' for r in active):return
            queue['entries'].remove(entry)
            execution.atomic_json(_paths(self.root)[0],queue);self._event(entry,phase)

    def _await(self,guard):
        while True:
            guard()
            with _lock(self.root,guard):
                queue=_queue(self.root)
                for stopped in list(queue['entries']):
                    if stopped['phase']=='handoff':
                        run=tasks.read_run(self.root,stopped['run_id'])
                        if run['stop_requested'] and run['outcome']=='intent' and run['native_identity'] is None:
                            queue['entries'].remove(stopped);self._event(stopped,'cancelled_before_start')
                            execution.atomic_json(_paths(self.root)[0],queue)
                entry=next((e for e in queue['entries'] if e['run_id']==self.run_id),None)
                if entry is None:raise ValueError('Original FIFO ticket is missing; preserve the original run')
                try:observed=_observe(self.root,save=True)
                except (OSError,ValueError,subprocess.SubprocessError):observed=None
                if observed is not None:
                    changed=_refresh_identities(queue,observed)
                    suspended = _controlled_waits(queue, observed)
                    exited_waits = _exited_review_waits(self.root, queue, observed)
                    active,counts,stale=_effective(queue, observed, suspended=suspended, exited_waits=exited_waits)
                    _retire_exited_waits(self.root, queue, self.actor, exited_waits)
                    for old in stale:
                        if old['run_id'] not in exited_waits:
                            queue['entries'].remove(old);self._event(old,'abandoned' if old['phase']=='queued' else 'released')
                    changed=_finish_protocol_handoff(queue) or changed
                    if _waiting_reason(entry,counts,queue,suspended,active) is None:
                        guard();entry.update(phase='admitted',admitted_at=time.time())
                        execution.atomic_json(_paths(self.root)[0],queue);self._event(entry,'admitted');return
                    if stale or changed:execution.atomic_json(_paths(self.root)[0],queue)
            time.sleep(POLL_SECONDS)

    @contextlib.contextmanager
    def admit(self,guard):
        try:
            with _lock(self.root,guard):
                queue=_queue(self.root)
                entry=next((e for e in queue['entries'] if e['run_id']==self.run_id),None)
                if entry is not None:
                    if (not self.handoff or entry.get('claimed_handoff')!=self.handoff or entry['kind']!=self.kind
                            or entry['manager']!=self.lease.value['manager'] or not current_ticket(entry)):
                        raise ValueError('Original run already owns a capacity ticket; observe without replay')
                    entry.pop('claimed_handoff')
                else:
                    admission_policy=PILOT_POLICY if queue.get('policy')==PILOT_POLICY else POLICY
                    if admission_policy==PILOT_POLICY and not self.provider:
                        raise ValueError('Identify the original provider before entering the pilot queue.')
                    entry={'run_id':self.run_id,'request_id':self.lease.value['request_id'],'kind':self.kind,
                        'sequence':queue['next_sequence'],'phase':'queued','queued_at':time.time(),
                        'manager':self.lease.value['manager'],'execution_path':str(self.lease.path),
                        'native_session':(self.lease.native or {}).get('id'),'policy':queue.get('policy'),
                        'admission_policy':admission_policy,'provider':self.provider}
                    queue['next_sequence']+=1;queue['entries'].append(entry)
                execution.atomic_json(_paths(self.root)[0],queue);self.enqueued=True;self._event(entry,'queued')
            self._await(guard)
            yield self
        except StopRequested as exc:
            if not self.launched:exc.observation={**getattr(exc,'observation',{}),'capacity_phase':'cancelled_before_start','native_started':False}
            raise
        finally:self._remove('released' if self.launched else 'cancelled_before_start',launched=self.launched)

    @contextlib.contextmanager
    def review_wait(self,guard,review_id):
        """Yield only the model seat; original execution/writer ownership stays."""
        guard();token=__import__('uuid').uuid4().hex
        with _lock(self.root,guard):
            queue=_queue(self.root)
            entry=next(e for e in queue['entries'] if e['run_id']==self.run_id)
            if entry['phase']!='admitted' or not current_ticket(entry):
                raise ValueError('Only the original admitted current-policy invocation can yield its seat')
            with self.lease.metadata_lock:
                self.lease.value['capacity_wait']={'token':token,'review_id':review_id,'source':'controlled_review_result'}
                execution.atomic_json(self.lease.path,self.lease.value)
            entry.update(phase='waiting_review',wait_token=token,review_id=review_id,
                native_session=(self.lease.value.get('native_identity') or {}).get('id'))
            execution.atomic_json(_paths(self.root)[0],queue);self._event(entry,'waiting_review')
        try:yield
        finally:
            # The pending tool response cannot reach the model until this same
            # invocation fairly reacquires a seat. Stop never clears/restarts it.
            try:
                guard()
                with _lock(self.root,guard):
                    queue=_queue(self.root);entry=next(e for e in queue['entries'] if e['run_id']==self.run_id)
                    entry.update(phase='queued',sequence=queue['next_sequence'],queued_at=time.time())
                    queue['next_sequence']+=1
                    execution.atomic_json(_paths(self.root)[0],queue);self._event(entry,'queued')
                self._await(guard)
                with self.lease.metadata_lock:
                    self.lease.value.pop('capacity_wait',None);execution.atomic_json(self.lease.path,self.lease.value)
            except StopRequested:raise
            except (OSError,ValueError,KeyError,StopIteration,subprocess.SubprocessError) as exc:
                from agent_base import CapabilityUnavailable
                raise CapabilityUnavailable('Original DSH model seat was not reacquired; advancement withheld: '+type(exc).__name__) from exc
