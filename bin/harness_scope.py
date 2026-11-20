"""Scope, delivery contracts and acceptance on the original v3 task ledger.

No scheduler, scientific inference or alternative business state database.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import uuid
import harness_tasks as t

ENDPOINTS=('evidence','candidate','saved_draft','submitted','published')
STATES=('unassigned','active','accepted','submitted','dispositioned')
TABLES=('work_scope_items','work_delivery_contracts')
SCHEMA=(
 '''CREATE TABLE IF NOT EXISTS work_scope_items (
 task_id TEXT NOT NULL REFERENCES work_tasks(id), scope_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('requirement','deliverable')), label TEXT NOT NULL,
 source_refs_json TEXT NOT NULL, target_endpoint TEXT CHECK(target_endpoint IN ('evidence','candidate','saved_draft','submitted','published')),
 mapping_verified INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL,
 PRIMARY KEY(task_id,scope_id))''',
 '''CREATE TABLE IF NOT EXISTS work_delivery_contracts (
 subject_kind TEXT NOT NULL CHECK(subject_kind IN ('task','request')), subject_id TEXT NOT NULL,
 revision INTEGER NOT NULL, body_json TEXT NOT NULL, source_ref_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(subject_kind,subject_id))''')


def install_schema(c):
    t.add_schema(c,SCHEMA,TABLES)


def migrate(root):
    """Explicit, repeatable additive migration; backup API before changing schema."""
    from execution_lease import FileLock
    root=Path(root).resolve()
    with FileLock(root,['controlled-autonomy-schema']):
        with contextlib.closing(t._connect(root,True)) as original:
            t.ensure_schema(original)
            if all(t._exists(original,name) for name in TABLES):
                return {'status':'already_installed','backup':None}
            home=root/'.control/backups';home.mkdir(parents=True,exist_ok=True)
            path=home/('controlled-autonomy-'+uuid.uuid4().hex+'.sqlite')
            with contextlib.closing(sqlite3.connect(path)) as backup:
                original.backup(backup)
        with t.transaction(root) as c:
            install_schema(c)
        return {'status':'installed','backup':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}


def _ref(root,ref):
    if isinstance(ref,str):
        from harness_sessions import reference
        return reference(root,ref)
    if isinstance(ref,dict) and 'sha256' not in ref and ref.get('path'):
        from harness_sessions import reference
        return {**ref,**reference(root,ref['path'])}
    if not isinstance(ref,dict):raise ValueError('Provide an original file path or exact source reference.')
    return {**ref,**t.evidence_ref(root,ref)}


def _refs(root,refs):
    if not isinstance(refs,list):raise ValueError('Provide explicit source references.')
    return [_ref(root,ref) for ref in refs]


def _owner(c,task,actor,*,request=None,maintenance=False):
    allowed=[task['owner_actor'],task['sponsor_actor']]
    if request is not None:allowed.append(request['sender_actor'])
    if any(t.native_key(value)==t.native_key(actor) for value in allowed):return
    if maintenance and 'maintainer' in t._roles(c,actor):return
    t._fail('scope_authority_required','Only the original delegator/owner or explicitly appointed maintainer may modify these facts',task['id'])


def contract(c,kind,ident):
    if not t._exists(c,'work_delivery_contracts'):return None
    row=c.execute('SELECT * FROM work_delivery_contracts WHERE subject_kind=? AND subject_id=?',(kind,ident)).fetchone()
    return t._decode(row) if row else None


def endpoint(agreement, scope=None):
    return (scope or {}).get('target_endpoint') or (agreement or {}).get('body', {}).get('endpoint')


def validate_contract(root,body):
    if not isinstance(body,dict) or set(body)-{'purpose','endpoint','scope_boundary','acceptance','authority_refs','interfaces'}:
        raise ValueError('Contract uses purpose, endpoint, scope_boundary, acceptance, authority_refs and interfaces.')
    body=json.loads(json.dumps(body))
    if body.get('endpoint') is not None and body['endpoint'] not in ENDPOINTS:
        raise ValueError('Use endpoint evidence|candidate|saved_draft|submitted|published.')
    for field in ('purpose',):
        if field in body and not isinstance(body[field],str):raise ValueError(field+' must be text.')
    for field,fields in [('scope_boundary',{'in_scope','out_of_scope'}),('acceptance',{'text','refs'}),('interfaces',{'text','refs'})]:
        if field not in body:continue
        if not isinstance(body[field],dict) or set(body[field])-fields:raise ValueError('Invalid '+field+' fields.')
        for key,value in body[field].items():
            if key=='refs':body[field]['refs']=_refs(root,value)
            elif not isinstance(value,str):raise ValueError(field+'.'+key+' must be text.')
    if 'authority_refs' in body:body['authority_refs']=_refs(root,body['authority_refs'])
    return body


def contract_refs(body):
    return body.get('authority_refs',[])+body.get('acceptance',{}).get('refs',[])+body.get('interfaces',{}).get('refs',[])


def set_contract(c,root,kind,ident,body,source,actor,*,expected_revision=None):
    subject=t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
    task=subject if kind=='task' else t._row(c,'work_tasks',subject['task_id'])
    _owner(c,task,actor,request=subject if kind=='request' else None,maintenance=True)
    if expected_revision is not None:t._revision(task,{'expected_task_revision':expected_revision})
    body=validate_contract(root,body);source=_ref(root,source)
    prior=contract(c,kind,ident)
    if prior and prior['body']==body and prior['source_ref']==source:return prior
    task=t._touch(c,task)
    c.execute('''INSERT INTO work_delivery_contracts VALUES (?,?,?,?,?,?)
        ON CONFLICT(subject_kind,subject_id) DO UPDATE SET revision=excluded.revision,body_json=excluded.body_json,
        source_ref_json=excluded.source_ref_json,updated_at=excluded.updated_at''',
        (kind,ident,task['revision'],t._json(body),t._json(source),t._now()))
    result=contract(c,kind,ident)
    t._event(c,task,'contract-updated',actor,{'before':prior,'after':result,'source_ref':source})
    return result


def read_contract(root,kind,ident):
    with contextlib.closing(t._connect(root,True)) as c:
        t.ensure_schema(c);t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
        return {'subject':{'kind':kind,'id':ident},'contract':contract(c,kind,ident)}


def save_contract(root,actor,kind,ident,path,source_path):
    from harness_sessions import reference
    body=json.loads(Path(path).read_bytes());source=reference(root,source_path)
    with contextlib.closing(t._connect(root,True)) as c:
        subject=t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
        task=subject if kind=='task' else t._row(c,'work_tasks',subject['task_id'])
        expected=task['revision']
    migrate(root)
    with t.transaction(root) as c:
        return set_contract(c,root,kind,ident,body,source,t.verified_actor(root,actor),expected_revision=expected)


def _scope_input(root,value):
    if not isinstance(value,dict) or set(value)-{'items','links','verified_scope_ids','source_ref'}:
        raise ValueError('Scope input uses items, links, verified_scope_ids and source_ref.')
    source=_ref(root,value.get('source_ref'))
    items=value.get('items',[]);links=value.get('links',[])
    if not isinstance(items,list) or not isinstance(links,list):raise ValueError('Scope items and links must be arrays.')
    ids=[]
    for item in items:
        if not isinstance(item,dict) or set(item)-{'scope_id','kind','label','source_refs','target_endpoint'}:raise ValueError('Invalid scope item.')
        ids.append(t._text(item.get('scope_id'),'scope_id'))
        t._text(item.get('label'),'Scope label')
        if item.get('kind') not in ('requirement','deliverable'):raise ValueError('Scope kind is requirement or deliverable.')
        if item.get('target_endpoint') is not None and item['target_endpoint'] not in ENDPOINTS:raise ValueError('Invalid scope endpoint.')
        if not item.get('source_refs'):raise ValueError('Every scope item needs its exact original sources.')
        item['source_refs']=_refs(root,item['source_refs'])
    t._list(ids,'Scope IDs')
    verified=t._list(value.get('verified_scope_ids',[]),'Verified scope IDs')
    for link in links:
        if not isinstance(link,dict) or set(link)!={'request_id','scope_ids'}:raise ValueError('Link uses request_id and scope_ids.')
        t._text(link['request_id'],'Original request ID');t._list(link['scope_ids'],'Linked scope IDs')
    return items,links,verified,source


def apply_scope(root,task_id,value,actor,*,dry_run=False):
    value=json.loads(json.dumps(value))
    items,links,verified,source=_scope_input(root,value)
    value['source_ref']=source
    actor=t.verified_actor(root,actor)
    with contextlib.closing(t._connect(root,True)) as c:
        task=t._row(c,'work_tasks',task_id);_owner(c,task,actor,maintenance=True)
        revision=task['revision']
        before=_scope_rows(c,task)
        requests={link['request_id']:t._row(c,'work_requests',link['request_id']) for link in links}
        if any(req['task_id']!=task_id for req in requests.values()):raise ValueError('Scope link crosses original tasks.')
        existing={row['scope_id']:row for row in before if not row.get('legacy_unindexed')}
        for item in items:
            prior=existing.get(item['scope_id'],{})
            existing[item['scope_id']]={**prior,**item,'mapping_verified':bool(prior.get('mapping_verified',False))}
        if not set(verified).issubset(existing):raise ValueError('Verified IDs must exist in the explicit scope.')
        for link in links:
            if not set(link['scope_ids']).issubset(existing):raise ValueError('A request link names scope outside the task.')
        preview={'task_id':task_id,'source_revision':revision,'items':list(existing.values()),'links':links,
                 'verified_scope_ids':verified,'source_ref':source,
                 'legacy_scope_history':json.loads(task['required_scope_json']) if not any(not r.get('legacy_unindexed') for r in before) else [],
                 'counts':{kind:sum(item['kind']==kind for item in existing.values()) for kind in ('requirement','deliverable')}}
    if dry_run:return {'status':'dry_run',**preview}
    migration=migrate(root)
    with t.transaction(root) as c:
        task=t._row(c,'work_tasks',task_id);t._revision(task,{'expected_task_revision':revision})
        _owner(c,task,actor,maintenance=True)
        _scope_input(root,value)  # source bytes rechecked after acquiring the write transaction
        changes=[]
        for item in items:
            old=c.execute('SELECT * FROM work_scope_items WHERE task_id=? AND scope_id=?',(task_id,item['scope_id'])).fetchone()
            fields={'kind':item['kind'],'label':item['label'],'source_refs_json':t._json(_refs(root,item['source_refs'])),
                    'target_endpoint':item.get('target_endpoint',old['target_endpoint'] if old else None)}
            if old is not None and all(old[k]==v for k,v in fields.items()):continue
            c.execute('''INSERT INTO work_scope_items VALUES (?,?,?,?,?,?,?,?)
              ON CONFLICT(task_id,scope_id) DO UPDATE SET kind=excluded.kind,label=excluded.label,
              source_refs_json=excluded.source_refs_json,target_endpoint=excluded.target_endpoint,revision=excluded.revision''',
              (task_id,item['scope_id'],fields['kind'],fields['label'],fields['source_refs_json'],fields['target_endpoint'],
               old['mapping_verified'] if old else 0,revision+1))
            changes.append({'before':dict(old) if old else None,'after':item})
        for sid in verified:
            row=c.execute('SELECT * FROM work_scope_items WHERE task_id=? AND scope_id=?',(task_id,sid)).fetchone()
            if not row['mapping_verified']:
                c.execute('UPDATE work_scope_items SET mapping_verified=1,revision=? WHERE task_id=? AND scope_id=?',(revision+1,task_id,sid))
                changes.append({'scope_id':sid,'mapping_verified':True})
        for link in links:
            row=t._row(c,'work_requests',link['request_id'])
            if json.loads(row['required_scope_json'])!=link['scope_ids']:
                c.execute('UPDATE work_requests SET required_scope_json=? WHERE id=?',(t._json(link['scope_ids']),row['id']))
                changes.append({'request_id':row['id'],'before_scope':json.loads(row['required_scope_json']),'scope_ids':link['scope_ids']})
        # First explicit typed import replaces unindexed legacy management labels;
        # the original list remains verbatim in this ledger event. Typed rows are never deleted.
        current=[r[0] for r in c.execute('SELECT scope_id FROM work_scope_items WHERE task_id=? ORDER BY scope_id',(task_id,))]
        if json.loads(task['required_scope_json'])!=current:
            changes.append({'previous_required_scope':json.loads(task['required_scope_json']),'required_scope':current})
        if changes:
            task=t._touch(c,task,required_scope_json=t._json(current))
            t._event(c,task,'scope-updated',actor,{'changes':changes,'source_ref':source,'before':before})
        _scope_input(root,value)
    return {'status':'applied' if changes else 'unchanged','migration':migration,**preview,'revision':task['revision']}


def _scope_rows(c,task):
    rows=[t._decode(r) for r in c.execute('SELECT * FROM work_scope_items WHERE task_id=? ORDER BY scope_id',(task['id'],))] if t._exists(c,'work_scope_items') else []
    known={r['scope_id'] for r in rows}
    rows.extend({'task_id':task['id'],'scope_id':sid,'kind':None,'label':sid,'source_refs':[json.loads(task['original_ref_json'])],
                 'target_endpoint':None,'mapping_verified':False,'legacy_unindexed':True,'revision':0}
                for sid in json.loads(task['required_scope_json']) if sid not in known)
    return rows


def _events(c,task_id,kind):
    return [t._decode(r) for r in c.execute('SELECT * FROM work_task_events WHERE task_id=? AND kind=? ORDER BY id',(task_id,kind))]


def projection(c,root,task_id):
    """One all-scope read-only projection, reused by CLI, DSH and context hooks."""
    task=t._row(c,'work_tasks',task_id)
    parent=contract(c,'task',task_id)
    requests=[t._decode(r) for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id',(task_id,))]
    returned=_returns(c,task_id)
    runs=[t._run_view(c,r) for r in c.execute('SELECT r.* FROM work_runs r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY r.created_at,r.id',(task_id,))]
    latest={}
    for run in runs:latest[run['request_id']]=run
    ended=t._delegation_decisions(c,task_id)
    decisions=[t._decode(r) for r in t._effective_dispositions(c,task_id)]
    acceptance={e['payload']['disposition_id']:e['payload'] for e in _events(c,task_id,'delivery-acceptance-recorded')}
    items=[];filters={k:[] for k in ('unassigned','no_explicit_coverage','coverage_pending_adoption','execution_ended_incomplete','coverage_unindexed','mapping_unverified','execution_unknown','stopped_or_paused','never_started')}
    dispositioned={}
    if t._exists(c,'work_decisions'):
        import harness_decisions
        for row in _scope_rows(c,task):
            resolved=harness_decisions.resolve(root,{'task':task_id,'object':row['scope_id']},connection=c,subject='scope-disposition',verify_sources=False)
            for subject in resolved['subjects']:
                if subject['status']!='applicable':continue
                for d in subject['decisions']:
                    if (t.native_key(d['recorded_by'])==t.native_key(task['sponsor_actor'])
                        and d['scope'].get('action') in ('cancel','replace','reduce')):
                        dispositioned[row['scope_id']]=d
    for row in _scope_rows(c,task):
        sid=row['scope_id'];verified=bool(row['mapping_verified'])
        linked=[q for q in requests if sid in q['required_scope'] and not q['independent'] and q['kind']=='plan']
        responsibility=[q for q in linked if ended.get(q['id'],{}).get('action')!='end']
        ids={q['id'] for q in responsibility}
        all_related=[r for r in returned if r['request_id'] in ids]
        coverage=[r for r in returned if sid in r['coverage']]
        candidate_decisions=[d for d in decisions if sid in d['scope']]
        finals=_final_adoptions(candidate_decisions,acceptance)
        required_endpoint=endpoint(parent,row)
        final=finals[-1] if finals else None
        reached=acceptance[final['id']].get('endpoint_by_scope',{}).get(sid) if final else None
        accepted=bool(required_endpoint and reached==required_endpoint)
        state=None if not verified else 'dispositioned' if sid in dispositioned else ('submitted' if accepted and acceptance[final['id']].get('submitted_scope_ids',[]).__contains__(sid) else 'accepted') if accepted else 'active' if responsibility else 'unassigned'
        run_ids=ids or {q['id'] for q in linked}
        related_ids=run_ids|{q['id'] for q in requests if q['independent'] and q['parent_request_id'] in run_ids}
        execution=[{'run_id':r['id'],'request_id':r['request_id'],'native_identity':r['native_identity'],
            'state':r['outcome'],'stop_requested':bool(r['stop_requested']),'observed_at':r['updated_at'],
            'execution_observation':r.get('execution_observation'),'native_result_state':r['outcome'],
            'data_effect_state':'Read exact native operation evidence; not inferred from transport or OS exit.',
            'source_ref':r['latest_receipt'],'meaning':'承接调用的最新已观察事实；不证明每条需求正在制作'}
            for ident,r in latest.items() if ident in related_ids]
        remaining=state not in ('accepted','submitted','dispositioned')
        reasons=[]
        if not verified:reasons.append('待核关联')
        if not required_endpoint:reasons.append('终点待核')
        if remaining:
            reasons.append('已采用保存草稿，父范围仍待正式送审' if reached=='saved_draft' and required_endpoint=='submitted'
                           else '尚无满足当前终点的最终采用')
        item={**row,'mapping_verified':verified,'state':state,'endpoint':required_endpoint,'accepted_endpoint':reached,'remaining':remaining,
            'responsibility':[{'request_id':q['id'],'owner':q['recipient_actor'],'brief_ref':q['input_ref']} for q in responsibility],
            'coverage_returns':[{'return_id':r['id'],'result_ref':r['result_ref'],'artifacts':r['artifact_refs']} for r in coverage],
            'coverage_unindexed':[{'return_id':r['id'],'result_ref':r['result_ref']} for r in all_related if not r['coverage']],
            'adoptions':[{'disposition':d,'acceptance':acceptance.get(d['id'],{'acceptance':None,'meaning':'验收层级待核'})} for d in candidate_decisions],
            'execution':execution,'remaining_reasons':reasons,
            'disposition':dispositioned.get(sid),'detail_command':'python3 bin/harness.py work-scope --task '+shlex.quote(task_id)}
        items.append(item)
        if not remaining:continue
        if not verified:filters['mapping_unverified'].append(sid)
        if verified and not responsibility:filters['unassigned'].append(sid)
        if not coverage:filters['no_explicit_coverage'].append(sid)
        if coverage:filters['coverage_pending_adoption'].append(sid)
        if item['coverage_unindexed']:filters['coverage_unindexed'].append(sid)
        if any(r['stop_requested'] or r['state']=='stopped' for r in execution) or task['state'] in ('paused','cancelled'):
            filters['stopped_or_paused'].append(sid)
        elif not execution:filters['never_started'].append(sid)
        elif any(r['state'] in ('unknown','intent') and (r['execution_observation'] or {}).get('state')!='exited' for r in execution):filters['execution_unknown'].append(sid)
        elif all(r['state'] in ('succeeded','failed') or (r['execution_observation'] or {}).get('state')=='exited' for r in execution):filters['execution_ended_incomplete'].append(sid)
    counts={kind:{'total':sum(r['kind']==kind for r in items),**{state:sum(r['kind']==kind and r['state']==state for r in items) for state in STATES},
                  'unverified':sum(r['kind']==kind and r['state'] is None for r in items),
                  'remaining':sum(r['kind']==kind and r['remaining'] for r in items)} for kind in ('requirement','deliverable',None) if any(r['kind']==kind for r in items)}
    return {'task_id':task_id,'task_state':task['state'],'contract':parent,'scope_items':items,
        'counts':{str(k) if k is not None else 'unclassified':v for k,v in counts.items()},'total':len(items),'shown':len(items),
        'remaining_scope':[r['scope_id'] for r in items if r['remaining']],
        'remaining_count':sum(r['remaining'] for r in items),'filters':{k:{'total':len(ids),'scope_ids':ids} for k,ids in filters.items()},
        'filter_note':'筛选可重叠，不能相加；无明确覆盖不等于从未工作。已承担不表示仍运行。',
        'endpoint_authority':'声明终点不授予远端操作权限；运行状态是最新已观察事实。'}


def read_scope(root,task_id):
    with contextlib.closing(t._connect(root,True)) as c:
        c.execute('BEGIN');t.ensure_schema(c)
        return projection(c,root,task_id)

# Boundary checks consume original, complete evidence; no external action runs here.
CHECK_SCHEMA='data-workspace.delivery-check.v1'
CONCLUSIONS=('supported','supported_with_limits','material_issue','not_assessed')


def _returns(c,task_id):
    rows=[t._decode(r) for r in c.execute('SELECT r.* FROM work_returns r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY r.created_at,r.id',(task_id,))]
    by_id={r['id']:r for r in rows}
    for e in _events(c,task_id,'return-observation'):
        p=e['payload'];r=by_id.get(p.get('return_id'))
        if r:
            for key in ('coverage','artifact_refs','remaining_scope'):
                if key in p:r[key]=p[key]
    return rows


def _final_adoptions(decisions,acceptance):
    current={}
    for decision in decisions:
        if decision['disposition']!='adopt':current.pop(decision['return_id'],None)
        elif acceptance.get(decision['id'],{}).get('acceptance')=='final':current[decision['return_id']]=decision
    return list(current.values())


class Evidence:
    def __init__(self,root):
        self.root=Path(root);self.sources={};self.documents={};self.issues=[]
    def read(self,ref):
        if not isinstance(ref,dict):
            self.issues.append({'ref':ref,'reason':'Missing exact source reference'});return None
        key=t._json(ref)
        if key in self.documents:return self.documents[key]
        row={'ref':ref,'status':'unknown'}
        try:
            path=Path(t.evidence_ref(self.root,ref)['path']);raw=path.read_bytes()
            actual=hashlib.sha256(raw).hexdigest()
            if actual!=ref['sha256']:raise ValueError('Original source changed during read')
            row.update(status='pass',actual_sha256=actual,bytes=len(raw))
            try:value=json.loads(raw)
            except (ValueError,UnicodeError):value=None
        except (ValueError,OSError,KeyError,TypeError) as exc:
            value=None;row['detail']=str(exc)
            try:row['actual_sha256']=hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest()
            except (OSError,KeyError,TypeError):pass
            self.issues.append({'ref':ref,'reason':str(exc)})
        self.sources[key]=row;self.documents[key]=value
        return value
    def local(self,path,expected=None):
        path=Path(path)
        try:
            raw=path.read_bytes();actual=hashlib.sha256(raw).hexdigest()
            ref={'path':str(path),'sha256':expected or actual};return ref,self.read(ref)
        except OSError:
            ref={'path':str(path),'sha256':expected or '0'*64};return ref,self.read(ref)
    def intact(self,ref):
        self.read(ref);return self.sources.get(t._json(ref),{}).get('status')=='pass'


def _check(ident,status,detail,refs=()):
    return {'id':ident,'status':status,'source_refs':list(refs),'detail':detail}


def _complete_envelope(body):
    from delivery_evidence import complete_envelope
    return complete_envelope(body)


def _body_identity(body):
    from delivery_evidence import body_identity
    return body_identity(body)


def _contract_for_request(c,request):
    own=contract(c,'request',request['id'])
    return own,contract(c,'task',request['task_id'])


def _contract_checks(evidence,body,source,prefix):
    checks=[]
    for name,value in [('purpose',body.get('purpose')),('endpoint',body.get('endpoint')),
                       ('scope_boundary.in_scope',body.get('scope_boundary',{}).get('in_scope')),
                       ('scope_boundary.out_of_scope',body.get('scope_boundary',{}).get('out_of_scope')),
                       ('acceptance.text',body.get('acceptance',{}).get('text'))]:
        okay=isinstance(value,str) and bool(value.strip()) and (name!='endpoint' or value in ENDPOINTS)
        checks.append(_check(prefix+name,'pass' if okay else 'fail','Information present; scientific adequacy remains with delegator/author.' if okay else 'Missing '+name,[source] if source else []))
    for ref in ([source] if source else [])+contract_refs(body):evidence.read(ref)
    return checks


def review_record(root,actor,return_id,conclusion,basis_path,*,supersedes=()):
    from harness_sessions import reference
    if conclusion not in CONCLUSIONS:raise ValueError('Use supported|supported_with_limits|material_issue|not_assessed.')
    basis=reference(root,basis_path)
    with t.transaction(root) as c:
        returned=t._row(c,'work_returns',return_id);request=t._row(c,'work_requests',returned['request_id'])
        task=t._row(c,'work_tasks',request['task_id'])
        if not request['independent']:raise ValueError('Select an original independent review return.')
        if (t.native_key(actor) not in (t.native_key(returned['author_actor']),t.native_key(task['owner_actor']),t.native_key(task['sponsor_actor']))
            and not t.participates(c,task,actor)):
            t._fail('review_index_authority_required','Index the original opinion from its original task responsibility',return_id)
        # The reader explicitly indexes a stated conclusion; there is no prose classifier.
        opinion=t.evidence_ref(root,json.loads(returned['result_ref_json']))
        submission=t.evidence_ref(root,json.loads(request['input_ref_json']))
        history=[e for e in _events(c,task['id'],'review-conclusion-indexed') if e['payload']['return_id']==return_id]
        supersedes=list(dict.fromkeys(supersedes))
        if history:
            prior=history[-1]['payload']
            if prior['conclusion']!=conclusion or prior['basis_ref']!=basis:
                raise ValueError('This original opinion is already indexed differently; retain it and record a new substantive opinion.')
            if not supersedes or set(supersedes).issubset(prior.get('supersedes_return_ids',[])):
                return {**prior,'reviewer':json.loads(returned['author_actor']),'opinion_ref':opinion}
            supersedes=list(dict.fromkeys([*prior.get('supersedes_return_ids',[]),*supersedes]))
            submission=t.evidence_ref(root,prior['submission_ref'])
        try:packet=json.loads(Path(submission['path']).read_bytes())
        except ValueError:packet=None
        if not isinstance(packet,dict) or not packet.get('artifact_refs'):
            # Plain public review briefs stay plain. The one existing contract
            # can explicitly name canonical candidates; index those exact bytes
            # once alongside the original opinion, without waking its reviewer.
            agreement=(contract(c,'request',request['id']) or {}).get('body',{})
            candidates=[];other=[]
            for ref in contract_refs(agreement):
                exact=t.evidence_ref(root,ref)
                try:body=json.loads(Path(exact['path']).read_bytes())
                except ValueError:body=None
                identity=_body_identity(body)
                if identity:candidates.append({'id':identity[0]+'/'+identity[1],'version':identity[2],**exact})
                else:other.append(exact)
            if candidates:
                from production_review import program_home,_fix_artifact,_save_fixed
                parent=t._decode(t._row(c,'work_requests',request['parent_request_id']))
                directory=program_home(parent,root=root)/'reviews'/request['id']
                fixed=[_fix_artifact(root,directory,ref) for ref in candidates]
                packet={'schema':'data-workspace.review-submission-index.v1','original_brief_ref':submission,
                    'review_scope':agreement.get('purpose',''),'artifact_refs':fixed,
                    'evidence_refs':[submission,*fixed,*other]}
                raw=(t._json(packet)+'\n').encode()
                submission=_save_fixed(root,directory/('submission-index-'+hashlib.sha256(raw).hexdigest()+'.json'),raw)
        if supersedes:
            # Supporting reports/scripts remain full evidence, but only
            # canonical dataset bodies define the scientific target identities.
            reader=Evidence(root)
            def target_ids(value):
                identities=set()
                for ref in (value.get('artifact_refs',[]) if isinstance(value,dict) else []):
                    body=reader.read(ref)
                    if not reader.intact(ref):raise ValueError('Original complete review artifact is unavailable or changed')
                    identity=_body_identity(body)
                    if identity:identities.add(identity)
                return identities
            current_ids=target_ids(packet)
            from harness_package import author_lineage
            lineage=author_lineage(c,t._decode(t._row(c,'work_requests',request['parent_request_id'])))
            for previous_id in supersedes:
                previous=t._row(c,'work_returns',previous_id);old=t._row(c,'work_requests',previous['request_id'])
                if (previous_id==return_id or old['task_id']!=task['id'] or not old['independent']
                        or old['parent_request_id'] not in lineage or previous['created_at']>=returned['created_at']):
                    raise ValueError('Supersession retains the same original author responsibility and earlier independent return')
                old_history=[e for e in _events(c,task['id'],'review-conclusion-indexed') if e['payload']['return_id']==previous_id]
                old_ref=t.evidence_ref(root,old_history[-1]['payload']['submission_ref'] if old_history else json.loads(old['input_ref_json']))
                old_packet=reader.read(old_ref)
                old_ids=target_ids(old_packet)
                if not old_ids or not old_ids.issubset(current_ids):
                    raise ValueError('The replacing complete review must explicitly cover every prior target identity')
        p={'return_id':return_id,'conclusion':conclusion,'basis_ref':basis,'submission_ref':submission,
           'supersedes_return_ids':supersedes}
        t._event(c,task,'review-conclusion-indexed',actor,p)
        return {**p,'reviewer':json.loads(returned['author_actor']),'opinion_ref':opinion}


def _scientific_use(request,agreement):
    # Endpoint, account authority and technical revisions do not change a use.
    return hashlib.sha256(t._json({key:agreement.get(key) for key in
        ('purpose','scope_boundary','acceptance','interfaces')}).encode()).hexdigest()


def review_applications(c,root,task,request,returned,actor,review_ids,handling_refs):
    """Bind the lead's explicit handling/use judgment, never rewrite a review."""
    from delivery_evidence import EvidenceSet,body_identity
    from production_review import program_home,_fix_artifact
    _owner(c,task,actor)
    if not handling_refs:raise ValueError('Cite the complete original author handling with --handling')
    reader=Evidence(root)
    refs=[t.evidence_ref(root,r) for r in handling_refs]
    for ref in [returned['result_ref'],*refs]:
        if not reader.intact(ref):raise ValueError('Original author report/handling is unavailable or changed')
    own,parent=_contract_for_request(c,request);agreement=(own or parent or {}).get('body',{})
    pool=EvidenceSet(c,root,request,returned,reader)
    if not pool.artifacts or pool.selection_missing:raise ValueError('Associate the exact current canonical candidates before accepting applicability')
    indexes={e['payload']['return_id']:e['payload'] for e in _events(c,task['id'],'review-conclusion-indexed')}
    applications=[]
    for review_id in dict.fromkeys(review_ids):
        review=t._decode(t._row(c,'work_returns',review_id));q=t._decode(t._row(c,'work_requests',review['request_id']))
        index=indexes.get(review_id)
        if not q['independent'] or q['task_id']!=task['id'] or q['parent_request_id'] not in pool.lineage:
            raise ValueError('Apply only an independent opinion from this original author responsibility')
        if t.native_key(review['author_actor'])==t.native_key(returned['author_actor']):
            raise ValueError('An author Session cannot certify its own work as independent review')
        if not index or index['conclusion']=='not_assessed':
            raise ValueError('Index an actually assessed original independent conclusion first; not_assessed cannot certify this use')
        if review.get('run_id') and t._row(c,'work_runs',review['run_id'])['outcome']!='succeeded':
            raise ValueError('Read the original successful independent return before accepting applicability')
        packet=reader.read(index['submission_ref'])
        if not isinstance(packet,dict):raise ValueError('Read the original indexed complete review submission first')
        frozen = packet.get('artifact_refs', [])
        if not frozen or not all(reader.intact(a) for a in frozen):
            raise ValueError('Original frozen review artifacts must remain intact')
        targets = {body_identity(reader.read(a)) for a in frozen}
        targets.discard(None)
        opaque = [a for a in frozen if not body_identity(reader.read(a))]
        selected = []
        for ref in pool.artifacts:
            kind, identifier, version = body_identity(pool.peek(ref))
            explicit_successor=ref.get('id','').startswith('selected:') and any(
                old_kind==kind and old_id==identifier for old_kind,old_id,_ in targets)
            if ((kind, identifier, version) in targets or explicit_successor or any(
                    a.get('sha256') == ref['sha256'] or
                    (a.get('version') == version and a.get('id') in (
                        kind+'/'+identifier, ':'.join((kind, identifier, version))))
                    for a in opaque)):
                selected.append(ref)
        if not selected:raise ValueError('Original opinion has no canonical target in this exact current delivery')
        for ref in [review['result_ref'],index['basis_ref'],index['submission_ref']]:
            if not reader.intact(ref):raise ValueError('Complete original opinion and its frozen scope must remain readable')
        fixed=[]
        for ref in selected:
            identity=body_identity(pool.require(ref))
            fixed.append(_fix_artifact(root,program_home(request,root=root)/'checks'/'review-applications',
                {**ref,'id':':'.join(identity),'version':identity[2]}))
        applications.append({'review_return_id':review_id,'original_conclusion':index['conclusion'],
            'submission_ref':index['submission_ref'],'opinion_ref':review['result_ref'],'basis_ref':index['basis_ref'],
            'author_return_id':returned['id'],'author_report_ref':returned['result_ref'],'handling_refs':refs,
            'candidate_refs':fixed,'use_fingerprint':_scientific_use(request,agreement),
            'meaning':'The task lead explicitly accepts original review applicability after author handling for these exact bodies/use. This is not a new independent opinion or an altered old conclusion.'})
    return applications


def _accepted_review_applications(c,task,request,pool,agreement):
    metadata={e['payload']['disposition_id']:e['payload'] for e in _events(c,task['id'],'delivery-acceptance-recorded')}
    active={}
    for row in t._effective_dispositions(c,task['id']):
        ret=t._row(c,'work_returns',row['return_id'])
        if ret['request_id'] not in pool.lineage:continue
        if row['disposition']!='adopt':active[row['return_id']]=[];continue
        for application in metadata.get(row['id'],{}).get('review_applications',[]):
            if application.get('use_fingerprint')!=_scientific_use(request,agreement):continue
            active.setdefault(row['return_id'],[]).append({**application,'disposition_id':row['id'],
                'adoption_ref':json.loads(row['evidence_ref_json']),'adopter':json.loads(row['actor_json']),
                'adopted_scope_ids':json.loads(row['scope_json'])})
    return [a for group in active.values() for a in group]


def _delivery_checks(c,root,task,request,returned,endpoint,evidence,prefix,proposed=()):
    from delivery_evidence import EvidenceSet,same_review_body,canonical_body,content_digest,changed_paths
    checks=[];own,parent=_contract_for_request(c,request)
    agreement=(own or parent or {}).get('body',{})
    agreement_source=(own or parent or {}).get('source_ref')
    pool=EvidenceSet(c,root,request,returned,evidence,extra_refs=contract_refs(agreement))
    checks.append(_check(prefix+'report','pass' if evidence.intact(returned['result_ref']) else 'unknown',
        'Read the complete original report and its conditions.',[returned['result_ref']]))
    checks.extend(_contract_checks(evidence,agreement,agreement_source,prefix+'contract.'))
    if endpoint is None:
        checks.append(_check(prefix+'endpoint','unknown','The agreed endpoint is not determined.'));return checks
    unbound_effects=[ref for ref,doc,_ in pool.documents() if doc.get('schemaVersion')==1
        and doc.get('outcome') in ('unknown','effect_unknown')
        and str(doc.get('operation','')).endswith((' create',' save',' submit',' resubmit',' publish'))
        and not ((doc.get('data') or {}).get('subject') or
                 ((doc.get('data') or {}).get('request') or {}).get('target'))]
    if unbound_effects:
        for ref in unbound_effects:pool.require(ref)
        checks.append(_check(prefix+'unknown_data_effect','unknown',
            'Original write attempt lacks an exact object binding. Reconcile this original attempt; no inferred effect or replay.',unbound_effects))
    artifacts=pool.artifacts
    members={t._json(_body_identity(pool.require(a))):{'artifact':a,'body':pool.peek(a),'facts':pool.native_facts(a),
        'opinions':[],'prior_opinions':[],'applications':[],'application_limits':[]} for a in artifacts}
    needs_review=endpoint!='evidence' or bool(request.get('requires_review'))
    if endpoint!='evidence':
        checks.append(_check(prefix+'candidate','pass' if artifacts and not pool.selection_missing else 'unknown',
            'Current explicitly submitted canonical versions; an empty save-only turn preserves the prior selection.',artifacts))
        native_sources=[r for m in members.values() for r in m['facts']['structure']]
        details=[{'subject':_body_identity(m['body']),'coverage':m['facts']['validation_coverage'],
                  'reading_limits':m['facts']['reading_limits']} for m in members.values()]
        checks.append(_check(prefix+'structure_references','pass' if members and all(m['facts']['structure'] for m in members.values()) else 'unknown',
            'Exact-input native checks and their actual coverage; no lca-gate wrapper is required. '+t._json(details),native_sources))
    review_rows={e['payload']['return_id']:e['payload'] for e in _events(c,task['id'],'review-conclusion-indexed')}
    applications=[*_accepted_review_applications(c,task,request,pool,agreement),*proposed]
    evidence_opinions=[]
    for r in _returns(c,task['id']):
        q=t._decode(t._row(c,'work_requests',r['request_id']))
        if not q['independent'] or q['parent_request_id'] not in pool.lineage:continue
        if t.native_key(r['author_actor']) == t.native_key(returned['author_actor']):continue
        if r.get('run_id') and t._row(c,'work_runs',r['run_id'])['outcome']!='succeeded':continue
        indexed=review_rows.get(r['id']);packet_ref=indexed['submission_ref'] if indexed else q['input_ref']
        packet=pool.peek(packet_ref)
        if not isinstance(packet,dict) or not pool.available(r['result_ref']):continue
        frozen=[(a,pool.peek(a)) for a in packet.get('artifact_refs',[])]
        for m in members.values():
            for ref,body in frozen:
                prior_identity=_body_identity(body)
                current_identity=_body_identity(m['body'])
                if (prior_identity and current_identity and prior_identity[:2]==current_identity[:2]
                        and not same_review_body(body,m['body'])):
                    pool.require(packet_ref);pool.require(r['result_ref']);pool.require(ref)
                    m['prior_opinions'].append({'return_id':r['id'],'opinion_ref':r['result_ref'],
                        'frozen_candidate':ref,'current_candidate':m['artifact'],'changed_paths':changed_paths(body,m['body']),
                        'same_version':prior_identity==current_identity,
                        'conclusion':indexed['conclusion'] if indexed else 'not_indexed',
                        'meaning':'Original scientific opinion remains intact. Read both complete bodies and author handling to assess its current applicability; differences do not fabricate a new scientific verdict.'})
        covered=[m for m in members.values() if any(same_review_body(m['body'],body) for _,body in frozen)]
        for application in [a for a in applications if a['review_return_id']==r['id']]:
            for m in members.values():
                matched=[ref for ref in application['candidate_refs'] if same_review_body(m['body'],pool.peek(ref))]
                if not matched:
                    if any(ref.get('id')==':'.join(_body_identity(m['body'])) for ref in application['candidate_refs']):
                        m['application_limits'].append({'application':application,'reason':'Current body differs from the explicitly adopted application, or its original binding is unreadable'})
                    continue
                sources=[application[k] for k in ('submission_ref','opinion_ref','basis_ref','author_report_ref','adoption_ref') if k in application]
                sources.extend([*application['handling_refs'], *matched, *(ref for ref, _ in frozen)])
                if not all(pool.available(ref) for ref in sources):
                    m['application_limits'].append({'application':application,'reason':'An original application source is unavailable/changed; no inferred pass'})
                    continue
                for ref in sources:pool.require(ref)
                m['applications'].append(application)

        if endpoint!='evidence' and not covered:continue
        pool.require(packet_ref);pool.require(r['result_ref'])
        if indexed:pool.require(indexed['basis_ref'])
        for m in covered:
            for ref,body in frozen:
                if same_review_body(m['body'],body):pool.require(ref)
            m['opinions'].append((r,indexed,packet))
        if endpoint=='evidence':evidence_opinions.append((r,indexed,packet))
    slots=list(members.values()) if endpoint!='evidence' else [{'opinions':evidence_opinions,'applications':[],'prior_opinions':[]}]
    for m in slots:
        replaced={old for _,index,_ in m['opinions'] if index for old in index.get('supersedes_return_ids',[])}
        m['opinions']=[(r,{**index,'superseded':r['id'] in replaced} if index else None,packet) for r,index,packet in m['opinions']]
        m['supported']=[o for o in m['opinions'] if o[1] and not o[1].get('superseded') and o[1]['conclusion'] in ('supported','supported_with_limits')]
        handled={a['review_return_id'] for a in m['applications']}
        issues={o[0]['id'] for o in m['opinions'] if o[1] and o[1]['conclusion']=='material_issue'}
        issues.update(o['return_id'] for o in m['prior_opinions'] if o['same_version'] and o['conclusion']=='material_issue')
        m['material_issue']=bool(issues-replaced-handled)
        m['science_ok']=bool(m['supported'] or m['applications']) and not m['material_issue']
        m['handling_ok']=m['science_ok'] and (bool(m['applications']) or returned['created_at']>=max(o[0]['created_at'] for o in m['opinions']))
    if needs_review:
        opinion_refs=[r['result_ref'] for m in slots for r,_,_ in m['opinions']]
        supported_refs=[r['result_ref'] for m in slots for r,_,_ in m['supported']]
        science_ok=bool(slots) and all(m['science_ok'] for m in slots)
        checks.append(_check(prefix+'scientific_review','pass' if science_ok else 'unknown',
            'Original independent scope/conclusion and any explicit task-lead acceptance of author handling/current body/use remain separate; neither an implicit latest-wins rule nor byte equality replaces scientific judgment.',opinion_refs))
        checks.append(_check(prefix+'author_handling','pass' if slots and all(m['handling_ok'] for m in slots) else 'unknown',
            'Original author report follows the opinion, or the task lead explicitly accepts the cited complete handling for these exact bodies/use.',[returned['result_ref']]))
        for name in ('unit_quantity_and_meaning','bilingual_meaning'):
            checks.append(_check(prefix+name,'pass' if science_ok else 'unknown',
                'Exact original coverage or explicit lead-accepted applicability after author handling; no mechanical threshold or inferred language approval.',supported_refs))
    if endpoint in ('saved_draft','submitted','published'):
        authority=agreement.get('authority_refs',[]) or (parent or {}).get('body',{}).get('authority_refs',[])
        checks.append(_check(prefix+'named_authority','pass' if authority and all(evidence.intact(r) for r in authority) else 'unknown',
            'Original named account/object/action authority; contract and evidence records grant no permission.',authority))
        names=['saved','full_readback']+(['submitted'] if endpoint in ('submitted','published') else [])+(['published'] if endpoint=='published' else [])
        for name in names:
            checks.append(_check(prefix+name,'pass' if members and all(m['facts'][name] for m in members.values()) else 'unknown',
                'Reuse original native account/environment/request and exact content evidence. Partial disk output is separate from complete stdout.',
                [r for m in members.values() for r in m['facts'][name]]))
    for key,m in (members.items() if endpoint!='evidence' else []):
        facts=m['facts'];detail={'subject':_body_identity(m['body']),'canonical_sha256':content_digest(canonical_body(m['body'])),
            'candidate_ref':m['artifact'],
            'prior_opinions_with_body_changes':m['prior_opinions'],
            'automatic_review_match':'matched' if m['opinions'] else 'unknown',
            'scientific_applications':m['applications'],'application_reading_limits':m['application_limits'],
            'native_validation_coverage':facts['validation_coverage'],'reading_limits':facts['reading_limits'],
            'scientific_opinions':[{'return_id':r['id'],'conclusion':p['conclusion'] if p else 'not_indexed',
                'superseded':bool(p and p.get('superseded')),
                'original_review_scope':packet.get('review_scope'),'source_ref':r['result_ref']} for r,p,packet in m['opinions']]}
        for name,okay,refs in [('structure_references',bool(facts['structure']),facts['structure']),
            *([('scientific_review',m['science_ok'],[r['result_ref'] for r,_,_ in m['opinions']])] if needs_review else []),
            *([(name,bool(facts[name]),facts[name]) for name in ('saved','full_readback','submitted','published')
               if name in names or facts[name]] if endpoint in ('saved_draft','submitted','published') else [])]:
            checks.append(_check(prefix+'member.'+key+'.'+name,'pass' if okay else 'unknown',t._json(detail),refs))
        if facts['unknown_effects']:
            checks.append(_check(prefix+'member.'+key+'.unknown_data_effect','unknown',
                'Reconcile this exact object/attempt. Original successful writes and unrelated objects are not replayed or invalidated.',facts['unknown_effects']))
    return checks


def _semantic_basis(c,task_id):
    task=t._row(c,'work_tasks',task_id)
    def selected(row,keys):return {k:row[k] for k in keys}
    contracts=[t._decode(r) for r in c.execute('SELECT * FROM work_delivery_contracts WHERE (subject_kind=\'task\' AND subject_id=?) OR (subject_kind=\'request\' AND subject_id IN (SELECT id FROM work_requests WHERE task_id=?)) ORDER BY subject_kind,subject_id',(task_id,task_id))] if t._exists(c,'work_delivery_contracts') else []
    for con in contracts:con.pop('revision',None);con.pop('updated_at',None)
    scope=_scope_rows(c,task)
    for row in scope:row.pop('revision',None)
    events=[e['payload'] for e in _events(c,task_id,'delivery-acceptance-recorded')]
    return {'task':selected(task,('id','original_ref_json','plan_ref_json','owner_actor','sponsor_actor','required_scope_json')),
        'stop_state':task['state'] if task['state'] in ('paused','cancelled') else None,
        'scope':scope,'contracts':contracts,
        'requests':[t._decode(r) for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY id',(task_id,))],
        'returns':_returns(c,task_id),'adoptions':[t._decode(r) for r in t._effective_dispositions(c,task_id)],
        'acceptance':events,'reviews':[e['payload'] for e in _events(c,task_id,'review-conclusion-indexed')],
        'delegation':t._delegation_decisions(c,task_id),
        'runs':[selected(r,('id','request_id','outcome','native_identity_json','stop_requested')) for r in c.execute('SELECT r.* FROM work_runs r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY r.id',(task_id,))],
        'decisions':[dict(r) for r in c.execute('SELECT * FROM work_decisions WHERE json_extract(scope_json,\'$.task\')=? ORDER BY id',(task_id,))] if t._exists(c,'work_decisions') else []}


def _dispatch_basis(c,task,requests):
    """Exact launch inputs, independent of concurrent delivery/progress facts.

    This check is compared before intent creation and at native launch. Returns,
    adoption, unrelated scopes and new run observations are neither dispatch
    inputs nor evidence that an admitted frozen review changed. Actual run/OS
    exclusion and native authority are still checked at the launch boundary.
    """
    request_ids={request['id'] for request in requests}
    scope_ids={sid for request in requests for sid in request['required_scope']}
    scopes=[]
    for row in _scope_rows(c,task):
        if row['scope_id'] in scope_ids:
            scopes.append({key:value for key,value in row.items() if key!='revision'})
    contracts=[]
    for request in requests:
        own=contract(c,'request',request['id'])
        if own:contracts.append({key:value for key,value in own.items() if key not in ('revision','updated_at')})
    controls=t._delegation_decisions(c,task['id'])
    return {'task':{key:task[key] for key in ('id','state','owner_actor','sponsor_actor','original_ref_json')},
        'requests':requests,'scope':scopes,'contracts':contracts,
        'delegation':{ident:controls[ident] for ident in sorted(request_ids) if ident in controls}}


def build_check(c,root,kind,ident,for_action,*,return_id=None,scope_ids=None,proposed=()):
    if for_action not in ('dispatch','handoff','complete'):raise ValueError('Check boundary must be dispatch|handoff|complete.')
    subject=t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
    task=subject if kind=='task' else t._row(c,'work_tasks',subject['task_id'])
    if for_action=='handoff':
        scope_ids=None  # One exact report check can be reused for any declared subset.
        if kind=='request' and return_id is None:
            versions=[r for r in _returns(c,task['id']) if r['request_id']==ident]
            if versions:return_id=versions[-1]['id']
    evidence=Evidence(root);checks=[];handoff_scopes={}
    requests=[t._decode(subject)] if kind=='request' else [t._decode(r) for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id',(ident,))]
    if for_action=='dispatch':
        checks.append(_check('task_active','pass' if task['state']=='active' else 'fail','Original task state is '+task['state']))
        scope={r['scope_id']:r for r in _scope_rows(c,task) if not r.get('legacy_unindexed')}
        for q in requests:
            evidence.read(q['input_ref'])
            checks.append(_check(q['id']+'.brief','pass' if evidence.intact(q['input_ref']) else 'unknown','Read the complete original brief.',[q['input_ref']]))
            if q['kind']!='plan' and not q['independent']:
                checks.append(_check(q['id']+'.scientific_contract','not_applicable','Maintenance/consultation/report registration does not use a production contract.'));continue
            own,parent=_contract_for_request(c,q);body=(own or {}).get('body',{})
            checks.extend(_contract_checks(evidence,body,(own or {}).get('source_ref'),q['id']+'.contract.'))
            scopes=q['required_scope']
            valid_scope=(bool(scopes) and set(scopes).issubset(scope)) or (q['independent'] and q.get('parent_request_id') and not scopes)
            checks.append(_check(q['id']+'.scope','pass' if valid_scope else 'fail','New production delegation identifies existing scope IDs; an original independent frozen-packet review may retain historical unindexed parent scope.'))
            for sid in scopes:
                for ref in scope.get(sid,{}).get('source_refs',[]):evidence.read(ref)
            checks.append(_check(q['id']+'.responsibility','pass' if q['recipient_role'] in t.ORG_ROLES else 'fail','Original receiver responsibility; native sandbox/capability admission still runs at launch.'))
    elif for_action=='handoff':
        scope_rows={row['scope_id']:row for row in _scope_rows(c,task)}
        for q in requests:
            selected=[r for r in _returns(c,task['id']) if r['request_id']==q['id'] and (not return_id or r['id']==return_id)]
            selected=selected if return_id else selected[-1:]
            if not selected:checks.append(_check(q['id']+'.report','unknown','No original report selected; partial reports are still always accepted.'));continue
            own,parent=_contract_for_request(c,q)
            child_endpoint=endpoint(own or parent)
            for r in selected:
                prefix=r['id']+'.'
                child_checks=_delivery_checks(c,root,task,q,r,child_endpoint,evidence,prefix,proposed)
                checks.extend(child_checks)
                additional={}
                for sid in r['coverage']:
                    if sid not in scope_rows:continue
                    required=endpoint(parent,scope_rows[sid])
                    achieved=child_endpoint
                    pending=[]
                    if child_endpoint and required and required!=child_endpoint:
                        if required not in additional:
                            scope_evidence=Evidence(root)
                            compared=_delivery_checks(c,root,task,q,r,required,scope_evidence,prefix,proposed)
                            withheld=[x for x in compared if x['status'] in ('unknown','fail')]
                            withheld.extend(
                                _check('scope_source','unknown',e.get('detail','Source unreadable'),[e['ref']])
                                for e in scope_evidence.sources.values() if e['status']!='pass')
                            additional[required]=(withheld, [e['ref'] for e in scope_evidence.sources.values()
                                                             if e['status']=='pass'])
                        pending,sources=additional[required]
                        for ref in sources:evidence.read(ref)
                        if not pending:achieved=required
                    handoff_scopes.setdefault(r['id'],{})[sid]={'required':required,'checked_endpoint':achieved,
                                                               'pending':pending}
    else:
        if task['state'] in ('paused','cancelled'):
            stops=_events(c,task['id'],'pause' if task['state']=='paused' else 'cancel')
            refs=[stops[-1]['payload']['evidence_ref']] if stops else []
            for ref in refs:evidence.read(ref)
            checks.append(_check('original_stop','fail','Original task is '+task['state']+'; completion does not restore it.',refs))
        view=projection(c,root,task['id'])
        checks.append(_check('scope_defined','pass' if view['total'] else 'unknown','Current necessary scope and endpoints must be explicit.'))
        all_returns={r['id']:r for r in _returns(c,task['id'])}
        for item in view['scope_items']:
            sid=item['scope_id']
            if kind=='request' and sid not in json.loads(subject['required_scope_json']):continue
            if item['state']=='dispositioned':
                evidence.read(item['disposition']['source_ref']);checks.append(_check(sid+'.scope_disposition','pass','Original delegator explicitly disposed this responsibility.',[item['disposition']['source_ref']]));continue
            checks.append(_check(sid+'.mapping','pass' if item['mapping_verified'] else 'unknown','Scope relations are verified.' if item['mapping_verified'] else 'Scope relations need explicit original-source verification.',item['source_refs']))
            metadata={d['disposition']['id']:d['acceptance'] for d in item['adoptions']}
            finals=_final_adoptions([d['disposition'] for d in item['adoptions']],metadata)
            required=item['endpoint']
            if kind=='request':required=endpoint(contract(c,'request',ident)) or required
            def reached(decision):
                recorded=metadata[decision['id']]
                parent_endpoint=recorded.get('endpoint_by_scope',{}).get(sid)
                return recorded.get('request_endpoint',parent_endpoint) if kind=='request' else parent_endpoint
            finals=finals[-1:] if finals and required and reached(finals[-1])==required else []
            checks.append(_check(sid+'.final_adoption','pass' if finals else 'unknown','Final owner adoption at the agreed endpoint; stage adoption leaves responsibility open.'))
            if not finals:continue
            d=finals[-1];r=all_returns[d['return_id']];q=t._decode(t._row(c,'work_requests',r['request_id']))
            evidence.read(d['evidence_ref'])
            checks.extend(_delivery_checks(c,root,task,q,r,required,evidence,sid+'.'))
        for q in requests:
            for row in t.pending_runs(c,q['id']):
                run=t._run_view(c,row)
                from execution_lease import observe_run
                observed=run.get('execution_observation') or (observe_run(root,run) if run.get('target_host')=='deepseek_harness' else {})
                clear=observed.get('state')=='exited' and not run.get('stop_requested')
                checks.append(_check(run['id']+'.execution','pass' if clear else 'unknown',
                    t._json({'execution':observed,'native_outcome':run['outcome'],
                        'detail':'OS exit does not settle native/data effects. Exact object effects are checked with the selected delivery.'})))
    for source in list(evidence.sources.values()):
        if source['status']!='pass':checks.append(_check('source:'+hashlib.sha256(t._json(source['ref']).encode()).hexdigest(),'unknown',source.get('detail','Source unreadable'),[source['ref']]))
    basis=_dispatch_basis(c,task,requests) if for_action=='dispatch' else _semantic_basis(c,task['id'])
    sources=sorted(evidence.sources.values(),key=lambda v:t._json(v['ref']))
    fingerprint=hashlib.sha256(t._json([kind,ident,for_action,return_id,scope_ids,basis,sources,checks,handoff_scopes]).encode()).hexdigest()
    missing=[check for check in checks if check['status'] in ('unknown','fail')]
    return {'schema':CHECK_SCHEMA,'subject':{'kind':kind,'id':ident,**({'return_id':return_id} if return_id else {})},
        'for':for_action,'ready':not missing,'checks':checks,'missing':missing,'sources':sources,'input_fingerprint':fingerprint,
        'scope_ids':scope_ids,**({'scope_endpoints':handoff_scopes} if handoff_scopes else {}),
        'authority':'Read-only evidence check; scientific adequacy/use and native write authorization remain with their owners.'}


def checked_report(c,root,kind,ident,for_action,*,supplied=None,return_id=None,scope_ids=None,proposed=()):
    # Re-derive deterministic interpretation from original reports; no expensive
    # native validation is rerun. A hand-written ready/pass cannot override it.
    report=build_check(c,root,kind,ident,for_action,return_id=return_id,scope_ids=scope_ids,proposed=proposed)
    if supplied:
        given=json.loads(Path(supplied).read_bytes())
        if given != report and (not proposed or given != build_check(c,root,kind,ident,for_action,
                return_id=return_id,scope_ids=scope_ids)):t._fail('stale_or_untrusted_check','Check is not this service result for the current exact inputs; regenerate work-check.',ident)
    subject=t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
    if kind=='task':home=Path(json.loads(subject['plan_ref_json'])['path']).parent/'program'
    else:
        from production_review import program_home
        home=program_home(t._decode(subject),root=root)
    from production_review import _save_fixed
    path=home/'checks'/report['input_fingerprint']/(for_action+'.json')
    ref=_save_fixed(root,path,(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode())
    return report,ref


def handoff_summary(report, ref):
    """A navigation view of the same check, without repeating opinion text."""
    members, remaining, author_reports = {}, [], {}
    for row in report['checks']:
        check_id = row['id']
        if '.member.' not in check_id:
            if check_id.endswith('.report'):
                author_reports[check_id.removesuffix('.report')] = row['source_refs']
            if row['status'] in ('unknown', 'fail'):
                remaining.append(row)
            continue
        key, name = check_id.rsplit('.', 1)
        if key not in members:
            return_id = key.split('.member.', 1)[0]
            detail = json.loads(row['detail'])
            accepted = [{
                'review_return_id': item['review_return_id'],
                'handling_refs': item['handling_refs'],
                'adoption_ref': item.get('adoption_ref'),
            } for item in detail['scientific_applications']]
            members[key] = {
                'return_id': return_id,
                'subject': detail['subject'],
                'candidate_ref': detail['candidate_ref'],
                'author_report_refs': author_reports.get(return_id, []),
                'opinions': [{k: opinion[k] for k in ('return_id','conclusion','superseded','source_ref')}
                             for opinion in detail['scientific_opinions']],
                'prior_opinions_with_changes': [{k: item[k] for k in ('return_id','opinion_ref','frozen_candidate')}
                                                for item in detail['prior_opinions_with_body_changes']],
                'lead_applications': accepted,
                'checks': {},
            }
        members[key]['checks'][name] = {'status': row['status'], 'source_refs': row['source_refs']}
    return {'subject': report['subject'], 'ready': report['ready'], 'check_ref': ref,
            'members': list(members.values()), 'remaining_checks': remaining,
            'scope_endpoints':{ret:{sid:{k:v for k,v in status.items() if k!='pending'}
                                    for sid,status in scopes.items()}
                               for ret,scopes in report.get('scope_endpoints',{}).items()}}


def check(root,kind,ident,for_action,*,summary=False):
    with contextlib.closing(t._connect(root,True)) as c:
        c.execute('BEGIN');t.ensure_schema(c)
        report,ref=checked_report(c,root,kind,ident,for_action)
        return handoff_summary(report, ref) if summary else {**report,'check_ref':ref}


def complete(c,root,actor,payload):
    task=t._row(c,'work_tasks',payload['task_id']);_owner(c,task,actor,maintenance=True)
    note=_ref(root,payload.get('evidence_ref'))
    lifecycle=c.execute("SELECT id FROM work_task_events WHERE task_id=? AND kind IN ('create','reopen') ORDER BY id DESC LIMIT 1",(task['id'],)).fetchone()[0]
    prior=c.execute("SELECT * FROM work_task_events WHERE task_id=? AND kind='complete' AND id>=? ORDER BY id LIMIT 1",(task['id'],lifecycle)).fetchone()
    if task['state']=='completed' and prior:
        saved=json.loads(prior['payload_json']);return saved.get('result',t._decode(task))
    stop=None
    if task['state'] in ('paused','cancelled'):
        stops=_events(c,task['id'],'pause' if task['state']=='paused' else 'cancel')
        stop=stops[-1] if stops else {'status':'original stop source needs readback'}
    report,ref=checked_report(c,root,'task',task['id'],'complete')
    group=[task['id'],lifecycle,t.native_key(actor),note,report['input_fingerprint']]
    kind='complete' if report['ready'] else 'completion-pending'
    ident=kind+':'+hashlib.sha256(t._json(group).encode()).hexdigest()
    existing=c.execute('SELECT payload_json FROM work_task_events WHERE task_id=? AND kind=? AND json_extract(payload_json,\'$.id\')=?',(task['id'],kind,ident)).fetchone()
    if existing:return json.loads(existing[0])['result']
    # Detect file races inside the transaction, before applying the completion.
    fresh=build_check(c,root,'task',task['id'],'complete')
    if fresh['input_fingerprint']!=report['input_fingerprint']:
        t._fail('check_input_changed','Original evidence changed during completion; no stale check applied.',task['id'])
    if report['ready']:task=t._touch(c,task,state='completed')
    result={**t._decode(task),'status':'completed' if report['ready'] else 'completion_pending',
            'completion_id':ident,'lifecycle':lifecycle,'check_ref':ref,'missing':report['missing']}
    if stop:result.update(stop_preserved=True,note_ref=note,stop_basis=stop)
    t._event(c,task,kind,actor,{'id':ident,'lifecycle':lifecycle,'evidence_ref':note,'input_fingerprint':report['input_fingerprint'],
                              'check_ref':ref,'remaining':report['missing'],'result':result})
    return result
