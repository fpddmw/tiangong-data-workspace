"""Scope, delivery contracts and acceptance on the original v3 task ledger.

No scheduler, scientific inference or alternative business state database.
"""
import contextlib
import gzip
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
        from task_ledger.sessions import reference
        return reference(root,ref)
    if isinstance(ref,dict) and 'sha256' not in ref and ref.get('path'):
        from task_ledger.sessions import reference
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
    if not isinstance(body,dict) or set(body)-{'purpose','endpoint','scope_boundary','acceptance','authority_refs','interfaces','reserve'}:
        raise ValueError('Contract uses purpose, endpoint, scope_boundary, acceptance, authority_refs, interfaces and optional task reserve.')
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
    if 'reserve' in body:
        reserve=body['reserve']
        if not isinstance(reserve,dict) or set(reserve)!={'scope_ids','core_scope_ids','max_requests'}:
            raise ValueError('Reserve identifies authorized scope_ids, core_scope_ids and max_requests')
        ids=t._list(reserve['scope_ids'],'Authorized reserve scope',True)
        core=t._list(reserve['core_scope_ids'],'Original core scope',True)
        if set(ids).intersection(core) or type(reserve['max_requests']) is not int or reserve['max_requests']<1:
            raise ValueError('Keep original core and reserve distinct and set a positive authorized request limit')
    return body


def contract_refs(body):
    return body.get('authority_refs',[])+body.get('acceptance',{}).get('refs',[])+body.get('interfaces',{}).get('refs',[])


def _retain_plan_sources(root, task, refs):
    """Keep the accepted bytes whenever a mutable continuation page is cited."""
    plan = Path(root) / json.loads(task['plan_ref_json'])['path']
    seen = set()
    for ref in refs:
        if Path(ref['path']) == plan and ref['sha256'] not in seen:
            t._plan_ref(root, ref)
            seen.add(ref['sha256'])


def _contract_source_copy(root, ref):
    source=Path(root)/ref['path']
    return {**ref,'path':str(source.parent/'contract-history'/ref['sha256']/source.name)}


def set_contract(c,root,kind,ident,body,source,actor,*,expected_revision=None):
    subject=t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
    task=subject if kind=='task' else t._row(c,'work_tasks',subject['task_id'])
    _owner(c,task,actor,request=subject if kind=='request' else None,maintenance=True)
    if expected_revision is not None:t._revision(task,{'expected_task_revision':expected_revision})
    body=validate_contract(root,body);source=_ref(root,source)
    if kind=='request' and 'reserve' in body:
        raise ValueError('Reserve comes from the original task commitment')
    _retain_plan_sources(root,task,[source,*contract_refs(body)])
    # Decision and interface pages describe the original promise. Keep their
    # accepted bytes without substituting for live grants or exact brief files.
    if kind=='task' or not subject['independent']:
        from production_review import _save_fixed
        for ref in [source,*body.get('interfaces',{}).get('refs',[])]:
            original=t.evidence_ref(root,ref)
            copied=_contract_source_copy(root,ref)
            raw=Path(original['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=ref['sha256']:
                raise ValueError('Contract source changed while preserving its original bytes.')
            _save_fixed(root,copied['path'],raw)
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
        t.ensure_schema(c);subject=t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
        result={'subject':{'kind':kind,'id':ident},'contract':contract(c,kind,ident)}
        if kind=='request':
            result['effective']=effective_contract(c,t._decode(subject))
        return result


def save_contract(root,actor,kind,ident,path,source_path):
    from task_ledger.sessions import reference
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
        sources = [source, *(ref for item in items for ref in item['source_refs'])]
        _retain_plan_sources(root, task, sources)
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


def projection(c,root,task_id,*,compact=False,returns=None,detail_scope_id=None):
    """One all-scope read-only projection, reused by CLI, DSH and context hooks."""
    task=t._row(c,'work_tasks',task_id)
    parent=contract(c,'task',task_id)
    requests=[t._decode(r) for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id',(task_id,))]
    returned=_returns(c,task_id) if returns is None else returns
    latest={row['request_id']:t._decode(row) for row in c.execute('''SELECT r.* FROM work_runs r
        JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY r.created_at,r.id''',(task_id,))}
    # This projection uses only the latest original run for each request. Read
    # those receipts together so a large package still fits the Hook deadline.
    receipts={row['run_id']:row for row in c.execute('''SELECT e.run_id,e.receipt_json,e.payload_json FROM work_run_events e
        JOIN (SELECT MAX(id) AS id FROM work_run_events WHERE receipt_json IS NOT NULL
              AND run_id IN (SELECT value FROM json_each(?)) GROUP BY run_id) chosen ON e.id=chosen.id''',
        (t._json([r['id'] for r in latest.values()]),))} if latest else {}
    for run in latest.values():
        observed=receipts.get(run['id'])
        run['latest_receipt']=json.loads(observed['receipt_json']) if observed else None
        run['execution_observation']=json.loads(observed['payload_json']).get('execution_observation') if observed else None
    ended=t._delegation_decisions(c,task_id)
    decisions=[t._decode(r) for r in t._effective_dispositions(c,task_id)]
    acceptance={e['payload']['disposition_id']:e['payload'] for e in _events(c,task_id,'delivery-acceptance-recorded')}
    from platform_review.submission import submitted_members
    submissions=submitted_members(c,task_id)
    items=[];filters={k:[] for k in ('unassigned','no_explicit_coverage','coverage_pending_adoption','execution_ended_incomplete','coverage_unindexed','mapping_unverified','execution_unknown','stopped_or_paused','never_started')}
    dispositioned={}
    if t._exists(c,'work_decisions'):
        from task_ledger import decisions as harness_decisions
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
        if reached=='saved_draft' and required_endpoint=='submitted' and final:
            adopted_members=acceptance[final['id']].get('candidate_subjects',[])
            expected={(tuple(row['subject']),row['canonical_sha256']) for row in adopted_members}
            if expected and expected<=submissions.get(final['id'],set()):
                reached='submitted'
        accepted=bool(required_endpoint and reached==required_endpoint)
        state=None if not verified else 'dispositioned' if sid in dispositioned else ('submitted' if reached in ('submitted','published') else 'accepted') if accepted else 'active' if responsibility else 'unassigned'
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
        if compact:
            if final and sid == detail_scope_id:
                item['accepted_use'] = {'id': final['id'], 'source_ref': final['evidence_ref'],
                                        'endpoint': reached}
            for key, id_key in (('coverage_returns', 'return_id'), ('coverage_unindexed', 'return_id')):
                item[key+'_count'] = len(item[key])
                item[key] = [{id_key: r[id_key], 'result_ref': r['result_ref']} for r in item[key][-1:]]
            item['adoption_count'] = len(item['adoptions'])
            item['adoptions'] = [{'id': d['disposition']['id'],
                                  'use': d['disposition']['disposition'],
                                  'source_ref': d['disposition']['evidence_ref']}
                                 for d in (item['adoptions'] if sid == detail_scope_id else item['adoptions'][-1:])]
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


def _returns(c,task_id,*,request_ids=None):
    condition = '' if request_ids is None else ' AND q.id IN (SELECT value FROM json_each(?))'
    args = (task_id,) if request_ids is None else (task_id, t._json(list(request_ids)))
    rows=[t._decode(r) for r in c.execute('SELECT r.* FROM work_returns r JOIN work_requests q ON q.id=r.request_id '
        'WHERE q.task_id=?'+condition+' ORDER BY r.created_at,r.id',args)]
    by_id={r['id']:r for r in rows}
    for e in _evidence_events(c,task_id,'return-observation','return_id',by_id):
        p=e['payload'];r=by_id.get(p.get('return_id'))
        if r:
            for key in ('coverage','artifact_refs','remaining_scope'):
                if key in p:r[key]=p[key]
    return rows


def _evidence_events(c, task_id, kind, field, ids):
    if not ids:
        return []
    return [t._decode(row) for row in c.execute('''SELECT * FROM work_task_events
        WHERE task_id=? AND kind=? AND json_extract(payload_json,?) IN (SELECT value FROM json_each(?))
        ORDER BY id''', (task_id, kind, '$.'+field, t._json(list(ids))))]


def _review_returns(c, task_id, lineage):
    requests = {r['id']:t._decode(r) for r in c.execute('''SELECT * FROM work_requests WHERE task_id=?
        AND independent=1 AND parent_request_id IN (SELECT value FROM json_each(?))''', (task_id,t._json(lineage)))}
    return [(r, requests[r['request_id']]) for r in _returns(c, task_id, request_ids=requests)]


def _final_adoptions(decisions,acceptance):
    current={}
    for decision in decisions:
        if decision['disposition']!='adopt':current.pop(decision['return_id'],None)
        elif acceptance.get(decision['id'],{}).get('acceptance')=='final':
            current.pop(decision['return_id'],None)
            current[decision['return_id']]=decision
    return list(current.values())


class Evidence:
    def __init__(self,root,*,observations=None):
        self.root=Path(root);self.sources={};self.issues=[]
        self.historical_gaps=set()
        self._observed = observations if observations is not None else {}

    @staticmethod
    def _revision(path):
        try:
            info = path.stat()
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        except OSError:
            return None

    def _observe(self, ref):
        coordinate = ref.get('path'), ref.get('sha256')
        if not all(isinstance(item, str) and item for item in coordinate):
            return {'status':'unknown','detail':'Evidence requires an exact path and sha256'}, None, {}
        if coordinate in self._observed:
            return self._observed[coordinate]
        sources = {}
        try:
            path = Path(t.evidence_ref(self.root, ref)['path'])
            revision = self._revision(path)
            raw = path.read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if not revision or actual != ref['sha256'] or self._revision(path) != revision:
                raise ValueError('Original source changed during read')
            sources[path] = revision
            original = self.root / ref['path']
            if original.resolve() == path:
                sources[original] = revision
            row = {'status':'pass', 'actual_sha256':actual, 'bytes':len(raw)}
            try:
                if path.suffix == '.gz':
                    from io import BytesIO
                    with gzip.GzipFile(fileobj=BytesIO(raw)) as compressed:
                        limit = 32 * 1024 * 1024
                        raw = compressed.read(limit+1)
                    value = json.loads(raw) if len(raw) <= limit else None
                else:
                    value = json.loads(raw)
            except (ValueError, UnicodeError, OSError, EOFError):
                value = None
        except (ValueError, OSError, KeyError, TypeError) as exc:
            row, value = {'status':'unknown','detail':str(exc)}, None
            sources.clear()
        self._observed[coordinate] = row, value, sources
        return self._observed[coordinate]

    def peek(self,ref):
        return self._observe(ref)[1] if isinstance(ref,dict) else None

    def available(self,ref):
        return isinstance(ref,dict) and self._observe(ref)[0]['status'] == 'pass'

    def read(self,ref):
        if not isinstance(ref,dict):
            self.issues.append({'ref':ref,'reason':'Missing exact source reference'});return None
        key=t._json(ref)
        row,value,_=self._observe(ref)
        if key not in self.sources:
            self.sources[key]={'ref':ref,**row}
            if row['status']!='pass':
                self.issues.append({'ref':ref,'reason':row['detail']})
        return value

    def unchanged(self):
        """Compare versions of the exact files actually used by the decision."""
        versions = {version for source in self.sources.values() for version in
                    self._observe(source['ref'])[2].items()}
        return all(self._revision(path) == revision for path, revision in versions)

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
    # Later contract edits at the same plan version also cannot widen this
    # delegation. Use the last parent contract actually available at binding.
    row=c.execute('''SELECT json_extract(payload_json,'$.after') FROM work_task_events
        WHERE task_id=? AND kind='contract-updated' AND plan_revision<=? AND created_at<=?
          AND json_extract(payload_json,'$.after.subject_kind')='task' ORDER BY id DESC LIMIT 1''',
        (request['task_id'],request['plan_revision'],request['created_at'])).fetchone()
    parent=json.loads(row[0]) if row else None
    if parent is None:
        original=c.execute('''SELECT json_extract(payload_json,'$.baseline.contract') FROM work_task_events
            WHERE task_id=? AND plan_revision=? AND created_at<=? AND kind IN ('create','revise','reopen')
            ORDER BY id DESC LIMIT 1''',(request['task_id'],request['plan_revision'],request['created_at'])).fetchone()
        parent=json.loads(original[0]) if original and original[0] else None
    return contract(c,'request',request['id']),parent


def effective_contract(c,request):
    own,parent=_contract_for_request(c,request)
    sources=[parent['source_ref']] if parent else []
    inherited=(parent or {}).get('body',{})
    parent_exclusions=inherited.get('scope_boundary',{}).get('out_of_scope')
    if request['kind']=='plan' and (previous:=t.finite.request_configs(c,request['task_id']).get(request['id'],{}).get('continues_request_id')):
        original=effective_contract(c,t._decode(t._row(c,'work_requests',previous)))
        inherited=original['body']
        sources.extend(original['source_refs'])
    change=(own or {}).get('body',{})
    body={**inherited,**change}
    for name in ('scope_boundary','acceptance','interfaces'):
        first,second=inherited.get(name,{}),change.get(name,{})
        if first or second:
            body[name]={**first,**second}
            if name!='scope_boundary' and (first.get('refs') or second.get('refs')):
                body[name]['refs']=list({t._json(ref):ref for ref in first.get('refs',[])+second.get('refs',[])}.values())
    boundary=body.get('scope_boundary',{})
    exclusions=list(dict.fromkeys(value for value in (parent_exclusions,inherited.get('scope_boundary',{}).get('out_of_scope'),
                                                      change.get('scope_boundary',{}).get('out_of_scope')) if value))
    if exclusions:
        boundary['out_of_scope']='\n'.join(exclusions)
    authority=inherited.get('authority_refs',[])+change.get('authority_refs',[])
    if authority:
        body['authority_refs']=list({t._json(ref):ref for ref in authority}.values())
    return {'body':body,'plan_revision':request['plan_revision'],'scope_ids':request['required_scope'],
            'brief_ref':request['input_ref'],'source_refs':list({t._json(ref):ref for ref in
                 [*sources,*([own['source_ref']] if own else [])]}.values())}


def request_progress(c,request):
    from harness_package import author_lineage
    expected=endpoint(effective_contract(c,request))
    scope_ids=request['required_scope']
    finals={}
    lineage=author_lineage(c,request)
    rows=[t._decode(row) for row in t._effective_dispositions(c,request['task_id'],request_ids=lineage)
          if t._row(c,'work_returns',row['return_id'])['request_id'] in lineage]
    accepted={e['payload']['disposition_id']:e['payload'] for e in _evidence_events(
        c,request['task_id'],'delivery-acceptance-recorded','disposition_id',[row['id'] for row in rows])}
    for row in _final_adoptions(rows,accepted):
        record=accepted[row['id']]
        for sid in row['scope']:
            if sid in scope_ids and (record.get('request_endpoint')==expected or record.get('endpoint_by_scope',{}).get(sid)==expected):
                finals[sid]={'id':row['id'],'return_id':row['return_id'],'source_ref':row['evidence_ref'],'at':row['created_at']}
    remaining=[sid for sid in scope_ids if sid not in finals]
    return {'endpoint':expected,'adopted':finals,'remaining_scope':remaining,
            'delivered':bool(scope_ids and expected and not remaining)}


def _partially_adopted_return(c, request, lineage, returned, revision):
    # The ledger already identifies exact candidates on native returns and final
    # adoptions. Compare those facts without reading large bodies in a dispatch transaction.
    import delivery_evidence
    from dataset_checks.references import UUID, VERSION
    refs = json.loads(returned['artifact_refs_json'])
    original = set()
    for ref in delivery_evidence.explicit_selection(refs) or refs:
        parts = ref.get('id', '').removeprefix('selected:').split(':')
        if len(parts) == 3 and parts[2] == ref.get('version') and UUID.fullmatch(parts[1]) and VERSION.fullmatch(parts[2]):
            original.add(tuple(parts))
    if not original:
        return False
    later = [t._decode(row) for row in t._effective_dispositions(c,request['task_id'],request_ids=lineage)
             if row['created_at']>=revision['created_at']
             and set(json.loads(row['scope_json'])).intersection(json.loads(revision['scope_json']))]
    events = {e['payload']['disposition_id']:e['payload'] for e in _evidence_events(
        c,request['task_id'],'delivery-acceptance-recorded','disposition_id',[r['id'] for r in later])}
    adopted = set()
    for final in _final_adoptions(later,events):
        members = events[final['id']].get('candidate_subjects')
        if final['return_id'] == returned['id'] or not members:
            return False
        adopted.update(tuple(row['subject']) for row in members)
    return bool(adopted and original - adopted)


def continuation(c, root, request, basis=None):
    """The unfinished original unit can continue; settled units need an exact reopening basis."""
    if request['kind']!='plan' or request['independent']:
        if basis:
            raise ValueError('A continuation basis belongs to an original author assignment')
        return None
    progress=request_progress(c,request)
    if basis:
        from harness_package import author_lineage
        from scientific_review.rounds import completed_return, recorded_assessments
        lineage=author_lineage(c,request)
        returned=c.execute('SELECT * FROM work_returns WHERE id=?',(basis,)).fetchone()
        adoption_cutoff=max((item['at'] for item in progress['adopted'].values()),default='')
        if returned:
            review=t._row(c,'work_requests',returned['request_id'])
            explicit=recorded_assessments(c,request['task_id']).get(basis,{})
            if (review['independent'] and review['task_id']==request['task_id'] and review['parent_request_id'] in lineage
                    and returned['created_at']>adoption_cutoff and completed_return(c,returned)
                    and explicit.get('conclusion')!='not_assessed'):
                return t.evidence_ref(root,json.loads(returned['result_ref_json']))
        decision=c.execute('SELECT * FROM work_dispositions WHERE id=?',(basis,)).fetchone()
        if decision and decision['disposition']=='revise':
            returned=t._row(c,'work_returns',decision['return_id'])
            if (returned['request_id'] in lineage
                    and any(d['id']==basis for d in t._effective_dispositions(c,request['task_id'],return_id=returned['id']))
                    and (decision['created_at']>adoption_cutoff or _partially_adopted_return(c,request,lineage,returned,decision))):
                return t.evidence_ref(root,json.loads(decision['evidence_ref_json']))
        reopened=c.execute('''SELECT kind,payload_json FROM work_task_events WHERE task_id=? AND kind IN ('reopen','revise')
            AND created_at>? AND json_extract(payload_json,'$.id')=?''',(request['task_id'],adoption_cutoff,basis)).fetchone()
        if reopened:
            original=json.loads(reopened['payload_json'])
            if (reopened['kind']=='reopen' or set(original.get('affected_request_ids',[])).intersection(lineage)
                    or set(original.get('affected_scope',[])).intersection(request['required_scope'])):
                return t.evidence_ref(root,original['evidence_ref'])
        raise ValueError('Use an applicable new original independent return or the lead’s original revise decision for this same assignment')
    if progress['delivered']:
        return {'state':'original_delivered','request_id':request['id'],'progress':progress,
                'next':'Use an original review or revise decision for this unit. Assign a new independent use with work-request.'}
    return None


def _contract_checks(evidence,body,source,prefix):
    checks=[]
    for name,value in [('purpose',body.get('purpose')),('endpoint',body.get('endpoint'))]:
        okay=isinstance(value,str) and bool(value.strip()) and (name!='endpoint' or value in ENDPOINTS)
        checks.append(_check(prefix+name,'pass' if okay else 'fail','Information present; scientific adequacy remains with delegator/author.' if okay else 'Missing '+name,source))
    exact=body.get('authority_refs',[])+body.get('acceptance',{}).get('refs',[])
    for ref in exact:
        evidence.read(ref)
    for ref in source+body.get('interfaces',{}).get('refs',[]):
        if ref not in exact:
            copied=_contract_source_copy(evidence.root,ref)
            evidence.read(copied if evidence.available(copied) else ref)
    return checks


def freeze_review_submission(root, parent, review_id, brief, agreement):
    """Keep a plain brief and its explicitly named canonical candidates together."""
    from production_review import program_home, _fix_artifact, _save_fixed
    candidates, other = [], []
    for ref in contract_refs(agreement):
        exact = t.evidence_ref(root, ref)
        try:
            body = json.loads(Path(exact['path']).read_bytes())
        except ValueError:
            body = None
        identity = _body_identity(body)
        if identity:
            candidates.append({'id': identity[0]+'/'+identity[1], 'version': identity[2], **exact})
        else:
            other.append(exact)
    if not candidates:
        return None
    directory = program_home(parent, root=root)/'reviews'/review_id
    fixed = [_fix_artifact(root, directory, ref) for ref in candidates]
    packet = {'schema': 'data-workspace.review-submission-index.v1', 'original_brief_ref': brief,
              'review_scope': agreement.get('purpose', ''), 'artifact_refs': fixed,
              'evidence_refs': [brief, *fixed, *other]}
    raw = (t._json(packet)+'\n').encode()
    return _save_fixed(root, directory/('submission-index-'+hashlib.sha256(raw).hexdigest()+'.json'), raw)


def review_record(root,actor,return_id,conclusion,basis_path,*,supersedes=(),recurs_after=None):
    from task_ledger.sessions import reference
    from scientific_review.rounds import submission_ref
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
        submission=t.evidence_ref(root,submission_ref(c,t._decode(request)))
        history=[e for e in _events(c,task['id'],'review-conclusion-indexed') if e['payload']['return_id']==return_id]
        supersedes=list(dict.fromkeys(supersedes))
        if history:
            prior=history[-1]['payload']
            if prior['conclusion']!=conclusion or prior['basis_ref']!=basis:
                raise ValueError('This original opinion is already indexed differently; retain it and record a new substantive opinion.')
            if recurs_after and prior.get('recurs_after') and recurs_after!=prior['recurs_after']:
                raise ValueError('This original opinion already names a different recurrence basis')
            if (set(supersedes).issubset(prior.get('supersedes_return_ids', []))
                    and (not recurs_after or recurs_after==prior.get('recurs_after'))):
                return {**prior,'reviewer':json.loads(returned['author_actor']),'opinion_ref':opinion}
            recurs_after=recurs_after or prior.get('recurs_after')
            supersedes=list(dict.fromkeys([*prior.get('supersedes_return_ids',[]),*supersedes]))
            submission=t.evidence_ref(root,prior['submission_ref'])
        try:packet=json.loads(Path(submission['path']).read_bytes())
        except ValueError:packet=None
        if not isinstance(packet,dict) or not packet.get('artifact_refs'):
            # Historical plain briefs may predate freezing at registration.
            agreement=(contract(c,'request',request['id']) or {}).get('body',{})
            parent=t._decode(t._row(c,'work_requests',request['parent_request_id']))
            if frozen:=freeze_review_submission(root,parent,request['id'],submission,agreement):
                submission=frozen
                packet=json.loads(Path(frozen['path']).read_bytes())
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
                old_ref=t.evidence_ref(root, submission_ref(c,t._decode(old),old_history[-1]['payload'] if old_history else None))
                old_packet=reader.read(old_ref)
                old_ids=target_ids(old_packet)
                if not old_ids or not old_ids.issubset(current_ids):
                    raise ValueError('The replacing complete review must explicitly cover every prior target identity')
        if recurs_after:
            from scientific_review.rounds import completed_return, recorded_assessments
            from scientific_review.delta import packet_bodies
            from harness_package import author_lineage
            if conclusion!='material_issue' or t.native_key(actor) not in (
                    t.native_key(returned['author_actor']), t.native_key(task['owner_actor']), t.native_key(task['sponsor_actor'])):
                raise ValueError('The independent reviewer, task lead or sponsor may refer an assessed material recurrence')
            earlier=t._row(c,'work_returns',recurs_after)
            old=t._decode(t._row(c,'work_requests',earlier['request_id']))
            lineage=author_lineage(c,t._decode(t._row(c,'work_requests',request['parent_request_id'])))
            assessments=recorded_assessments(c,task['id'])
            earlier_index=assessments.get(recurs_after,{})
            if (not old['independent'] or old['task_id']!=task['id'] or old['parent_request_id'] not in lineage
                    or earlier['created_at']>=returned['created_at'] or not completed_return(c,earlier)
                    or not completed_return(c,returned) or earlier_index.get('conclusion')=='not_assessed'):
                raise ValueError('Refer to an earlier completed assessment of this author responsibility')
            reader=Evidence(root)
            previous=reader.read(submission_ref(c,old,earlier_index))
            reader.read(json.loads(earlier['result_ref_json']))
            if (reader.issues or not all(isinstance(p,dict) and p.get('artifact_refs') for p in (packet,previous))
                    or packet.get('scientific_purpose')!=previous.get('scientific_purpose')
                    or not ({key[:2] for key in packet_bodies(root,packet)[0]} &
                            {key[:2] for key in packet_bodies(root,previous)[0]})):
                raise ValueError('A recurrence referral needs earlier original evidence and an overlapping frozen object and use')
        p={'return_id':return_id,'conclusion':conclusion,'basis_ref':basis,'submission_ref':submission,
           'supersedes_return_ids':supersedes,**({'recurs_after':recurs_after} if recurs_after else {})}
        t._event(c,task,'review-conclusion-indexed',actor,p)
        return {**p,'reviewer':json.loads(returned['author_actor']),'opinion_ref':opinion}


def _scientific_use(request,agreement):
    # Endpoint, account authority and technical revisions do not change a use.
    return hashlib.sha256(t._json({key:agreement.get(key) for key in
        ('purpose','scope_boundary','acceptance','interfaces')}).encode()).hexdigest()


def review_applications(c,root,task,request,returned,actor,review_ids,handling_refs,*,reader=None):
    """Bind the lead's explicit handling/use judgment, never rewrite a review."""
    from delivery_evidence import EvidenceSet,body_identity
    from production_review import program_home,_fix_artifact
    from scientific_review.rounds import completed_return, _original_text, submission_ref
    _owner(c,task,actor)
    if not handling_refs:raise ValueError('Cite the complete original author handling with --handling')
    reader=reader if reader is not None else Evidence(root)
    refs=[t.evidence_ref(root,r) for r in handling_refs]
    for ref in [returned['result_ref'],*refs]:
        if not reader.intact(ref):raise ValueError('Original author report/handling is unavailable or changed')
    agreement=effective_contract(c,request)['body']
    pool=EvidenceSet(c,root,request,returned,reader)
    if not pool.artifacts or pool.selection_missing:raise ValueError('Associate the exact current canonical candidates before accepting applicability')
    indexes={e['payload']['return_id']:e['payload'] for e in
        _evidence_events(c,task['id'],'review-conclusion-indexed','return_id',review_ids)}
    applications=[]
    for review_id in dict.fromkeys(review_ids):
        review=t._decode(t._row(c,'work_returns',review_id));q=t._decode(t._row(c,'work_requests',review['request_id']))
        index=indexes.get(review_id)
        if not q['independent'] or q['task_id']!=task['id'] or q['parent_request_id'] not in pool.lineage:
            raise ValueError('Apply only an independent opinion from this original author responsibility')
        if t.native_key(review['author_actor'])==t.native_key(returned['author_actor']):
            raise ValueError('An author Session cannot certify its own work as independent review')
        if index and index['conclusion']=='not_assessed':
            raise ValueError('The original independent return is explicitly not_assessed and cannot certify this use')
        if not completed_return(c, review) or not _original_text(root, review['result_ref']).strip():
            raise ValueError('Read the original successful independent return before accepting applicability')
        submission = submission_ref(c, q, index)
        basis_ref = index['basis_ref'] if index else review['result_ref']
        packet=reader.read(submission)
        if not isinstance(packet,dict):raise ValueError('Read the complete original review submission that names the frozen candidates')
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
        for ref in [review['result_ref'],basis_ref,submission]:
            if not reader.intact(ref):raise ValueError('Complete original opinion and its frozen scope must remain readable')
        fixed=[]
        for ref in selected:
            identity=body_identity(pool.require(ref))
            fixed.append(_fix_artifact(root,program_home(request,root=root)/'checks'/'review-applications',
                {**ref,'id':':'.join(identity),'version':identity[2]}))
        applications.append({'review_return_id':review_id,'original_conclusion':index['conclusion'] if index else None,
            'submission_ref':submission,'opinion_ref':review['result_ref'],'basis_ref':basis_ref,
            'author_return_id':returned['id'],'author_report_ref':returned['result_ref'],'handling_refs':refs,
            'candidate_refs':fixed,'use_fingerprint':_scientific_use(request,agreement),
            'meaning':'The task lead explicitly accepts original review applicability after author handling for these exact bodies/use. This is not a new independent opinion or an altered old conclusion.'})
    return applications


def _accepted_review_applications(c,task,request,pool,agreement):
    decisions=t._effective_dispositions(c,task['id'],request_ids=pool.lineage)
    metadata={e['payload']['disposition_id']:e['payload'] for e in
        _evidence_events(c,task['id'],'delivery-acceptance-recorded','disposition_id',[row['id'] for row in decisions])}
    active={}
    for row in decisions:
        if row['disposition']!='adopt':active[row['return_id']]=[];continue
        for application in metadata.get(row['id'],{}).get('review_applications',[]):
            if application.get('use_fingerprint')!=_scientific_use(request,agreement):continue
            active.setdefault(row['return_id'],[]).append({**application,'disposition_id':row['id'],
                'adoption_ref':json.loads(row['evidence_ref_json']),'adopter':json.loads(row['actor_json']),
                'adopted_scope_ids':json.loads(row['scope_json'])})
    return [a for group in active.values() for a in group]


def _delivery_checks(c,root,task,request,returned,endpoint,evidence,prefix,proposed=()):
    from delivery_evidence import EvidenceSet,same_review_body,canonical_body,content_digest,changed_paths
    from scientific_review.rounds import submission_ref
    checks=[];effective=effective_contract(c,request)
    agreement=effective['body']
    pool=EvidenceSet(c,root,request,returned,evidence,extra_refs=contract_refs(agreement))
    checks.append(_check(prefix+'report','pass' if evidence.intact(returned['result_ref']) else 'unknown',
        'Read the complete original report and its conditions.',[returned['result_ref']]))
    checks.extend(_contract_checks(evidence,agreement,effective['source_refs'],prefix+'contract.'))
    if endpoint is None:
        checks.append(_check(prefix+'endpoint','unknown','The agreed endpoint is not determined.'));return checks
    unbound_effects=[ref for ref,doc,_ in pool.documents() if doc.get('schemaVersion')==1
        and (doc.get('status')=='unknown' or doc.get('outcome') in ('unknown','effect_unknown','write_outcome_unknown'))
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
    candidate_reviews = _review_returns(c,task['id'],pool.lineage)
    review_rows={e['payload']['return_id']:e['payload'] for e in
        _evidence_events(c,task['id'],'review-conclusion-indexed','return_id',[r['id'] for r,_ in candidate_reviews])}
    applications=[*_accepted_review_applications(c,task,request,pool,agreement),*proposed]
    evidence_opinions=[]
    for r,q in candidate_reviews:
        if t.native_key(r['author_actor']) == t.native_key(returned['author_actor']):continue
        if r.get('run_id') and t._row(c,'work_runs',r['run_id'])['outcome']!='succeeded':continue
        indexed=review_rows.get(r['id']);packet_ref=submission_ref(c,q,indexed)
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
        for application in [a for a in applications if a['review_return_id']==r['id']
                            and (not indexed or indexed['conclusion']!='not_assessed')]:
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
        opinion_refs=[row for m in slots for row in
                      [*(r['result_ref'] for r,_,_ in m['opinions']),*(a['opinion_ref'] for a in m['applications'])]]
        science_ok=bool(slots) and all(m['science_ok'] for m in slots)
        checks.append(_check(prefix+'scientific_review','pass' if science_ok else 'unknown',
            'Original independent scope/conclusion and any explicit task-lead acceptance of author handling/current body/use remain separate; neither an implicit latest-wins rule nor byte equality replaces scientific judgment.',opinion_refs))
        checks.append(_check(prefix+'author_handling','pass' if slots and all(m['handling_ok'] for m in slots) else 'unknown',
            'Original author report follows the opinion, or the task lead explicitly accepts the cited complete handling for these exact bodies/use.',[returned['result_ref']]))
    if endpoint in ('saved_draft','submitted','published'):
        authority=agreement.get('authority_refs',[])
        unavailable=[ref for ref in authority if not evidence.intact(ref)]
        plan=Path(root)/json.loads(task['plan_ref_json'])['path']
        historical=bool(endpoint in ('saved_draft','submitted') and 0<len(unavailable)<len(authority) and members
                        and all(Path(root)/ref['path']==plan for ref in unavailable)
                        and all(m['facts']['submitted'] and not m['facts']['unknown_effects'] for m in members.values()))
        if historical:
            evidence.historical_gaps.update(t._json(ref) for ref in unavailable)
        checks.append(_check(prefix+'named_authority',
            'historical_unavailable' if historical else 'pass' if authority and not unavailable else 'unknown',
            ('Missing original plan bytes. Exact existing acceptance may be judged on its retained evidence. '
             'New platform actions require current named authority.' if historical else
             'Original named account/object/action authority; contract and evidence records grant no permission.'),authority))
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
                'reviewer':r['author_actor'],'frozen_candidates':packet.get('artifact_refs',[]),
                'original_review_scope':packet.get('review_scope'),'source_ref':r['result_ref']} for r,p,packet in m['opinions']]}
        for name,okay,refs in [('structure_references',bool(facts['structure']),facts['structure']),
            *([('scientific_review',m['science_ok'],[*(r['result_ref'] for r,_,_ in m['opinions']),
                    *(a['opinion_ref'] for a in m['applications'])])] if needs_review else []),
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
    contracts=[effective_contract(c,request) for request in requests]
    controls=t._delegation_decisions(c,task['id'])
    return {'task':{key:task[key] for key in ('id','state','owner_actor','sponsor_actor','original_ref_json')},
        'requests':requests,'scope':scopes,'contracts':contracts,
        'delegation':{ident:controls[ident] for ident in sorted(request_ids) if ident in controls}}


def _handoff_basis(c, task, request, returned, scope_ids=()):
    """Only metadata that can change this delivery or its competing scope use."""
    from harness_package import author_lineage

    task_id = task['id']
    lineage = author_lineage(c, request)
    reviews = _review_returns(c, task_id, lineage)
    review_ids = [r['id'] for r,_ in reviews]
    author_returns = _returns(c, task_id, request_ids=lineage)
    selected_runs = [r['run_id'] for r,_ in reviews if r.get('run_id')]
    if returned and returned.get('run_id'):
        selected_runs.append(returned['run_id'])
    native = [dict(r) for r in c.execute('''SELECT id,outcome,stop_requested,native_identity_json FROM work_runs
        WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id''', (t._json(selected_runs),))]
    effects = c.execute('''SELECT max(e.id) FROM work_run_events e JOIN work_runs r ON r.id=e.run_id
        WHERE e.kind='delivery-evidence' AND r.request_id IN (SELECT value FROM json_each(?))''',
        (t._json(lineage),)).fetchone()[0]
    decisions = t._effective_dispositions(c, task_id, request_ids=lineage, scope_ids=scope_ids)
    accepted = _evidence_events(c, task_id, 'delivery-acceptance-recorded','disposition_id',[d['id'] for d in decisions])
    covered = set(scope_ids) | set(returned['coverage'] if returned else [])
    controls = _evidence_events(c,task_id,'delegation-decision','request_id',lineage)
    return {'task':{key:task[key] for key in ('id','state','owner_actor','sponsor_actor','plan_revision')},
        'request':request,'lineage':lineage,'contracts':effective_contract(c,request),
        'current_parent':contract(c,'task',task_id),
        'scopes':[{k:v for k,v in row.items() if k!='revision'} for row in _scope_rows(c,task) if row['scope_id'] in covered],
        'returns':author_returns,'opinion_returns':reviews,'native':native,'delivery_events_through':effects,
        'opinions':_evidence_events(c,task_id,'review-conclusion-indexed','return_id',review_ids),
        'dispositions':[t._decode(row) for row in decisions], 'acceptance':accepted,'delegation':controls}


def build_check(c,root,kind,ident,for_action,*,return_id=None,scope_ids=None,proposed=(),evidence=None,basis=None):
    if for_action not in ('dispatch','handoff','complete'):raise ValueError('Check boundary must be dispatch|handoff|complete.')
    subject=t._row(c,'work_tasks' if kind=='task' else 'work_requests',ident)
    task=subject if kind=='task' else t._row(c,'work_tasks',subject['task_id'])
    if for_action=='handoff':
        if kind=='request' and return_id is None:
            versions=_returns(c,task['id'],request_ids=[ident])
            if versions:return_id=versions[-1]['id']
    evidence=Evidence(root) if evidence is None else evidence
    checks=[];handoff_scopes={};delegations={}
    requests=[t._decode(subject)] if kind=='request' else [t._decode(r) for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id',(ident,))]
    if for_action=='dispatch':
        checks.append(_check('task_active','pass' if task['state']=='active' else 'fail','Original task state is '+task['state']))
        scope={r['scope_id']:r for r in _scope_rows(c,task) if not r.get('legacy_unindexed')}
        for q in requests:
            evidence.read(q['input_ref'])
            checks.append(_check(q['id']+'.brief','pass' if evidence.intact(q['input_ref']) else 'unknown','Read the complete original brief.',[q['input_ref']]))
            if q['kind']!='plan' and not q['independent']:
                checks.append(_check(q['id']+'.scientific_contract','not_applicable','Maintenance/consultation/report registration does not use a production contract.'));continue
            if q['independent']:
                from scientific_review.rounds import submission_ref
                frozen = submission_ref(c, q)
                if frozen != q['input_ref']:
                    packet = evidence.read(frozen)
                    for ref in (packet or {}).get('artifact_refs', []):
                        evidence.read(ref)
            effective=effective_contract(c,q)
            delegations[q['id']]=effective
            checks.extend(_contract_checks(evidence,effective['body'],effective['source_refs'],q['id']+'.contract.'))
            scopes=q['required_scope']
            valid_scope=(bool(scopes) and set(scopes).issubset(scope)) or (q['independent'] and q.get('parent_request_id') and not scopes)
            checks.append(_check(q['id']+'.scope','pass' if valid_scope else 'fail','New production delegation identifies existing scope IDs; an original independent frozen-packet review may retain historical unindexed parent scope.'))
            for sid in scopes:
                for ref in scope.get(sid,{}).get('source_refs',[]):evidence.read(ref)
            checks.append(_check(q['id']+'.responsibility','pass' if q['recipient_role'] in t.ORG_ROLES else 'fail','Original receiver responsibility; native sandbox/capability admission still runs at launch.'))
    elif for_action=='handoff':
        scope_rows={row['scope_id']:row for row in _scope_rows(c,task)}
        for q in requests:
            selected=[r for r in _returns(c,task['id'],request_ids=[q['id']]) if not return_id or r['id']==return_id]
            selected=selected if return_id else selected[-1:]
            if kind=='request' and basis is None:
                basis=_handoff_basis(c,task,q,selected[0] if selected else None,scope_ids or ())
            if not selected:checks.append(_check(q['id']+'.report','unknown','No original report selected; partial reports are still always accepted.'));continue
            parent=contract(c,'task',task['id'])
            child_endpoint=endpoint(effective_contract(c,q))
            for r in selected:
                prefix=r['id']+'.'
                child_checks=_delivery_checks(c,root,task,q,r,child_endpoint,evidence,prefix,proposed)
                checks.extend(child_checks)
                additional={}
                for sid in r['coverage'] if scope_ids is None else scope_ids:
                    if sid not in scope_rows:continue
                    required=endpoint(parent,scope_rows[sid])
                    achieved=child_endpoint
                    pending=[]
                    if child_endpoint and required and required!=child_endpoint:
                        if required not in additional:
                            scope_evidence=Evidence(root,observations=evidence._observed)
                            compared=_delivery_checks(c,root,task,q,r,required,scope_evidence,prefix,proposed)
                            withheld=[x for x in compared if x['status'] in ('unknown','fail')]
                            withheld.extend(
                                _check('scope_source','unknown',e.get('detail','Source unreadable'),[e['ref']])
                                for key,e in scope_evidence.sources.items()
                                if e['status']!='pass' and key not in scope_evidence.historical_gaps)
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
        deliveries={}
        for item in view['scope_items']:
            sid=item['scope_id']
            if kind=='request' and sid not in json.loads(subject['required_scope_json']):continue
            if item['state']=='dispositioned':
                evidence.read(item['disposition']['source_ref']);checks.append(_check(sid+'.scope_disposition','pass','Original delegator explicitly disposed this responsibility.',[item['disposition']['source_ref']]));continue
            checks.append(_check(sid+'.mapping','pass' if item['mapping_verified'] else 'unknown','Scope relations are verified.' if item['mapping_verified'] else 'Scope relations need explicit original-source verification.',item['source_refs']))
            metadata={d['disposition']['id']:d['acceptance'] for d in item['adoptions']}
            finals=_final_adoptions([d['disposition'] for d in item['adoptions']],metadata)
            required=item['endpoint']
            if kind=='request':required=endpoint(effective_contract(c,t._decode(subject))) or required
            def reached(decision):
                recorded=metadata[decision['id']]
                parent_endpoint=recorded.get('endpoint_by_scope',{}).get(sid)
                if kind=='task' and item['accepted_endpoint']=='submitted' and parent_endpoint=='saved_draft':
                    return 'submitted'
                return recorded.get('request_endpoint',parent_endpoint) if kind=='request' else parent_endpoint
            finals=finals[-1:] if finals and required and reached(finals[-1])==required else []
            checks.append(_check(sid+'.final_adoption','pass' if finals else 'unknown','Final owner adoption at the agreed endpoint; stage adoption leaves responsibility open.'))
            if not finals:continue
            d=finals[-1];r=all_returns[d['return_id']];q=t._decode(t._row(c,'work_requests',r['request_id']))
            evidence.read(d['evidence_ref'])
            key=r['id'],required
            if key not in deliveries:
                deliveries[key]=_delivery_checks(c,root,task,q,r,required,evidence,'')
            checks.extend({**row,'id':sid+'.'+row['id']} for row in deliveries[key])
        for q in requests:
            for row in t.pending_runs(c,q['id']):
                run=t._run_view(c,row)
                from execution_lease import observe_run
                observed=run.get('execution_observation') or (observe_run(root,run) if run.get('target_host')=='deepseek_harness' else {})
                clear=observed.get('state')=='exited' and not run.get('stop_requested')
                checks.append(_check(run['id']+'.execution','pass' if clear else 'unknown',
                    t._json({'execution':observed,'native_outcome':run['outcome'],
                        'detail':'OS exit does not settle native/data effects. Exact object effects are checked with the selected delivery.'})))
    for key,source in evidence.sources.items():
        if source['status']!='pass' and key not in evidence.historical_gaps:
            checks.append(_check('source:'+hashlib.sha256(t._json(source['ref']).encode()).hexdigest(),'unknown',source.get('detail','Source unreadable'),[source['ref']]))
    if basis is None:
        basis=_dispatch_basis(c,task,requests) if for_action=='dispatch' else _semantic_basis(c,task['id'])
    sources=sorted(evidence.sources.values(),key=lambda v:t._json(v['ref']))
    fingerprint=hashlib.sha256(t._json([kind,ident,for_action,return_id,scope_ids,basis,sources,checks,handoff_scopes]).encode()).hexdigest()
    missing=[check for check in checks if check['status'] in ('unknown','fail')]
    return {'schema':CHECK_SCHEMA,'subject':{'kind':kind,'id':ident,**({'return_id':return_id} if return_id else {})},
        'for':for_action,'ready':not missing,'checks':checks,'missing':missing,'sources':sources,'input_fingerprint':fingerprint,
        'scope_ids':scope_ids,**({'scope_endpoints':handoff_scopes} if handoff_scopes else {}),
        **({'effective_delegations':delegations} if delegations else {}),
        'authority':'Read-only evidence check; scientific adequacy/use and native write authorization remain with their owners.'}


def checked_report(c,root,kind,ident,for_action,*,supplied=None,return_id=None,scope_ids=None,proposed=(),evidence=None,basis=None):
    # Re-derive deterministic interpretation from original reports; no expensive
    # native validation is rerun. A hand-written ready/pass cannot override it.
    evidence=Evidence(root) if evidence is None else evidence
    report=build_check(c,root,kind,ident,for_action,return_id=return_id,scope_ids=scope_ids,
                        proposed=proposed,evidence=evidence,basis=basis)
    if supplied:
        given=json.loads(Path(supplied).read_bytes())
        if given != report and given != build_check(c,root,kind,ident,for_action,
                return_id=return_id,evidence=Evidence(root,observations=evidence._observed)):
            t._fail('stale_or_untrusted_check','Check is not this service result for the current exact inputs; regenerate work-check.',ident)
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
                'opinion_ref': item['opinion_ref'],
                'adopter': item.get('adopter'),
                'handling_refs': item['handling_refs'],
                'adoption_ref': item.get('adoption_ref'),
            } for item in detail['scientific_applications']]
            members[key] = {
                'return_id': return_id,
                'subject': detail['subject'],
                'candidate_ref': detail['candidate_ref'],
                'canonical_sha256':detail['canonical_sha256'],
                'author_report_refs': author_reports.get(return_id, []),
                'opinions': [{k: opinion[k] for k in ('return_id','conclusion','superseded','source_ref','reviewer','frozen_candidates')}
                             for opinion in detail['scientific_opinions']],
                'prior_opinions_with_changes': [{k: item[k] for k in ('return_id','opinion_ref','frozen_candidate')}
                                                for item in detail['prior_opinions_with_body_changes']],
                'lead_applications': accepted,
                'native_validation_coverage':detail['native_validation_coverage'],
                'checks': {},
            }
        members[key]['checks'][name] = {'status': row['status'], 'source_refs': row['source_refs']}
    return {'subject': report['subject'], 'ready': report['ready'], 'check_ref': ref,
            'members': list(members.values()), 'remaining_checks': remaining,
            'scope_endpoints':{ret:{sid:{k:v for k,v in status.items() if k!='pending'}
                                    for sid,status in scopes.items()}
                               for ret,scopes in report.get('scope_endpoints',{}).items()}}


@contextlib.contextmanager
def _verification_snapshot(root):
    from task_ledger.store import evidence_connection

    with contextlib.closing(t._connect(root,True)) as c:
        c.execute('BEGIN');t.ensure_schema(c)
        with evidence_connection(c):
            yield c


def check(root,kind,ident,for_action,*,summary=False):
    with _verification_snapshot(root) as c:
        report,ref=checked_report(c,root,kind,ident,for_action)
        return handoff_summary(report, ref) if summary else {**report,'check_ref':ref}


def _lifecycle(c,task):
    return c.execute("SELECT id FROM work_task_events WHERE task_id=? AND kind IN ('create','reopen') "
                     "ORDER BY id DESC LIMIT 1", (task['id'],)).fetchone()[0]


def prepare_decision(root,action,actor,payload):
    """Verify original content in a WAL read snapshot before asking for a writer."""
    with _verification_snapshot(root) as c:
        reader = Evidence(root)
        if action=='complete':
            task=t._row(c,'work_tasks',payload['task_id'])
            _owner(c,task,actor,maintenance=True)
            note=_ref(root,payload.get('evidence_ref'))
            prepared={'note':note,'lifecycle':_lifecycle(c,task),'reader':reader}
            if task['state']!='completed':
                basis=_semantic_basis(c,task['id'])
                report,ref=checked_report(c,root,'task',task['id'],'complete',evidence=reader,basis=basis)
                prepared.update(basis=basis,report=report,check_ref=ref)
                reader.read(ref)
        else:
            ret=t._row(c,'work_returns',payload.get('return_id'))
            request=t._decode(t._row(c,'work_requests',ret['request_id']))
            task=t._row(c,'work_tasks',request['task_id'])
            _owner(c,task,actor)
            note=t.evidence_ref(root,payload.get('evidence_ref'))
            scopes=t._list(payload.get('scope',[]),'Adopted scope IDs')
            returned=next(r for r in _returns(c,task['id'],request_ids=[request['id']]) if r['id']==ret['id'])
            applied=t._list(payload.get('apply_review_return_ids',[]),'Original independent reviews whose applicability the lead accepts')
            applications=[]
            if applied:
                if payload.get('disposition')!='adopt':
                    raise ValueError('Scientific applicability requires the task lead\'s explicit adopt judgment')
                applications=review_applications(c,root,task,request,returned,actor,applied,
                    payload.get('handling_refs',[]),reader=reader)
            prepared={'note':note,'reader':reader,'applications':applications,
                      'basis':_handoff_basis(c,task,request,returned,scopes)}
            if payload.get('acceptance')=='final':
                selected=set(scopes)
                declared={row['scope_id'] for row in _scope_rows(c,task) if row['mapping_verified']}
                if (request['kind']!='plan' or request['independent'] or not selected
                        or not selected.issubset(set(request['required_scope']) & declared)):
                    t._fail('coverage_required','Select exact original scope of this author assignment and identify its delivered evidence.',ret['id'])
                run=t._row(c,'work_runs',returned['run_id']) if returned.get('run_id') else None
                if task['state']!='active' or run and (run['stop_requested'] or run['outcome']=='stopped'):
                    t._fail('original_stopped','The exact original stop remains in force.',ret['id'])
                report,ref=checked_report(c,root,'request',request['id'],'handoff',return_id=ret['id'],
                    scope_ids=scopes,supplied=payload.get('checks_path'),proposed=applications,evidence=reader)
                if not report['ready']:
                    t._fail('delivery_not_ready','Final adoption pending; read '+ref['path'],ret['id'])
                checked=report.get('scope_endpoints',{}).get(ret['id'],{})
                members=handoff_summary(report,ref)['members']
                choice=[{sid:checked.get(sid,{}).get('checked_endpoint') for sid in scopes},
                    [row['candidate_ref'] for row in members]]
                prepared.update(report=report,check_ref=ref,choice=choice)
                prepared['candidate_subjects']=[{key:row[key] for key in ('subject','canonical_sha256')} for row in members]
                reader.read(ref)
        if not reader.intact(note):
            t._fail('check_input_changed','The original decision changed while it was being verified.',task['id'])
        return prepared


def verify_disposition(c,task,request,return_id,scopes,prepared):
    returned=next(r for r in _returns(c,task['id'],request_ids=[request['id']]) if r['id']==return_id)
    if _handoff_basis(c,task,request,returned,scopes)!=prepared['basis'] or not prepared['reader'].unchanged():
        t._fail('check_input_changed','Relevant original evidence or decisions changed during final adoption. Read the current original before applying it.',return_id)


def complete(c,root,actor,payload,prepared):
    if prepared is None:
        raise ValueError('Prepare the completion through the original whole-task entry point.')
    task=t._row(c,'work_tasks',payload['task_id']);_owner(c,task,actor,maintenance=True)
    note=prepared['note']
    lifecycle=_lifecycle(c,task)
    if lifecycle!=prepared['lifecycle']:
        t._fail('check_input_changed','The original task was reopened during the completion check.',task['id'])
    prior=c.execute("SELECT * FROM work_task_events WHERE task_id=? AND kind='complete' AND id>=? ORDER BY id LIMIT 1",(task['id'],lifecycle)).fetchone()
    if task['state']=='completed' and prior:
        saved=json.loads(prior['payload_json']);return saved.get('result',t._decode(task))
    if prepared.get('basis')!=_semantic_basis(c,task['id']) or not prepared['reader'].unchanged():
        t._fail('check_input_changed','Relevant original evidence or decisions changed during completion.',task['id'])
    stop=None
    if task['state'] in ('paused','cancelled'):
        stops=_events(c,task['id'],'pause' if task['state']=='paused' else 'cancel')
        stop=stops[-1] if stops else {'status':'original stop source needs readback'}
    report,ref=prepared['report'],prepared['check_ref']
    group=[task['id'],lifecycle,t.native_key(actor),note,report['input_fingerprint']]
    kind='complete' if report['ready'] else 'completion-pending'
    ident=kind+':'+hashlib.sha256(t._json(group).encode()).hexdigest()
    existing=c.execute('SELECT payload_json FROM work_task_events WHERE task_id=? AND kind=? AND json_extract(payload_json,\'$.id\')=?',(task['id'],kind,ident)).fetchone()
    if existing:return json.loads(existing[0])['result']
    if report['ready']:task=t._touch(c,task,state='completed')
    result={**t._decode(task),'status':'completed' if report['ready'] else 'completion_pending',
            'completion_id':ident,'lifecycle':lifecycle,'check_ref':ref,'missing':report['missing']}
    if stop:result.update(stop_preserved=True,note_ref=note,stop_basis=stop)
    t._event(c,task,kind,actor,{'id':ident,'lifecycle':lifecycle,'evidence_ref':note,'input_fingerprint':report['input_fingerprint'],
                              'check_ref':ref,'remaining':report['missing'],'result':result})
    return result
