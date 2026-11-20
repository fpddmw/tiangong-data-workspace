"""Original review availability and actual full tool-response delivery facts."""
import json
import harness_tasks as tasks
from harness_package import author_lineage
from review_queue import _links, withheld


def projection(c,root,request):
    lineage=author_lineage(c,request)
    placeholders=','.join('?' for _ in lineage)
    delivered={json.loads(row[0])['review_id'] for row in c.execute(
        "SELECT e.payload_json FROM work_run_events e JOIN work_runs r ON r.id=e.run_id WHERE e.kind='review-delivered' AND r.request_id IN ("+placeholders+")",lineage)}
    # A new host retains a new Session. Its existing contract can point at the
    # original packet/opinion; no fabricated continuation identity is needed.
    import harness_scope
    agreement=harness_scope.contract(c,'request',request['id'])
    explicit={r['sha256'] for r in harness_scope.contract_refs((agreement or {}).get('body',{}))}
    reviews=[]
    for raw in c.execute('SELECT * FROM work_requests WHERE independent=1 AND task_id=? ORDER BY created_at,id',(request['task_id'],)):
        linked=json.loads(raw['input_ref_json'])['sha256'] in explicit or any(json.loads(r[0])['sha256'] in explicit for r in c.execute('SELECT result_ref_json FROM work_returns WHERE request_id=?',(raw['id'],)))
        if raw['parent_request_id'] in lineage or raw['id'] in delivered or linked:reviews.append(raw)
    result=[]
    for raw in reviews:
        review=tasks._decode(raw)
        returned=c.execute('SELECT * FROM work_returns WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',(review['id'],)).fetchone()
        run=c.execute('SELECT * FROM work_runs WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',(review['id'],)).fetchone()
        returned=tasks._decode(returned) if returned else None
        replacement=_links(c,review['task_id'],'superseded_review_id',review['id'])
        reserved=withheld(c,review) if not run and not returned else None
        deliveries=[json.loads(e[0]) for e in c.execute("SELECT payload_json FROM work_run_events WHERE kind='review-delivered' AND json_extract(payload_json,'$.review_id')=? ORDER BY id",(review['id'],))]
        result.append({'review_id':review['id'],'parent_request_id':review['parent_request_id'],
            'state':('superseded_before_start' if replacement else 'replacement_withheld' if reserved
                     else 'ready' if returned and (not run or run['outcome']=='succeeded')
                     else run['outcome'] if run else 'not_started'),
            **({'supersession':replacement} if replacement else {}),
            **({'withheld_replacement':reserved} if reserved else {}),
            'return_id':returned['id'] if returned else None,'opinion_ref':returned['result_ref'] if returned else None,
            'submission_ref':review['input_ref'],'run_id':run['id'] if run else None,'deliveries':deliveries,
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
