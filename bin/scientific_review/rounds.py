"""Reservation and original-opinion links for a scientific delivery unit."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re

import harness_tasks as tasks

RESERVED = 'review-round-reserved'
OPINION = 'review-round-opinion'
HELD = 'review-round-held'
EXTRA = 'review-extra-round'
DEFAULT_ROUNDS = 3
ISSUE = re.compile(r'^\s*(?:(?:[-*]|\d+\.)\s+|#{1,6}\s+)?\**([A-Z][0-9]+[a-z]?)\s*(?=[（(:：\x5b*\s])')


def paragraphs(text, ref, *, pointer=None):
    """Mechanical locators into normal prose; meaning stays with its writer."""
    lines = text.splitlines()
    sections = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        issue = ISSUE.match(line)
        if not sections or sections[-1]['line_end'] != number - 1 or issue or line.lstrip().startswith('#'):
            sections.append({'label': issue[1] if issue else None, 'source_ref': ref,
                             'line_start': number, 'line_end': number,
                             **({'pointer': pointer} if pointer else {})})
        else:
            sections[-1]['line_end'] = number
    return sections


def _original_text(root, ref):
    from task_ledger.store import evidence_path
    path = Path(evidence_path(root, ref))
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('The original opinion no longer matches its recorded hash')
    return raw.decode('utf-8-sig')


def prepare_opinion(root, payload):
    req = tasks.read_request(root, payload['request_id'])
    if not req['independent']:
        return None
    try:
        text = _original_text(root, payload['result_ref'])
    except (OSError, ValueError):
        return None
    return {'has_content': bool(text.strip())}


def _events(c, task_id, kind, *, at=None):
    sql = 'SELECT payload_json,created_at,id FROM work_task_events WHERE task_id=? AND kind=?'
    args = (task_id, kind)
    if at is not None:
        sql += ' AND created_at<=?'
        args += (at,)
    return [(json.loads(r[0]), r[1], r[2]) for r in c.execute(sql + ' ORDER BY id', args)]


def completed_return(c, returned, *, at=None):
    """Only the original return and a confirmed terminal run can use a round."""
    if returned['run_id'] is None:
        return True
    return bool(c.execute("""SELECT 1 FROM work_run_events WHERE run_id=? AND kind='run-outcome'
        AND json_extract(payload_json,'$.outcome')='succeeded' AND created_at<=?""",
                          (returned['run_id'], at or tasks._now())).fetchone())


def recorded_assessments(c, task_id, *, at=None):
    return {row['return_id']: row for row, _, _ in _events(c, task_id, 'review-conclusion-indexed', at=at)}


def _status(c, root, task_id, request_id, *, at=None, observed=None, assessments=None):
    from scientific_review.queue import EVENT, WITHHELD
    cutoff = at or tasks._now()
    if observed is None:
        observed = {row['return_id']: row for row, _, _ in _events(c, task_id, OPINION, at=at)}
    if assessments is None:
        assessments = recorded_assessments(c, task_id, at=at)
    replacements = [p for p, _, _ in _events(c, task_id, EVENT, at=at)]
    if any(p['superseded_review_id'] == request_id for p in replacements) or (
            any(p['review_id'] == request_id for p, _, _ in _events(c, task_id, WITHHELD, at=at))
            and not any(p['review_id'] == request_id for p in replacements)):
        return 'released'
    unchecked = complete = False
    for returned in c.execute('SELECT * FROM work_returns WHERE request_id=? AND created_at<=? ORDER BY created_at,id',
                              (request_id, cutoff)):
        if not completed_return(c, returned, at=cutoff):
            continue
        complete = True
        if assessments.get(returned['id'], {}).get('conclusion') == 'not_assessed':
            continue
        saved = observed.get(returned['id'], {})
        has_content = saved.get('has_content')
        if has_content is None:
            try:
                has_content = bool(_original_text(root, json.loads(returned['result_ref_json'])).strip())
            except (OSError, ValueError):
                unchecked = True
        if has_content:
            return 'used'
    if unchecked:
        return 'reserved'
    if at is not None:
        return 'released' if complete else 'unproven'
    run = c.execute('SELECT id,outcome,target_host FROM work_runs WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
                    (request_id,)).fetchone()
    if run and tasks.pending_runs(c, request_id):
        return 'reserved'
    decision = tasks._delegation_decisions(c, task_id).get(request_id, {})
    if decision.get('action') == 'end':
        return 'released'
    if run and run['outcome'] == 'failed' and run['target_host'] == 'deepseek_harness':
        from dsh_auto_resume import CLAIM, DEFERRED, HANDLED, WATCH
        recovery = c.execute('SELECT kind FROM work_run_events WHERE run_id=? AND kind IN (?,?,?,?) ORDER BY id DESC LIMIT 1',
                             (run['id'], CLAIM, DEFERRED, HANDLED, WATCH)).fetchone()
        if recovery and recovery[0] != HANDLED:
            return 'reserved'
    if complete or (run and run['outcome'] in ('failed', 'stopped')):
        return 'released'
    return 'reserved'


def _purpose(c, request):
    from harness_scope import effective_contract
    return (effective_contract(c,request)['body'].get('purpose') or '').strip()


def _packet(root, ref, contract=None):
    from production_review import _review_packet
    try:
        packet = _review_packet(root, ref)
    except (OSError, ValueError):
        packet = None
    if isinstance(packet, dict) and isinstance(packet.get('artifact_refs'), list):
        return packet
    from harness_scope import contract_refs
    return {'artifact_refs': contract_refs(contract or {}), 'review_scope': '', 'legacy_plain_brief': True}


def prepare(root, payload):
    parent = tasks.read_request(root, payload['parent_request_id'])
    packet = _packet(root, payload['input_ref'], payload.get('delivery_contract'))
    frozen = None
    if packet.get('legacy_plain_brief') and payload.get('delivery_contract'):
        from harness_scope import freeze_review_submission
        frozen = freeze_review_submission(root, parent, payload['id'], payload['input_ref'], payload['delivery_contract'])
    from scientific_review.queue import subjects
    objects = sorted('/'.join(key) for key in subjects(root, packet))
    return {'objects': objects, 'replacement': packet.get('supersedes_review_id'),
            'author_request_id': parent['id'],'author_run_id':packet.get('author_run_id'), 'submission_ref': frozen}


def unit_state(c, task_id, parent_id):
    reservations = [p for p, _, _ in _events(c, task_id, RESERVED) if p['lineage_root'] == parent_id]
    observed = {row['return_id']: row for row, _, _ in _events(c, task_id, OPINION)}
    assessments = recorded_assessments(c, task_id)
    root = tasks.workspace_root(c)
    return [(r, _status(c, root, task_id, r['review_id'], observed=observed, assessments=assessments)) for r in reservations]


def reserve(c, root, task, actor, parent, ident, prepared):
    from harness_package import author_lineage
    parent = tasks._decode(parent)
    if prepared is None or prepared['author_request_id']!=parent['id']:
        raise ValueError('Register a new independent review through the original request entry.')
    authors = [task['owner_actor'],task['sponsor_actor'],parent['recipient_actor'],*tasks.request_authors(c,parent)]
    if run_id := prepared['author_run_id']:
        origin=c.execute('SELECT dispatcher_actor FROM work_runs WHERE id=? AND request_id=? AND native_identity_json IS NOT NULL',
                         (run_id,parent['id'])).fetchone()
        if origin:
            authors.append(origin[0])
    if not any(tasks.native_key(value)==tasks.native_key(actor) for value in authors if value):
        tasks._fail('review_authority_required','The original author, responsible lead or sponsor must request independent review for this unit.',parent['id'])
    lineage = author_lineage(c, parent)
    original = tasks._decode(tasks._row(c, 'work_requests', lineage[-1]))
    matching = unit_state(c, task['id'], lineage[-1])
    purpose = matching[0][0]['purpose'] if matching else _purpose(c, original) or original['input_ref']['sha256']
    objects = prepared['objects'] or ['@assignment']
    if objects == ['@assignment'] and matching:
        objects = sorted({key for item, _ in matching for key in item['objects']})
    keys = {key: 'review-unit:' + hashlib.sha256(tasks._json([lineage[-1], purpose, key]).encode()).hexdigest()[:32]
            for key in objects}
    replacements = prepared['replacement']
    if replacements and not any(p['review_id']==replacements and set(p['objects'])==set(objects) for p,_ in matching):
        # Historical unreleased candidates had no autonomous reservation to take.
        replacements = None
    extra = []
    exhausted = {}
    for obj, key in keys.items():
        used_or_reserved = [(p,state) for p,state in matching if state in ('reserved','used') and
                            (obj in p['objects'] or '@assignment' in p['objects'])]
        # During atomic queue replacement, both cannot launch. New remains
        # blocked by the existing replacement guard until the old is withdrawn.
        counted = [(p,state) for p,state in used_or_reserved if not (state=='reserved' and p['review_id']==replacements)]
        if len(counted) < DEFAULT_ROUNDS:
            continue
        grant = available_grant(c, root, task['id'], key, counted)
        if not grant:
            exhausted[obj]=key
        else:
            extra.append(grant['id'])
    if exhausted:
        error=tasks.WorkConflict('review_round_budget',
            'Three autonomous rounds are used or reserved. The task lead can decide further investment for this review.',
            next(iter(exhausted.values())))
        error.review_units=exhausted
        raise error
    tasks._event(c, task, RESERVED, actor, {'review_id': ident, 'lineage_root': lineage[-1], 'purpose': purpose,
                'objects': objects, 'units': keys, 'extra_decisions': sorted(set(extra)),
                'replaces_review_id': replacements, 'scope': parent['required_scope'],
                **({'submission_ref': prepared['submission_ref']} if prepared['submission_ref'] else {})})


def budget_details(c, task, actor, selected, document):
    unit = selected.get('object')
    count=(document.get('details') or {}).get('rounds',1)
    if selected.get('action')!='review' or document['statement'] not in ('add_one_round','add_rounds') or type(count) is not int or count<1:
        raise ValueError('Authorize a positive exact number of additional rounds for this review')
    if not any(unit in value['units'].values() for value, _, _ in _events(c, task['id'], RESERVED)):
        raise ValueError('Use the exact review unit reported by the original review registration')
    return {'rounds':count} if document['statement']=='add_rounds' else {}


def available_grant(c, root, task_id, unit, counted):
    from task_ledger.decisions import decode
    from harness_wave import _valid
    if not tasks._exists(c,'work_decisions'):
        return None
    scope = {'task':task_id,'object':unit,'action':'review'}
    for row in c.execute("SELECT * FROM work_decisions WHERE subject=? AND scope_json=? AND status='active' ORDER BY recorded_at,id",
                         (EXTRA,tasks._json(scope))):
        grant=decode(row)
        used=sum(grant['id'] in p['extra_decisions'] for p,_ in counted)
        if used<grant['details'].get('rounds',1) and _valid(root,grant):
            return grant
    return None


def authorize(root, handle, rounds, reason, object_id=None):
    """Use the original request or review identifier; unit hashes remain internal."""
    from task_ledger.decisions import _add
    from harness_package import author_lineage
    from harness_scope import _owner
    actor=tasks.verified_actor(root)
    if type(rounds) is not int or rounds<1:
        raise ValueError('Use --add with a positive number of additional rounds')
    with tasks.transaction(root) as c:
        request=c.execute('SELECT * FROM work_requests WHERE id=?',(handle,)).fetchone()
        held=set()
        if request is None:
            pending=[json.loads(row[0]) for row in c.execute("SELECT payload_json FROM work_task_events WHERE kind=? AND json_extract(payload_json,'$.review_id')=?",
                                                            (HELD,handle))]
            if pending:
                held={row['unit'] for row in pending}
                request=tasks._row(c,'work_requests',pending[0]['author_request'])
        if request is None:
            raise ValueError('Select an original author request or an exact review ID')
        task=tasks._row(c,'work_tasks',request['task_id'])
        _owner(c,task,actor)
        parent=tasks._row(c,'work_requests',request['parent_request_id']) if request['independent'] else request
        if parent['kind']!='plan':
            raise ValueError('Review investment belongs to the original author request')
        lineage=author_lineage(c,tasks._decode(parent))[-1]
        reservations=[p for p,_,_ in _events(c,task['id'],RESERVED) if p['lineage_root']==lineage]
        if request['independent']:
            reservations=[p for p in reservations if p['review_id']==handle]
        units={obj:key for p in reservations for obj,key in p['units'].items() if not held or key in held}
        if len(units)>1:
            units.pop('@assignment',None)
        if object_id is not None:
            if object_id not in units:
                raise ValueError('Select an exact object from this frozen review unit')
            chosen=object_id
        elif len(units)==1:
            chosen=next(iter(units))
        elif not units:
            raise ValueError('No matching frozen review unit is available for this original request')
        else:
            return {'state':'select_object','request_id':parent['id'],'objects':sorted(units)}
        unit=units[chosen]
        home=Path(json.loads(parent['input_ref_json'])['path']).parent
        source=tasks.save_note(root,home,'review-investment',reason)
        ident='review-investment:'+hashlib.sha256(tasks._json([unit,rounds,source['sha256'],tasks.native_key(actor)]).encode()).hexdigest()
        record=_add(c,root,{'id':ident,'subject':EXTRA,'scope':{'task':task['id'],'object':unit,'action':'review'},
            'statement':'add_rounds','details':{'rounds':rounds},
            'source_ref':{**source,'locator':'file','decided_by':actor['id']}},actor,internal=True)
        return {'id':record['id'],'request_id':parent['id'],'object':chosen,'additional_rounds':rounds,'source_ref':record['source_ref']}


def record_opinion(c, root, task, req, result, prepared):
    reservation = next((p for p,_,_ in _events(c, task['id'], RESERVED) if p['review_id']==req['id']), None)
    if prepared is None or not reservation:
        return
    if any(p['return_id']==result['id'] for p,_,_ in _events(c, task['id'], OPINION)):
        return
    tasks._event(c, task, OPINION, json.loads(result['author_actor']) if isinstance(result['author_actor'], str) else result['author_actor'],
                 {'review_id': req['id'], 'return_id': result['id'], 'opinion_ref': result['result_ref'],
                  **prepared})


def submission_ref(c, request, index=None):
    if index:
        return index['submission_ref']
    row = c.execute("""SELECT json_extract(payload_json,'$.submission_ref') FROM work_task_events
        WHERE task_id=? AND kind=? AND json_extract(payload_json,'$.review_id')=?""",
                    (request['task_id'], RESERVED, request['id'])).fetchone()
    return json.loads(row[0]) if row and row[0] else request['input_ref']


def track(c, root, task_id, review_id, *, at=None, return_id=None, run_id=None):
    req = tasks._decode(tasks._row(c, 'work_requests', review_id))
    reservation = next((p for p,_,_ in _events(c, task_id, RESERVED, at=at) if p['review_id']==review_id), None)
    assessments = recorded_assessments(c, task_id, at=at)
    row = c.execute('''SELECT * FROM work_returns WHERE request_id=? AND created_at<=?
        AND (? IS NULL OR id=?) AND (? IS NULL OR run_id=?) ORDER BY created_at DESC,id DESC LIMIT 1''',
                    (review_id, at or tasks._now(), return_id, return_id, run_id, run_id)).fetchone()
    current = {'conclusions': [], 'sections': []}
    if row:
        ref = json.loads(row['result_ref_json'])
        declared = assessments.get(row['id'])
        current.update(return_id=row['id'], opinion_ref=ref, completed_return=completed_return(c, row, at=at))
        if declared:
            current.update(conclusions=[declared['conclusion']], declared_assessment=declared)
        try:
            current['sections'] = paragraphs(_original_text(root, ref), ref)
        except (OSError, ValueError):
            current['original_unavailable'] = True
    submission = (reservation or {}).get('submission_ref', req['input_ref'])
    packet = _packet(root, submission)
    prior, choices = packet.get('prior_review'), packet.get('prior_review_candidates', [])
    if not (prior or choices or packet.get('legacy_plain_brief')) and req['parent_request_id']:
        from scientific_review.delta import prior_candidates
        parent = tasks._decode(tasks._row(c, 'work_requests', req['parent_request_id']))
        prior, choices = prior_candidates(root, parent, packet, at=req['created_at'], connection=c)
    state = _status(c, root, task_id, review_id, at=at, assessments=assessments)
    handling = paragraphs(packet.get('review_scope', ''), submission, pointer='/review_scope')
    if original := packet.get('original_brief_ref'):
        try:
            handling = paragraphs(_original_text(root, original), original)
        except (OSError, ValueError):
            pass
    return {'review_id': req['id'], 'reservation': reservation,
            'budget_state': state if reservation else 'historical_unmapped', 'original_round_state': state,
            'prior_review': prior, 'prior_review_candidates': choices,
            'author_handling': handling, 'submission_ref': submission,
            'current': current}


def opinion_view(c, root, review, *, return_id=None, run_id=None, at=None, native_text=None, source=None):
    """Full original author-facing readback, also usable against a frozen ledger."""
    tracking = track(c, root, review['task_id'], review['id'], at=at, return_id=return_id, run_id=run_id)
    current = tracking['current']
    if current.get('completed_return'):
        source = current['opinion_ref']
        try:
            native_text = _original_text(root, source)
        except (ValueError, OSError):
            if not isinstance(native_text, str):
                return None
    if not isinstance(native_text, str):
        return None
    submission = tracking['submission_ref']
    packet = _packet(root, submission)
    return {'return_id': current.get('return_id'), 'ref': source, 'submission_ref': submission,
            'opinion': {'text': native_text, 'submission': packet, 'tracking': tracking,
                        'author_handling': tracking['author_handling']}}


def record_held(root, actor, payload, units):
    """Index the existing frozen request once for the lead's budget decision."""
    with tasks.transaction(root) as c:
        task=tasks._row(c,'work_tasks',payload['task_id'])
        recorded={p['unit'] for p,_,_ in _events(c,task['id'],HELD) if p['review_id']==payload['id']}
        for unit in units:
            if unit not in recorded:
                tasks._event(c,task,HELD,tasks.verified_actor(root,actor),
                             {'unit':unit,'review_id':payload['id'],'author_request':payload['parent_request_id'],
                              'submission_ref':payload['input_ref']})


def escalation_items(c, task):
    indexes = _events(c, task['id'], 'review-conclusion-indexed')
    dispositions = list(tasks._effective_dispositions(c, task['id'])) if any(p.get('recurs_after') for p, _, _ in indexes) else []
    handled = {row['return_id'] for row in dispositions}
    if dispositions:
        effective = {row['id'] for row in dispositions if row['disposition'] == 'adopt'}
        handled.update(a['review_return_id'] for p, _, _ in _events(c, task['id'], 'delivery-acceptance-recorded')
                       if p['disposition_id'] in effective for a in p.get('review_applications', []))
    for assessment, stamp, _ in indexes:
        if assessment.get('recurs_after') and assessment['return_id'] not in handled:
            returned = tasks._row(c, 'work_returns', assessment['return_id'])
            yield {'action': 'review_correction', 'object': returned['id'], 'request_id': returned['request_id'],
                   'return_id': returned['id'], 'source_ref': json.loads(returned['result_ref_json']), 'visible_at': stamp,
                   'responsible': json.loads(task['owner_actor']), 'recurs_after': assessment['recurs_after']}
    root=tasks.workspace_root(c)
    booked=[p for p,_,_ in _events(c,task['id'],RESERVED)]
    queued = {p['review_id'] for p in booked}
    seen = set()
    states = {}
    for pending, stamp, _ in reversed(_events(c,task['id'],HELD)):
        unit=pending['unit']
        if unit in seen or pending['review_id'] in queued:
            continue
        lineage = next((p['lineage_root'] for p in booked if unit in p['units'].values()), None)
        if not lineage:
            continue
        if lineage not in states:
            states[lineage] = unit_state(c, task['id'], lineage)
        obj = next(key for p, _ in states[lineage] for key, u in p['units'].items() if u == unit)
        counted=[(p,state) for p,state in states[lineage] if state in ('used','reserved')
                 and (obj in p['objects'] or '@assignment' in p['objects'])]
        if len(counted)<DEFAULT_ROUNDS:
            continue
        if not available_grant(c,root,task['id'],unit,counted):
            seen.add(unit)
            yield {'action':'review_correction','object':unit,'visible_at':stamp,
                   'source_ref':pending['submission_ref'],'request_id':pending['author_request'],'review_id':pending['review_id'],
                   'responsible':json.loads(task['owner_actor']),'reason':'round_budget'}
