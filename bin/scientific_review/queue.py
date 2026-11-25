"""An author's explicit replacement of one original, unstarted review."""
import contextlib
import json
from pathlib import Path

import harness_tasks as tasks
import production_review as review

EVENT = 'review-queue-replaced'
WITHHELD = 'review-replacement-withheld'


def _links(c, task, key, ident, *, kind=EVENT):
    row=c.execute('SELECT payload_json FROM work_task_events WHERE task_id=? AND kind=? '
                  'AND json_extract(payload_json,?)=? ORDER BY id DESC LIMIT 1',
                  (task,kind,'$.'+key,ident)).fetchone()
    return json.loads(row[0]) if row else None


def replaced(root, request):
    with contextlib.closing(tasks._connect(root,True)) as c:
        return _links(c,request['task_id'],'superseded_review_id',request['id'])


def withheld(c, request):
    pending=_links(c,request['task_id'],'review_id',request['id'],kind=WITHHELD)
    return pending if pending and not _links(c,request['task_id'],'review_id',request['id']) else None


def guard(c, root, request):
    if _links(c,request['task_id'],'superseded_review_id',request['id']):
        tasks._fail('review_superseded','The original author replaced this unstarted review.',request['id'])
    try:
        packet=review._review_packet(root,request['input_ref'])
    except (ValueError,OSError):
        return  # Existing independent briefs may be original plain text.
    if (isinstance(packet,dict) and packet.get('supersedes_review_id')
            and not _links(c,request['task_id'],'review_id',request['id'])):
        tasks._fail('review_replacement_pending','The original queue replacement has not been accepted.',request['id'])


def subjects(root, packet):
    from scientific_review.delta import packet_bodies
    bodies,_,ambiguous=packet_bodies(root,packet)
    if ambiguous:
        raise ValueError('The supplied packet has conflicting exact candidate bodies.')
    return {key[:2] for key in bodies}


def author_review(root, parent, native, ident, *, allow_lineage=False):
    if not isinstance(ident,str) or not ident:
        raise ValueError('Provide one exact original review ID.')
    original=tasks.read_request(root,ident)
    lineage = [parent['id']]
    authors = []
    if allow_lineage:
        from harness_package import author_lineage
        with contextlib.closing(tasks._connect(root,True)) as c:
            lineage = author_lineage(c,parent)
            authors = tasks.request_authors(c,tasks._row(c,'work_requests',parent['id']))
    if not original['independent'] or original['parent_request_id'] not in lineage or original['task_id']!=parent['task_id']:
        raise ValueError('The original review belongs to another author assignment.')
    packet=review._review_packet(root,original['input_ref'])
    if not isinstance(packet,dict):
        raise ValueError('The exact original review packet is unavailable.')
    owners=authors if original['parent_request_id']!=parent['id'] else packet.get('authors',[])
    if not native or not any(tasks.native_key(a)==tasks.native_key(native) for a in owners if a):
        raise ValueError('Only the original author Session can use this review as its own.')
    return original,packet


def replace(root, parent, native, new, old, actor):
    from dsh_capacity import DSH, DEVIN, _lock, _queue
    # A replacement may race a first launch, before the review has a run host.
    with _lock(root), _lock(root, host=DEVIN), tasks.transaction(root) as c:
        fresh=tasks._decode(tasks._row(c,'work_requests',new['id']))
        packet=review._review_packet(root,fresh['input_ref'])
        if packet.get('supersedes_review_id')!=old['id']:
            raise ValueError('Replacement must retain the exact selected old review.')
        _,previous=author_review(root,parent,native,old['id'])
        selected=subjects(root,packet)  # Verify the whole newly frozen packet first.
        if not selected or selected!=subjects(root,previous):
            raise ValueError('The queued replacement must cover the same scientific objects.')
        before=_links(c,parent['task_id'],'superseded_review_id',old['id'])
        if before:
            if before['review_id']==new['id']:
                return before
            refused={**before,'state':'already_replaced','review_id':new['id'],'existing_replacement_id':before['review_id']}
            if withheld(c,fresh)!=refused:
                tasks._event(c,tasks._row(c,'work_tasks',parent['task_id']),WITHHELD,actor,refused)
            return refused
        runs=[tasks._decode(row) for row in c.execute('SELECT * FROM work_runs WHERE request_id=?',(old['id'],))]
        host = runs[0]['target_host'] if len(runs) == 1 else None
        entries = _queue(root, host)['entries'] if host in (DSH, DEVIN) else []
        tickets = [entry for entry in entries if entry.get('request_id') == old['id']]
        returned=c.execute('SELECT 1 FROM work_returns WHERE request_id=? LIMIT 1',(old['id'],)).fetchone()
        launch=review.program_home(parent,root=root)/'reviews'/old['id']/'launch.json'
        if returned or any(row['outcome'] in ('succeeded','failed','stopped') for row in runs):
            state='terminal'
        elif any(row.get('native_identity') or row['outcome']=='running' for row in runs) or any(
                entry['phase']=='admitted' for entry in tickets):
            state='claimed'
        elif (runs and (len(runs)!=1 or len(tickets)!=1 or tickets[0]['phase']!='queued'
                         or tickets[0]['run_id']!=runs[0]['id']
                         or runs[0]['outcome']!='intent' or runs[0]['stop_requested'])
              or not runs and (tickets or launch.exists())):
            state='ownership_unverified'
        else:
            state='replaced_before_start'
        value={'state':state,'superseded_review_id':old['id'],'review_id':new['id'],
               'original_author':native,'old_run':runs[0]['id'] if len(runs)==1 else None}
        task=tasks._row(c,'work_tasks',parent['task_id'])
        if state!='replaced_before_start':
            if _links(c,parent['task_id'],'review_id',new['id'],kind=WITHHELD)!=value:
                tasks._event(c,task,WITHHELD,actor,value)
            return value
        tasks._admit_scope(c,task)
        if runs:
            tasks.apply(c,root,'run-stop',actor,{'id':runs[0]['id'],'summary':'Original author explicitly replaced this queued review.'})
        tasks._event(c,task,EVENT,actor,value)
        return value
