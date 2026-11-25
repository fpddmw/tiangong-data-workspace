"""Original review availability and actual full tool-response delivery facts."""
import json
from collections import defaultdict
import harness_tasks as tasks
from harness_package import author_lineage
from scientific_review.queue import EVENT, WITHHELD
from scientific_review.rounds import RESERVED


def projection(c,root,request=None,*,task_id=None):
    task_id = request['task_id'] if request else task_id
    reviews = list(c.execute('SELECT * FROM work_requests WHERE independent=1 AND task_id=? ORDER BY created_at,id',(task_id,)))
    if request:
        lineage=author_lineage(c,request)
        placeholders=','.join('?' for _ in lineage)
        delivered={json.loads(row[0])['review_id'] for row in c.execute(
            "SELECT e.payload_json FROM work_run_events e JOIN work_runs r ON r.id=e.run_id WHERE e.kind='review-delivered' AND r.request_id IN ("+placeholders+")",lineage)}
        # The original contract can also carry an explicit cross-Session handoff.
        import harness_scope
        agreement=harness_scope.contract(c,'request',request['id'])
        explicit={r['sha256'] for r in harness_scope.contract_refs((agreement or {}).get('body',{}))}
        linked=[]
        for row in reviews:
            if row['parent_request_id'] in lineage or row['id'] in delivered:
                linked.append(row)
                continue
            if not explicit:
                continue
            refs=[row['input_ref_json'], *(r[0] for r in c.execute(
                'SELECT result_ref_json FROM work_returns WHERE request_id=?',(row['id'],)))]
            if any(json.loads(ref)['sha256'] in explicit for ref in refs):
                linked.append(row)
        reviews=linked
    if not reviews:
        return []
    ids = tasks._json([review['id'] for review in reviews])
    returns = {row['request_id']:row for row in c.execute('''SELECT * FROM work_returns
        WHERE request_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,id''', (ids,))}
    runs = {row['request_id']:row for row in c.execute('''SELECT * FROM work_runs
        WHERE request_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,id''', (ids,))}
    deliveries = defaultdict(list)
    for row in c.execute("""SELECT payload_json FROM work_run_events WHERE kind='review-delivered'
            AND json_extract(payload_json,'$.review_id') IN (SELECT value FROM json_each(?)) ORDER BY id""", (ids,)):
        event=json.loads(row[0]);deliveries[event['review_id']].append(event)
    replacements, replaced, withheld, submissions = {}, {}, {}, {}
    for row in c.execute('SELECT kind,payload_json FROM work_task_events WHERE task_id=? AND kind IN (?,?,?) ORDER BY id',
                         (task_id,EVENT,WITHHELD,RESERVED)):
        event=json.loads(row['payload_json'])
        if row['kind']==EVENT:
            replacements[event['review_id']]=event
            replaced[event['superseded_review_id']]=event
        elif row['kind']==WITHHELD:
            withheld[event['review_id']]=event
        elif event.get('submission_ref'):
            submissions[event['review_id']]=event['submission_ref']
    result=[]
    for raw in reviews:
        review=tasks._decode(raw)
        returned=returns.get(review['id'])
        run=runs.get(review['id'])
        returned=tasks._decode(returned) if returned else None
        replacement=replaced.get(review['id'])
        reserved=withheld.get(review['id']) if not run and not returned and review['id'] not in replacements else None
        result.append({'review_id':review['id'],'parent_request_id':review['parent_request_id'],
            'state':('superseded_before_start' if replacement else 'replacement_withheld' if reserved
                     else 'ready' if returned and (not run or run['outcome']=='succeeded')
                     else run['outcome'] if run else 'not_started'),
            **({'supersession':replacement} if replacement else {}),
            **({'withheld_replacement':reserved} if reserved else {}),
            'return_id':returned['id'] if returned else None,'opinion_ref':returned['result_ref'] if returned else None,
            'submission_ref':submissions.get(review['id'],review['input_ref']),
            'run_id':run['id'] if run else None,'deliveries':deliveries[review['id']],
            'read_command':'review_result({"review_id":'+json.dumps(review['id'])+'})',
            'meaning':'Ready is discoverable. Delivery records a full tool response to the exact Session, not scientific adoption or proof of handling.'})
    return result


def record(c,root,actor,payload):
    run=tasks._row(c,'work_runs',payload['id'])
    parent=tasks._decode(tasks._row(c,'work_requests',run['request_id']))
    review=tasks._row(c,'work_requests',payload.get('review_id'))
    returned=tasks._row(c,'work_returns',payload.get('return_id'))
    if (not review['independent'] or review['task_id']!=parent['task_id']
            or returned['request_id']!=review['id'] or not run['native_identity_json']):
        raise ValueError('Full review delivery must retain the author Session and original review lineage')
    ref=tasks.evidence_ref(root,json.loads(returned['result_ref_json']))
    if payload.get('opinion_ref')!=ref:
        raise ValueError('Delivery must identify the complete original opinion hash')
    receiver=json.loads(run['native_identity_json']);receiver.pop('turn_id',None)
    fact={'review_id':review['id'],'return_id':returned['id'],'opinion_ref':ref,'receiver':receiver,
          'source':'review_result_full_tool_response'}
    encoded=tasks._json(fact)
    if not c.execute("SELECT 1 FROM work_run_events WHERE kind='review-delivered' AND payload_json=?",(encoded,)).fetchone():
        tasks._run_event(c,run,'review-delivered',actor,None,fact)
    return fact
