"""Current views and complete-block context rendering over one scope projection."""
import contextlib
import hashlib
import json
from pathlib import Path
import shlex
import time
import harness_tasks as t
import harness_scope as scope
from safe_diagnostics import safe_value

ROOT=Path(__file__).resolve().parent.parent
HOOK_INLINE_BUDGET_BYTES=8192
DUTIES={
 'global_main':'总主从原建设计划接续，按现实需要和现库覆盖决定下一笔投入。见 roles/global-main.md。',
 'task_lead':'主审用原生子智能体协助明确制作计划，按整包用途派工、采用成果、安排余项及平台交付。只剩等待时必须一次休眠满 30 分钟。见 roles/task-lead.md。',
 'executor':'作者在本包边界自主制作、自查、独审返修，达到子终点后交清相邻接口；主审承接父差额。',
 'scientific_reviewer':'独审核准确候选与声明用途，将科学意见原文交作者。',
 'maintainer':'维护者沿原计划和维护board亲自修共享能力并验证原入口。',
 'unknown':'读取原职责、完整委派与当前责任，继续承担原目标。'}


def command(root,task_id,*,detail=False):
    return shlex.join([str(Path(root)/'.venv/bin/python'),str(Path(root)/'bin/harness.py'),'--root',str(root),'work-show','--id',task_id]+(['--detail'] if detail else []))


def _role(c,actor,request=None):
    if request:return 'scientific_reviewer' if request['independent'] else request['recipient_role']
    roles=t._roles(c,actor) if actor else set()
    for role in ('maintainer','global_main','task_lead','executor'):
        if role in roles:return role
    if actor:
        observed=set()
        for row in c.execute('''SELECT r.native_identity_json,q.independent,q.recipient_role
            FROM work_runs r JOIN work_requests q ON q.id=r.request_id
            WHERE json_extract(r.native_identity_json,'$.id')=?''',(actor['id'],)):
            if t.native_key(row['native_identity_json'])==t.native_key(actor):
                observed.add('scientific_reviewer' if row['independent'] else row['recipient_role'])
        if len(observed)==1:return observed.pop()
    return 'unknown'


def actor_tasks(c,actor,request_id=None):
    """Read-only identity lookup; never invokes verified_actor or enrolls a process."""
    if request_id:
        request=t._row(c,'work_requests',request_id)
        if not actor:return [],None
        # The binding must already be an exact observed native execution.
        matched=any(t.native_key(row[0])==t.native_key(actor) for row in c.execute(
            'SELECT native_identity_json FROM work_runs WHERE request_id=? AND native_identity_json IS NOT NULL',(request_id,)))
        if not matched:return [],None
        return [t._row(c,'work_tasks',request['task_id'])],request
    rows=[r for r in c.execute("SELECT * FROM work_tasks WHERE state NOT IN ('completed','cancelled') ORDER BY id") if actor and t.participates(c,r,actor)]
    return rows,None


def _history(c,task_id,limit,offset):
    count=c.execute('SELECT count(*) FROM work_task_events WHERE task_id=?',(task_id,)).fetchone()[0]
    rows=[t._decode(e) for e in c.execute('SELECT * FROM work_task_events WHERE task_id=? ORDER BY id DESC LIMIT ? OFFSET ?',(task_id,limit,offset))]
    return {'total':count,'shown':len(rows),'offset':offset,'next_offset':offset+limit if offset+limit<count else None,'events':rows}


def task_view(c,root,task_id,*,request_id=None,limit=10,offset=0,detail=False):
    task=t._decode(t._row(c,'work_tasks',task_id));projected=scope.projection(c,root,task_id)
    all_requests=[t._decode(r) for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id',(task_id,))]
    requests=[q for q in all_requests if q['id']==request_id] if request_id else all_requests[offset:offset+limit]
    ids={q['id'] for q in requests}
    returns=[r for r in scope._returns(c,task_id) if r['request_id'] in ids]
    runs=[t._run_view(c,r) for r in c.execute('SELECT r.* FROM work_runs r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY r.created_at,r.id',(task_id,)) if r['request_id'] in ids]
    if not detail:
        latest={}
        for run in runs:latest[run['request_id']]=run
        runs=list(latest.values())
    decisions=[t._decode(r) for r in t._effective_dispositions(c,task_id)]
    history=_history(c,task_id,limit if not detail else max(1,c.execute('SELECT count(*) FROM work_task_events WHERE task_id=?',(task_id,)).fetchone()[0]),offset if not detail else 0)
    if history['next_offset'] is not None:
        history['next_page_command']=command(root,task_id)+' --limit '+str(limit)+' --offset '+str(history['next_offset'])
    count=1 if request_id else len(all_requests)
    from review_delivery import projection as review_projection
    return {'task':task,'scope':projected,'requests':requests,'returns':returns,'runs':runs,'decisions':decisions,
        'reviews':{q['id']:review_projection(c,root,q) for q in requests if not q['independent']},
        'pagination':{'total':count,'shown':len(requests),'offset':offset,'next_offset':offset+limit if not request_id and offset+limit<count else None,
            'filter':{'request_id':request_id},'full_scope_total':projected['total']},
        'history':history,'original_brief':task['original_ref'],'current_plan':task['plan_ref'],
        'detail_command':command(root,task_id,detail=True),'current_command':command(root,task_id),
        'meaning':'Current scope is complete. Lists are complete-entry pages; read original conclusions with their conditions before scientific use or authorization.'}


def read(root,*,task_id=None,request_id=None,limit=10,offset=0,detail=False):
    root=Path(root).resolve()
    if not 1<=limit<=100 or offset<0:raise ValueError('Use limit 1..100 and nonnegative offset.')
    if not (root/'.control/harness.sqlite').is_file():return {'status':'UNCONFIGURED','tasks':[]}
    with contextlib.closing(t._connect(root,True)) as c:
        c.execute('BEGIN');t.ensure_schema(c)
        try:actor=t.verified_actor(root)
        except ValueError:actor=None
        role=_role(c,actor)
        if request_id:
            request=t._row(c,'work_requests',request_id)
            if task_id and task_id!=request['task_id']:raise ValueError('The selected request belongs to a different task.')
            task_id=request['task_id']
        result={'status':'configured','actor':actor,'roles':sorted(t._roles(c,actor)) if actor else []}
        if task_id:return {**result,'work':task_view(c,root,task_id,request_id=request_id,limit=limit,offset=offset,detail=detail)}
        rows=list(c.execute("SELECT * FROM work_tasks WHERE state NOT IN ('completed','cancelled') ORDER BY id"))
        tasks=[{**t._decode(r),'detail_command':command(root,r['id'])} for r in rows[offset:offset+limit]]
        return {**result,'tasks':tasks,'pagination':{'total':len(rows),'shown':len(tasks),'offset':offset,'next_offset':offset+limit if offset+limit<len(rows) else None}}


def _basis(c,task_id):
    basis=scope._semantic_basis(c,task_id)
    basis['review_deliveries']=[json.loads(row[0]) for row in c.execute(
        "SELECT e.payload_json FROM work_run_events e JOIN work_runs r ON r.id=e.run_id JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? AND e.kind='review-delivered' ORDER BY e.id",(task_id,))]
    execution={}
    for row in c.execute("SELECT e.run_id,e.payload_json FROM work_run_events e JOIN work_runs r ON r.id=e.run_id JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? AND json_type(e.payload_json,'$.execution_observation')='object' ORDER BY e.id",(task_id,)):
        value=json.loads(row['payload_json'])['execution_observation']
        execution[row['run_id']]={k:value.get(k) for k in ('state','native_result_state')}
    basis['execution_observations']=execution
    # Progress counters, turn IDs and observer timestamps do not constitute a
    # new responsibility/terminal fact for the model.
    for run in basis['runs']:
        if run['native_identity_json']:
            native=json.loads(run['native_identity_json']);native.pop('turn_id',None)
            run['native_identity_json']=t._json(native)
    return basis


def bundle(root,actor=None,*,request_id=None,timeout=1):
    root=Path(root).resolve();started=time.monotonic()
    with contextlib.closing(t._connect(root,True,timeout=0.1)) as c:
        c.set_progress_handler(lambda:time.monotonic()-started>timeout,500)
        c.execute('BEGIN');t.ensure_schema(c)
        tasks,request=actor_tasks(c,actor,request_id)
        role=_role(c,actor,request)
        if role=='unknown' and tasks:
            if all(t.native_key(row['sponsor_actor'])==t.native_key(actor) for row in tasks):role='global_main'
            elif all(t.native_key(row['owner_actor'])==t.native_key(actor) for row in tasks):role='task_lead'
        views=[];basis=[]
        for task in tasks:
            p=scope.projection(c,root,task['id'])
            # Only ledger facts here: no report scans, auth files or remote calls.
            reqs=[t._decode(q) for q in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY id',(task['id'],))]
            decision_count=c.execute('SELECT count(*) FROM work_dispositions d JOIN work_returns r ON r.id=d.return_id JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=?',(task['id'],)).fetchone()[0]
            view={'task':t._decode(task),'scope':p,'request_binding':request['id'] if request else None,
                'request_brief':json.loads(request['input_ref_json']) if request else None,
                'request_contract':scope.contract(c,'request',request['id']) if request else None,
                'original_brief':json.loads(task['original_ref_json']),'current_plan':json.loads(task['plan_ref_json']),
                'detail_command':command(root,task['id']),
                'important':{'delegations':len(reqs),'returns':c.execute('SELECT count(*) FROM work_returns r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=?',(task['id'],)).fetchone()[0],
                    'adoptions':decision_count,'stopped_or_paused_scope':p['filters']['stopped_or_paused']['total'],'unknown_execution_scope':p['filters']['execution_unknown']['total']}}
            from review_delivery import projection as review_projection
            reviews=[r for q in reqs if not q['independent'] and (not request or q['id']==request['id']) for r in review_projection(c,root,q)]
            view['reviews']=list({r['review_id']:r for r in reviews}.values())
            view['important']['review_ready']=sum(r['state']=='ready' for r in view['reviews'])
            view['important']['review_delivered']=sum(bool(r['deliveries']) for r in view['reviews'])
            views.append(view);basis.append(_basis(c,task['id']))
        safe=safe_value({'role':role,'tasks':views,'actor':actor,'request_id':request_id,
             'workspace':str(root),'task_count':len(views)})
        safe['snapshot_hash']=hashlib.sha256(t._json(safe_value([role,request_id,basis])).encode()).hexdigest()
        safe['fact_revision']=safe['snapshot_hash']
        if time.monotonic()-started>timeout:raise TimeoutError('Local snapshot exceeded one second')
        return safe


def _counts_line(p):
    labels={'unassigned':'未安排','active':'已承担待交付','accepted':'已验收','submitted':'本方已提交','dispositioned':'范围已处置','unverified':'待核'}
    lines=[]
    for kind,counts in p['counts'].items():
        name={'requirement':'需求','deliverable':'交付责任','unclassified':'未分类范围'}[kind]
        lines.append(name+str(counts['total'])+'（'+', '.join(label+str(counts.get(key,0)) for key,label in labels.items())+'）')
    return '；'.join(lines) or '范围尚未结构化登记（不能据此认为没有余项）'


def _remaining_block(view):
    items=[]
    for item in view['scope']['scope_items']:
        if not item['remaining']:continue
        # This entire record is a block: adoption statements keep their complete
        # conditions and exact report/version, or the block is not inlined.
        items.append({k:item[k] for k in ('scope_id','label','state','endpoint','remaining_reasons','responsibility','coverage_returns','coverage_unindexed','adoptions','execution')})
    return json.dumps(items,ensure_ascii=False,indent=2)


def navigation(value):
    totals={}
    for view in value.get('tasks',[]):
        for kind,counts in view['scope']['counts'].items():
            dest=totals.setdefault(kind,{'total':0,'remaining':0})
            dest['total']+=counts['total'];dest['remaining']+=counts['remaining']
    return ('当前职责：'+value.get('role','unknown')+'。'+DUTIES.get(value.get('role'),DUTIES['unknown'])+'\n'
        +'关联任务总数：'+str(value.get('task_count',0))+'；全量范围与余项：'+json.dumps(totals,ensure_ascii=False)+'。\n'
        +'完整任务基础卡未展开；本卡是预览，未展示不代表没有条件或限制。\n'
        +'正式工作区：'+str(ROOT)+'\n'
        +'在该目录读取全部任务：.venv/bin/python bin/harness.py work-list（按 next_offset 遍历，再用 work-show --id 读准确任务）。')


def render(value,*,budget=HOOK_INLINE_BUDGET_BYTES):
    value=safe_value(value)
    tasks=value.get('tasks',[]);role=value.get('role','unknown')
    if not tasks:return navigation(value)
    base=['当前职责：'+role+'。'+DUTIES.get(role,DUTIES['unknown']), '原事实修订：'+value['fact_revision']]
    blocks=[]
    for index,view in enumerate(tasks,1):
        task=view['task'];p=view['scope'];tag='T'+str(index)
        filters=p['filters']
        endpoint=(p.get('contract') or {}).get('body',{}).get('endpoint') or '待核'
        child_endpoint=(view.get('request_contract') or {}).get('body',{}).get('endpoint') or '待核'
        base.append(tag+' task='+task['id']+(' request='+view['request_binding'] if view.get('request_binding') else '')+
                    '；父任务声明终点='+endpoint+('；子请求终点='+child_endpoint if view.get('request_binding') else '')+'。')
        base.append(_counts_line(p)+'；全部未达终点余项='+str(p['remaining_count'])+'。')
        base.append('余项筛选：无承接'+str(filters['unassigned']['total'])+'，尚无明确覆盖'+str(filters['no_explicit_coverage']['total'])+
                    '，有返回待最终采用'+str(filters['coverage_pending_adoption']['total'])+'，执行结束但未交付'+str(filters['execution_ended_incomplete']['total'])+
                    '，覆盖待核'+str(filters['coverage_unindexed']['total'])+'（可重叠，不可相加）。')
        base.append('重要事实类别/数量：'+json.dumps(view['important'],ensure_ascii=False)+'；明细按块展开或见详情。')
        base.append(tag+'完整视图/原委派入口：'+view['detail_command'])
        important={'task_state':task['state'],'stopped_or_paused_scope_ids':filters['stopped_or_paused']['scope_ids'],
            'reviews':view.get('reviews',[]),
            'execution_unknown_scope_ids':filters['execution_unknown']['scope_ids'],'source':view['current_plan'],
            'meaning':'停止/未知仅限制准确作用域；不得据此恢复原生产或重放未知数据动作。声明终点不授予远端权限。'}
        goal={'original_goal':task['summary'],'contract':p['contract'],'original_brief':view['original_brief'],'current_plan':view['current_plan']}
        if view.get('request_binding'):
            goal['bound_request']={'id':view['request_binding'],'original_brief':view.get('request_brief'),
                                   'contract':view.get('request_contract')}
        blocks.extend([(0,index,tag+'重要变化及作用域',len(important['stopped_or_paused_scope_ids'])+len(important['execution_unknown_scope_ids']),json.dumps(important,ensure_ascii=False,indent=2)),
            (1,index,tag+'原目标、验收及范围边界',1,json.dumps(goal,ensure_ascii=False,indent=2)),
            (2,index,tag+'全部未达终点余项',p['remaining_count'],_remaining_block(view))])
    base.append('这是预览。完整目标、适用条件与全部余项见准确入口；“已承担”不表示仍运行，未展开不表示没有限制。')
    blocks.sort(key=lambda b:(b[0],b[1]))
    def note(block):return block[2]+'未展开（完整数量'+str(block[3])+'）；见 '+('T'+str(block[1]))+' 的完整视图命令。'
    base_text='\n'.join(base)
    if len((base_text+'\n'+'\n'.join(note(b) for b in blocks)).encode())>budget:
        return navigation(value)
    expanded=[];blocked=False
    for i,block in enumerate(blocks):
        full=block[2]+'：\n'+block[4]
        remaining_notes=[note(b) for b in blocks[i+1:]]
        candidate='\n'.join([base_text,*expanded,full,*remaining_notes])
        if not blocked and len(candidate.encode())<=budget:expanded.append(full)
        else:blocked=True;expanded.append(note(block))
    result='\n'.join([base_text,*expanded])
    # A malformed/exceptional construction switches to fixed navigation, never
    # a prefix, split sentence or multiple injections.
    if len(result.encode())>budget:result=navigation(value)
    if len(result.encode())>HOOK_INLINE_BUDGET_BYTES:raise ValueError('Fixed navigation exceeds local context budget')
    return result


def export(root,path,value):
    from execution_lease import atomic_json
    from workspace_files import safe_path,is_credential_path,PRIVATE_CONTROL_PARTS
    output=safe_path(root,path,must_exist=False)
    if is_credential_path(output.relative_to(Path(root).resolve())) or any(p in PRIVATE_CONTROL_PARTS for p in output.relative_to(Path(root).resolve()).parts) or output.suffix in ('.sqlite','.db'):
        raise ValueError('Output cannot overwrite credential, control or database material.')
    atomic_json(output,value)
    return {'output':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
        'counts':value.get('counts') or value.get('scope',{}).get('counts') or value.get('pagination') or value.get('work',{}).get('scope',{}).get('counts') or value.get('current',{}).get('work',{}).get('scope',{}).get('counts'),
        'detail_command':value.get('detail_command') or value.get('work',{}).get('detail_command') or value.get('current',{}).get('work',{}).get('detail_command'),
        'meaning':'Complete query JSON saved; read this original output. No underlying data write was replayed.'}


def _package_native_metadata(root,run):
    """Expand exact original technical facts for the package reader only.

    The short Hook stays ledger-only. This full reader verifies original bytes;
    absent/changed optional metadata does not hide the run, report or adoption.
    """
    from harness_package import launch_spec
    value={'goalObservation':None,'continuationRef':None,'issues':[]}
    try:
        value['continuationRef']=launch_spec(root,run).get('continuation_ref')
    except (ValueError,OSError,KeyError,TypeError) as exc:
        value['issues'].append({'runId':run['id'],'kind':'launch_metadata','reason':str(exc)})
    if run.get('latest_receipt'):
        try:
            ref=t.evidence_ref(root,run['latest_receipt'])
            receipt=json.loads(Path(ref['path']).read_bytes())
            if receipt.get('run_id')!=run['id'] or receipt.get('request_id')!=run['request_id']:
                raise ValueError('Original receipt does not bind this exact run and request')
            result=receipt.get('result')
            if isinstance(result,dict):
                observation=result.get('native_goal_observation')
                if isinstance(observation,dict):
                    native=run.get('native_identity') or receipt.get('native_identity') or {}
                    if (observation.get('sessionId')!=native.get('id')
                            or str(observation.get('turn'))!=str(native.get('turn_id'))
                            or observation.get('source') not in ('dsh_live_agent','codex_native_rpc')):
                        raise ValueError('Goal observation does not bind this exact native Session/turn')
                    value['goalObservation']={'receipt':ref,'observedAt':'invocation_end',**observation}
                else:
                    persisted=result.get('native_session_observation')
                    if isinstance(persisted,dict) and isinstance(persisted.get('native_goal'),dict):
                        value['goalObservation']={'source':persisted.get('source'),
                            'observedAt':'receipt','receipt':ref,'goal':persisted['native_goal'],
                            'qualification':'Persisted Goal state only; live activation is not inferred.'}
        except (ValueError,OSError,KeyError,TypeError) as exc:
            value['issues'].append({'runId':run['id'],'kind':'native_metadata',
                'ref':run['latest_receipt'],'reason':str(exc)})
    return value


def package_snapshot(root,task_id=None):
    import harness_finite_work as finite
    with contextlib.closing(t._connect(root,True)) as c:
        c.execute('BEGIN');t.ensure_schema(c)
        tasks=list(c.execute('SELECT * FROM work_tasks'+(' WHERE id=?' if task_id else " WHERE state NOT IN ('completed','cancelled')")+' ORDER BY id',(task_id,) if task_id else ()))
        packages=[];scopes=[];unassigned=[]
        for task in tasks:
            p=scope.projection(c,root,task['id']);scopes.append(p)
            configs=finite.request_configs(c,task['id']);groups={}
            all_requests={r['id']:t._decode(r) for r in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id',(task['id'],))}
            for request in all_requests.values():
                package_id=finite._package_id(request,configs)
                if not package_id:unassigned.append(request);continue
                original=all_requests[package_id];home=Path(original['input_ref']['path']).parent
                group=groups.setdefault(package_id,{'id':task['id']+':'+package_id,'package':package_id,'packageRoot':str(home),
                    'coordinateSource':'original_request','taskId':task['id'],'currentPlan':json.loads(task['plan_ref_json']),
                    'taskState':task['state'],'requestIds':[],'programs':[],'stages':[],'returns':[],'decisions':[],
                    'scope_ids':original['required_scope'],'scope':{'task_id':task['id'],'parent_counts':p['counts'],
                        'remaining_scope':[sid for sid in original['required_scope'] if sid in p['remaining_scope']]},'evidenceIssues':[]})
                if len(group['requestIds'])==0:
                    try:
                        ref=t.evidence_ref(root,original['input_ref'])
                        try:body=json.loads(Path(ref['path']).read_bytes())
                        except ValueError:body={}
                        coordinate=body.get('package_ref',{}) if isinstance(body,dict) else {}
                        if isinstance(coordinate,dict) and coordinate:
                            group.update(package=coordinate.get('name') or package_id,packageRoot=coordinate.get('root') or str(home),coordinateSource='brief')
                    except (ValueError,OSError) as exc:
                        group['evidenceIssues'].append({'ref':original['input_ref'],'reason':str(exc),'meaning':'Original coordinate not expanded; use its exact original reader.'})
                group['requestIds'].append(request['id'])
                agreement=scope.contract(c,'request',request['id'])
                group['stages'].append({'requestId':request['id'],'declared':None,'endpoint':(agreement or {}).get('body',{}).get('endpoint'),
                    'brief':request['input_ref'],'independent':bool(request['independent']),'createdAt':request['created_at']})
                row=c.execute('SELECT * FROM work_runs WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',(request['id'],)).fetchone()
                if row:
                    run=t._run_view(c,row);native=run['native_identity'] or {}
                    metadata=_package_native_metadata(root,run)
                    group['evidenceIssues'].extend(metadata['issues'])
                    group['programs'].append({'runId':run['id'],'requestId':request['id'],'operation':run['operation'],
                        'independent':bool(request['independent']),'sessionId':native.get('id'),'nativeStatus':run['outcome'],
                        'stopRequested':bool(run['stop_requested']),'updatedAt':run['updated_at'],'receipt':run['latest_receipt'],
                        'goalObservation':metadata['goalObservation'],'continuationRef':metadata['continuationRef']})
            for group in groups.values():
                from review_delivery import projection as review_projection
                group['reviews']=list({r['review_id']:r for qid in group['requestIds'] if not all_requests[qid]['independent']
                    for r in review_projection(c,root,all_requests[qid])}.values())
                group['returns']=[{'id':r['id'],'requestId':r['request_id'],'createdAt':r['created_at'],'coverage':r['coverage'],
                    'resultRef':r['result_ref'],'artifacts':r['artifact_refs'],'contribution':r['summary'],'stage':None,
                    'structuredSummaryAvailable':False} for r in scope._returns(c,task['id']) if r['request_id'] in group['requestIds']]
                return_ids={r['id'] for r in group['returns']}
                acceptance={e['payload']['disposition_id']:e['payload'] for e in scope._events(c,task['id'],'delivery-acceptance-recorded')}
                group['decisions']=[{**t._decode(d),'acceptance':acceptance.get(d['id'],{'acceptance':None,'meaning':'验收层级待核'})} for d in t._effective_dispositions(c,task['id']) if d['return_id'] in return_ids]
                packages.append(group)
        return {'source':'original_work_task_store','packages':packages,'unassigned':unassigned,'task_scopes':scopes,
            'counts':{'tasks':len(tasks),'packages':len(packages),'scope_total':sum(p['total'] for p in scopes),'remaining':sum(p['remaining_count'] for p in scopes)},
            'detail_command':'python3 bin/harness.py work-packages'+(' --task '+shlex.quote(task_id) if task_id else '')+' --detail'}
