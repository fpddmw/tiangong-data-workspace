"""Qualified host boundary for a single pending decision of the original sponsor."""
from contextlib import closing
import json

import harness_tasks as tasks
import harness_wave as wave

INTENT = 'role-intake-intent'
BINDING = 'role-intake-binding'
RESULT = 'role-intake-result'
UNSENT = 'role-intake-unstarted'


def _events(c, task_id, intake_id):
    return {row['kind']: json.loads(row['payload_json']) for row in c.execute("""
        SELECT kind,payload_json FROM work_task_events WHERE task_id=?
        AND kind IN (?,?,?,?) AND json_extract(payload_json,'$.intake_id')=?
        ORDER BY id""", (task_id, INTENT, BINDING, RESULT, UNSENT, intake_id))}


def check_reassignment(c, task, matter, identity):
    for row in c.execute("""SELECT payload_json FROM work_task_events WHERE task_id=? AND kind=?
        AND json_extract(payload_json,'$.matter')=? ORDER BY id""", (task['id'],INTENT,matter)):
        previous=json.loads(row[0])
        events=_events(c,task['id'],previous['intake_id'])
        if previous['intake_id']!=identity and not (RESULT in events or UNSENT in events):
            tasks._fail('intake_original_unresolved','Resolve the original intake attempt before replacing its authority.',matter)


def _assignment(c, root, task, matter):
    assignment = wave._selected(c, root, {'task': task['id'], 'object': matter, 'action': 'intake'}, wave.INTAKE)
    if (assignment and assignment['details']['expires_at'] > tasks._now() and wave._valid(root, assignment)
            and tasks.native_key(assignment['recorded_by']) == tasks.native_key(task['sponsor_actor'])):
        return assignment
    return None


def pending(c, root, task):
    if not tasks._exists(c, 'work_decisions'):
        return
    for row in c.execute("""SELECT DISTINCT json_extract(scope_json,'$.object')
        FROM work_decisions WHERE subject=? AND json_extract(scope_json,'$.task')=? AND status='active'""",
                         (wave.INTAKE, task['id'])):
        matter = row[0]
        if matter and (decision := _assignment(c, root, task, matter)):
            events = _events(c,task['id'],decision['id'])
            if RESULT not in events and (INTENT not in events or UNSENT in events):
                yield {'action': 'intake', 'object': matter, 'intake_id': decision['id'],
                       'responsible': json.loads(task['sponsor_actor']),
                       'source_ref': decision['source_ref']}


def authorize_for_host(root,document,host):
    """Bind an already authorized exact matter while qualifying its native host."""
    from task_ledger.decisions import _add
    if document.get('subject')!=wave.INTAKE or not host.qualified_for((document.get('details') or {}).get('successor_host')):
        raise ValueError('Cold intake has no qualified original host for this matter')
    actor=tasks.verified_actor(root)
    with tasks.transaction(root) as c:
        return _add(c,root,document,actor,internal=True)


def offer(root, task_id, matter, host):
    """Offer to the original Session first. Only a qualified host may initiate a successor.

    The host supplies positive lifecycle evidence and a short initiation boundary.
    The intent survives a crash/ambiguous acknowledgement. The same matter is
    recovered through the host's original attempt ID, never by issuing it again.
    """
    actor = tasks.verified_actor(root)
    with closing(tasks._connect(root, True)) as c:
        task = tasks._row(c, 'work_tasks', task_id)
        original = json.loads(task['sponsor_actor'])
        decision = _assignment(c, root, task, matter)
        if task['state']!='active':
            return {'status':'stopped'}
        if not decision:
            ordinary=next((item for item in wave._wave_checks(c,root,task) if item['object']==matter),None)
            return {'status':'original_inbox','original':original,'matter':ordinary} if ordinary else {'status':'no_authorized_matter'}
        events = _events(c, task_id, decision['id'])
    if RESULT in events:
        return {'status': 'handled', **events[RESULT]}
    if INTENT in events:
        if UNSENT in events:
            return {'status':'original_inbox','intake_id':decision['id'],'original':original}
        if BINDING not in events and hasattr(host,'read_attempt'):
            observation=host.read_attempt(events[INTENT]['attempt_id'])
            if observation and observation.get('attempt_id')==events[INTENT]['attempt_id'] and observation.get('successor'):
                with tasks.transaction(root) as c:
                    task=tasks._row(c,'work_tasks',task_id)
                    latest=_events(c,task_id,decision['id'])
                    if BINDING not in latest and UNSENT not in latest:
                        _bind(c,root,task,actor,decision,events[INTENT],original,observation['successor'])
                return {'status':'successor_bound','intake_id':decision['id'],'recovered':True}
        return {'status': 'successor_bound' if BINDING in events else 'reconcile_original_intent',
                **events[INTENT], 'binding': events.get(BINDING)}
    original_observation = host.observe(original)
    if original_observation.get('state') in ('active', 'available'):
        # The original Session pulls this same durable item via its work-show.
        return {'status': 'original_inbox', 'intake_id': decision['id'], 'original': original}
    if (original_observation.get('state') != 'exited' or original_observation.get('actor') != original
            or not original_observation.get('source_ref')):
        return {'status': 'original_liveness_unresolved'}
    if not host.qualified_for(decision['details']['successor_host']):
        return {'status': 'host_unqualified'}
    exit_ref = tasks.evidence_ref(root, original_observation['source_ref'])
    intent = {'intake_id': decision['id'], 'matter': matter, 'exit_ref': exit_ref,
              'attempt_id': 'intake:' + wave._digest([task_id, decision['id']])[:32]}
    with tasks.transaction(root) as c:
        task = tasks._row(c, 'work_tasks', task_id)
        if tasks.native_key(task['sponsor_actor']) != tasks.native_key(original):
            return {'status': 'original_role_changed'}
        current = _assignment(c, root, task, matter)
        if task['state'] != 'active' or not current or current['id'] != decision['id']:
            return {'status': 'stopped' if task['state'] != 'active' else 'authority_changed'}
        if _events(c, task_id, decision['id']):
            return {'status': 'already_claimed'}
        intent['baseline']=json.loads(task['plan_ref_json'])
        tasks._event(c, task, INTENT, actor, intent)
    # This is a separate boundary. A stop after claim and before initiation wins.
    with tasks.transaction(root) as c:
        task = tasks._row(c, 'work_tasks', task_id)
        current = _assignment(c, root, task, matter)
        if task['state'] != 'active' or not current or current['id'] != decision['id']:
            tasks._event(c,task,UNSENT,actor,intent)
            return {'status': 'stopped_before_start', **intent}
        if not host.qualified_for(current['details']['successor_host']):
            tasks._event(c,task,UNSENT,actor,intent)
            return {'status':'host_unqualified',**intent}
        if not host.still_exited(original, original_observation):
            tasks._event(c,task,UNSENT,actor,intent)
            return {'status': 'original_liveness_changed', **intent}
        # start() enqueues one exact attempt and returns its observed binding, or
        # None for an uncertain result. The original intent is already durable.
        successor = host.start(intent, current, original)
        if successor is None:
            return {'status': 'reconcile_original_intent', **intent}
        successor=_bind(c,root,task,actor,current,intent,original,successor)
        return {'status': 'successor_bound', **intent, 'successor': successor}


def _bind(c,root,task,actor,decision,intent,original,successor):
    successor=tasks._actor(root,successor,observed=True)
    if successor['host']!=decision['details']['successor_host'] or tasks.native_key(successor)==tasks.native_key(original):
        raise ValueError('Successor receipt does not identify a separate Session on the authorized host')
    tasks._event(c,task,BINDING,actor,{'intake_id':decision['id'],'attempt_id':intent['attempt_id'],'successor':successor})
    return successor


def admit(c, root, actor, document):
    """Order the actual, exact decision with stops and the single holder."""
    from task_ledger.decisions import covers
    selected = document['scope']
    intake_id = document.get('details', {}).get('intake_id')
    task = tasks._row(c, 'work_tasks', selected.get('task'))
    tasks._active(task)
    matter = document['details'].get('matter')
    assignment = _assignment(c, root, task, matter)
    if not assignment or assignment['id'] != intake_id:
        tasks._fail('intake_authority_changed', 'Use the currently authorized original matter.', intake_id)
    permitted = assignment['details']['decision_scope']
    if document['subject'] != assignment['details']['decision_subject'] or not covers(permitted, selected):
        tasks._fail('intake_scope_mismatch', 'The proposed decision is outside the authorized original matter.', intake_id)
    events = _events(c, task['id'], intake_id)
    if RESULT in events:
        if events[RESULT]['decision_id'] != document['id']:
            tasks._fail('intake_already_handled', 'The original matter already has one recorded decision.', intake_id)
        return assignment
    binding = events.get(BINDING)
    if INTENT in events and UNSENT not in events and binding is None:
        tasks._fail('intake_effect_unknown', 'Read the original native startup receipt before assigning the matter again.', intake_id)
    holder = binding['successor'] if binding else json.loads(task['sponsor_actor'])
    if tasks.native_key(holder) != tasks.native_key(actor):
        tasks._fail('intake_holder_mismatch', 'Only the original confirmed holder may decide this matter.', intake_id)
    return assignment


def finished(c, task_id, actor, assignment, result):
    task = tasks._row(c, 'work_tasks', task_id)
    tasks._event(c, task, RESULT, actor, {'intake_id': assignment['id'], 'decision_id': result['id'],
                                         'source_ref': result['source_ref']})
