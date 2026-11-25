"""Local orchestration of the existing single-root native review operation."""
from contextlib import closing, contextmanager
import json
from pathlib import Path

import delivery_evidence as evidence
from task_ledger import decisions
import harness_tasks as tasks
from harness_wave import _selected, _valid
from dataset_checks.references import nested_references
from scientific_review.delta import _reference_key

APPROVE = 'review-batch-submit'
INDEPENDENT_USE = 'review-independent-use'
SUBMISSION = 'dataset.review_submission'
INTENT = 'review-batch-intent'
RESULT = 'review-batch-result'
NATIVE_KIND = {'lifecyclemodel': 'model'}
TABLES = {'processes':'process','flows':'flow','sources':'source','contacts':'contact','lifecyclemodels':'model'}
NATIVE_SCOPE = 'native_business_related'
PENDING = ('unknown','accepted_readback_pending','accepted_effect_unverified')


def _identity(value):
    if not isinstance(value, dict):
        raise ValueError('Select the precise native type, UUID and version')
    result = tuple(tasks._text(value.get(key), 'Review target '+key) for key in ('type','id','version'))
    from dataset_checks.references import UUID, VERSION
    if result[0] not in ('process','flow','source','model','contact') or not UUID.fullmatch(result[1]) or not VERSION.fullmatch(result[2]):
        raise ValueError('Use the precise native type, UUID and version')
    return result


def _key(identity):
    return '/'.join(identity)


def _load(root, ref):
    return json.loads(Path(tasks.evidence_ref(root,ref)['path']).read_bytes())


def independent_use(target, consumers, original_use=None, *, completeness='bounded'):
    """A visible exact reference or the lead's existing independent-use decision."""
    hits = []
    for ref, body in consumers:
        body = evidence.canonical_body(body)
        consumer = evidence.body_identity(body)
        if not consumer or consumer == target:
            continue
        for pointer, related in nested_references(body):
            key = _reference_key(related)
            if (NATIVE_KIND.get(key[0],key[0]),*key[1:]) == target:
                hits.append({'consumer':consumer,'consumer_ref':ref,'pointer':pointer})
    status = ('ready' if target[0] not in ('source','flow') or hits or original_use else
              'usage_missing' if completeness == 'bounded' else 'consumer_coverage_unknown')
    return {'state':status,'known_consumers':hits,'independent_use':original_use,
            'coverage':completeness,'external_consumers':'unknown'}


def decision_details(c, root, task, actor, selected, document):
    if not selected.get('object'):
        raise ValueError('Select the exact target using type/UUID/version')
    parts = selected['object'].split('/')
    if len(parts)!=3:
        raise ValueError('Select the exact target using type/UUID/version')
    target = _identity(dict(zip(('type','id','version'), parts)))
    if document['subject'] == INDEPENDENT_USE:
        if target[0] not in ('source','flow'):
            raise ValueError('The independent use decision applies to an exact Source or Flow')
        return {}
    details = document.get('details') or {}
    if selected.get('action') not in ('submit','resubmit') or not selected.get('account') or not selected.get('project'):
        raise ValueError('Authorize the exact native action, account and project')
    required = ('operator','authorized_related') if document['subject']==SUBMISSION else (
        'request_ref','candidate_ref','adoption_id','operator','authorized_related')
    if not all(key in details for key in required):
        raise ValueError('The exact root grant requires '+', '.join(required))
    operator = tasks._actor(root,details['operator'],observed=True)
    related = tasks._list(details['authorized_related'],'Authorized related type/UUID/version')
    for member in related:
        fields = member.split('/')
        if len(fields)!=3:
            raise ValueError('Use exact type/UUID/version for each related object')
        _identity(dict(zip(('type','id','version'),fields)))
    if document['subject']==SUBMISSION:
        if document['statement']!='allow' or set(selected)!={'task','object','action','account','project'}:
            raise ValueError('Authorize only an exact root, action, account, project and named operator')
        result = {'operator':operator,'authorized_related':sorted(related)}
        if set(details)==set(required):
            return result
        if (set(details)!=set(required)|{'related_policy','adoption_id','request_ref'}
                or details.get('related_policy')!=NATIVE_SCOPE or related):
            raise ValueError('Native business-related authorization requires an exact adoption and request, with an empty exact allow-list')
        original = tasks._row(c,'work_dispositions',details['adoption_id'])
        if original['disposition']!='adopt' or details['adoption_id'] not in {
                row['id'] for row in tasks._effective_dispositions(c,task['id']) if row['disposition']=='adopt'}:
            raise ValueError('Select a current adoption of this task')
        ref = tasks.evidence_ref(root,details['request_ref'])
        native = _load(root,ref)
        targets = native.get('targets',[])
        if (len(targets)!=1 or _identity(targets[0])!=target or native.get('expectedActor')!=selected['account']
                or native.get('includeRelated') is False):
            raise ValueError('The original request must select the authorized root and account for business-related submission')
        return {**result,'related_policy':NATIVE_SCOPE,'adoption_id':original['id'],'request_ref':ref}
    refs = [tasks.evidence_ref(root,ref) for ref in details.get('consumer_refs',[])]
    return {'request_ref':tasks.evidence_ref(root,details['request_ref']),
            'candidate_ref':tasks.evidence_ref(root,details['candidate_ref']),
            'adoption_id':tasks._text(details['adoption_id'],'Original adoption id'),
            'operator':operator,'authorized_related':sorted(related),'consumer_refs':refs}


def _candidate_documents(c,root,task,adoption):
    from harness_scope import _evidence_events,_returns
    returned=tasks._row(c,'work_returns',adoption['return_id'])
    value=next(x for x in _returns(c,task['id'],request_ids=[returned['request_id']]) if x['id']==returned['id'])
    events=_evidence_events(c,task['id'],'delivery-acceptance-recorded','disposition_id',[adoption['id']])
    metadata = events[-1]['payload'] if events else {}
    references = value['artifact_refs']
    exact = metadata.get('candidate_refs') or evidence.explicit_selection(references) or references
    for ref in exact:
        raw=evidence._json(root,ref)
        if isinstance(raw,dict) and evidence._read_success(raw):
            raw=(raw.get('data') or {}).get('content')
        body=evidence.canonical_body(raw)
        if evidence.body_identity(body):
            yield ref,body


def _selection(c,root,task,actor,entry,native_driver):
    from task_ledger.sessions import reference
    if not isinstance(entry,dict) or set(entry)-{'adoption','request','use'}:
        raise ValueError('Select an original adoption and native request file, with an optional original-use locator')
    path=entry.get('request')
    ref=reference(root,path) if isinstance(path,str) else tasks.evidence_ref(root,path)
    native=_load(root,ref)
    if not isinstance(native,dict) or not isinstance(native.get('targets'),list) or len(native['targets'])!=1:
        raise ValueError('Select one exact original native request per root')
    target=_identity(native['targets'][0])
    account=tasks._text(native.get('expectedActor'),'Original native account')
    tasks._text(native.get('expectedEnvironment'),'Original native environment')
    request_id=tasks._text(native.get('requestId'),'Stable native requestId')
    key=_key(target)
    chosen={'object':key,'request_id':request_id,'adoption_id':entry.get('adoption'),
            'account':account,'environment':native['expectedEnvironment'],
            'requested_scope':'root_only' if native.get('includeRelated') is False else 'business_related',
            'scope_bound_at_commit':native_driver.binds_related_scope_at_commit,
            'platform_scope_unchecked':not native_driver.binds_related_scope_at_commit,
            'native_root_only_supported':native_driver.binds_related_scope_at_commit}
    existing=c.execute('''SELECT payload_json FROM work_task_events WHERE task_id=? AND kind=?
        AND json_extract(payload_json,'$.request_id')=? AND json_extract(payload_json,'$.account')=? ORDER BY id DESC''',
                       (task['id'],INTENT,request_id,account)).fetchall()
    for row in existing:
        recorded=json.loads(row[0])
        binding={**chosen,'account':account,'project':recorded['project']}
        if not recorded.get('action') or not native_driver.matches(binding):
            continue  # Old attempts are still recoverable via their stored permission.
        if (recorded['object']!=key or recorded['request_sha256']!=ref['sha256']
                or recorded['adoption_id']!=entry.get('adoption')):
            raise ValueError('The stable requestId already identifies a different original operation')
        if not tasks.participates(c,task,actor) and tasks.native_key(recorded['authorized_operator'])!=tasks.native_key(actor):
            raise ValueError('Read back this original operation as its named operator or an original participant')
        try:
            body=_load(root,recorded['frozen_candidate_ref'])
        except (OSError,ValueError,KeyError):
            body=None  # Preserve the original attempt; only exact body evidence can finish readback.
        return binding,native,target,body,ref,recorded,None,[]
    adoptions=[row for row in tasks._effective_dispositions(c,task['id']) if row['disposition']=='adopt']
    original=next((row for row in adoptions if row['id']==entry.get('adoption')),None)
    if not original:
        raise ValueError('Select a current original adoption in this task')
    exact=[(candidate,body) for candidate,body in _candidate_documents(c,root,task,original) if evidence.body_identity(body)==target]
    if not exact or any(not evidence.same_review_body(exact[0][1],body) for _,body in exact[1:]):
        raise ValueError('The original adoption must resolve one unambiguous complete body for this version')
    candidate,body=exact[0]
    chosen['author_request']=tasks._row(c,'work_returns',original['return_id'])['request_id']
    references = set()
    for _, reference in nested_references(body):
        kind, identifier, version = _reference_key(reference)
        kind = NATIVE_KIND.get(kind,kind)
        if kind in TABLES.values() and identifier and version:
            try:
                references.add(_key(_identity({'type':kind,'id':identifier,'version':version})))
            except ValueError:
                continue
    chosen['known_references'] = sorted(references-{key})
    chosen['known_writers'] = _known_writers(c,chosen['author_request'])
    approvals=[]
    visible_approvals=[]
    if tasks._exists(c,'work_decisions'):
        for row in c.execute("SELECT * FROM work_decisions WHERE status='active' AND subject IN (?,?)",(SUBMISSION,APPROVE)):
            grant=decisions.decode(row)
            selected=grant['scope']
            operator=grant['details'].get('operator')
            if (selected.get('task')!=task['id'] or selected.get('object')!=key or selected.get('account')!=account
                    or not operator or not native_driver.matches(selected)):
                continue
            if (grant['subject']==APPROVE or grant['details'].get('related_policy')==NATIVE_SCOPE) and (grant['details'].get('adoption_id')!=original['id']
                    or (grant['details'].get('request_ref') or {}).get('sha256')!=ref['sha256']):
                continue
            verified=_selected(c,root,selected,grant['subject'])
            if verified and verified['id']==grant['id'] and _valid(root,verified):
                visible_approvals.append({'id':grant['id'],'operator':operator,'action':selected['action'],
                    'project':selected['project'],'source_ref':grant['source_ref'],
                    'related_policy':grant['details'].get('related_policy','exact'),
                    'authorized_related':grant['details']['authorized_related']})
                if tasks.native_key(operator)==tasks.native_key(actor):
                    approvals.append(grant)
    chosen['verified_grants'] = visible_approvals
    if len(approvals)!=1:
        return {**chosen,'state':'authorization_missing' if not approvals else 'authorization_ambiguous',
                'approval_boundary':'workspace_named_submission',
                'can_preview_native':tasks.participates(c,task,actor)},native,target,body,ref,None,None,[]
    grant=approvals[0]
    use=None
    if entry.get('use'):
        author=json.loads(original['actor_json'])
        use=decisions.checked_source(root,{**json.loads(original['evidence_ref_json']),'locator':entry['use'],'decided_by':author['id']})
        use={**use,'adoption_id':original['id'],'actor':author,'object':key}
    consumers=[]
    if target[0] in ('source','flow'):
        consumers=[({**ref,'adoption_id':adoption['id']},content) for adoption in adoptions
                   for ref,content in _candidate_documents(c,root,task,adoption)]
    return chosen,native,target,body,ref,None,(grant,candidate,use),consumers


def _known_writers(c, author_request):
    """Original author attempts for the adopted body."""
    return [{'request_id':author_request,'run_id':run['id'],'outcome':run['outcome']}
            for run in tasks.pending_runs(c,author_request)]


def _overlap_pending(status, connected, native_scope):
    # Either unenumerated operation may touch objects outside the known set.
    return status['state'] in PENDING and (
        native_scope or status.get('related_policy') == NATIVE_SCOPE
        or bool(connected.intersection(status['affected'])))


def _history(c, selection):
    statements = c.execute("""SELECT kind,payload_json FROM work_task_events WHERE kind IN (?,?)
        AND json_extract(payload_json,'$.account')=? AND json_extract(payload_json,'$.project')=? ORDER BY id""",
                           (INTENT,RESULT,selection['account'],selection['project']))
    attempts = {}
    for row in statements:
        value = json.loads(row['payload_json'])
        attempts.setdefault(value['request_id'],{}).update(value)
    return attempts


def _record_result(c,task_id,actor,result):
    current=_history(c,result).get(result['request_id'])
    if current==result or (current and current['state'] in ('accepted','rejected','stopped_before_send')):
        return current
    tasks._event(c,tasks._row(c,'work_tasks',task_id),RESULT,actor,result)
    return result


def latest_results(c,task_id):
    roots={}
    for row in c.execute('SELECT payload_json FROM work_task_events WHERE task_id=? AND kind IN (?,?) ORDER BY id',(task_id,INTENT,RESULT)):
        result=json.loads(row[0])
        roots[(result['account'],result['project'],result['request_id'])]=result
    return list(roots.values())


def submitted_members(c,task_id):
    members={}
    for result in latest_results(c,task_id):
        if result['state']=='accepted' and result.get('body_digest') and result.get('adoption_id'):
            adopted=members.setdefault(result['adoption_id'],set())
            adopted.add((tuple(result['object'].split('/')),result['body_digest']))
            for related in result['native_affected']:
                key=related['object']
                if related['before']==0 and related['after'] in (20,*range(100,200)) and key in result['related_readbacks']:
                    adopted.add((tuple(key.split('/')),result['related_preimages'][key]['body_digest']))
    return members


def _native_binding(response, native):
    context = response.get('context',{})
    return (context.get('environment')==native['expectedEnvironment'] and
            context.get('actor',{}).get('id')==native['expectedActor'])


def _receipt(response, native, target, action):
    data = response.get('data',{})
    target_response = (data.get('request') or {}).get('target')
    identity = evidence._subject(target_response)
    review_id=data.get('reviewId')
    outcomes=('review_submitted','review_resubmitted') if action=='resubmit' else ('review_submitted',)
    if (evidence.complete_envelope(response) and response.get('operation')=='review '+action
            and isinstance(review_id,str) and review_id.strip() and response.get('status') in ('accepted','completed')
            and response.get('outcome') in outcomes
            and response.get('error') is None and identity==target and _native_binding(response,native)
            and response['context'].get('requestId')==native['requestId']):
        return review_id
    return None


def _stopped(c, root, task, selection, related):
    if task['state'] != 'active':
        return True
    for key in (selection['object'],*related):
        result = decisions.resolve(root,{**selection,'object':key},connection=c,subject=decisions.DATASET_WRITE)
        if any(entry['status']!='applicable' or any(rule['statement']=='stop' for rule in entry['decisions'])
               for entry in result['subjects']):
            return True
    return False


class NativeReview:
    """Use the installed controlled CLI without exposing an executable or URL override."""
    # CLI 0.4.5 has no service-side commitment check for the previewed related set.
    binds_related_scope_at_commit = False

    def __init__(self, root, home):
        self.root, self.home = root, home

    def matches(self, selection):
        accounts = json.loads((self.root/'.control/accounts.json').read_bytes())
        return selection['project']==accounts['project_ref'] and selection['account']==accounts['editor']['user_id']

    def pending_dataset_writes(self, objects, native):
        from dataset_checks.native_journal import pending_writes
        return pending_writes({}, *({'object':key,'account':native['expectedActor'],
                                     'environment':native['expectedEnvironment']} for key in objects))

    def frozen_input(self, ref):
        from production_review import _save_fixed
        source = tasks.evidence_ref(self.root,ref)
        return _save_fixed(self.root,self.home/('request-'+ref['sha256']+'.json'),Path(source['path']).read_bytes())['path']

    def recover(self, native, target, action):
        # The native write command can dispatch if its journal is absent.
        directory = self.root/'work/controlled-cli-output/tool-output'
        for record in directory.glob('tiangong-cli-*/record.json'):
            try:
                meta = json.loads(record.read_bytes())
                if meta.get('phase')!='completed' or meta.get('argv',[])[:2]!=['review',action] or '--dry-run' in meta['argv']:
                    continue
                from task_ledger.sessions import reference
                ref = reference(self.root,record.with_name('stdout.bin'))
                result = _load(self.root,ref)
                if _receipt(result,native,target,action):
                    return result,ref
            except (OSError,ValueError,TypeError,KeyError):
                continue
        return None

    def _invoke(self, argv, *, boundary=None, guard=lambda: None, review_submission=False):
        from tiangong_cli_tool import execute
        run = execute(self.root, {'role':'editor','surface':'cli','argv':argv}, guard=guard,
                      cancelled=lambda:False, writable_roots=(self.home,), network_access=True,
                      boundary=boundary,review_submission=review_submission)
        try:
            value = _load(self.root,run['stdout_ref'])
        except (OSError,ValueError):
            value = {}
        return value, run['stdout_ref']

    def preview(self, action, path):
        return self._invoke(['review',action,'--input',path,'--dry-run','--json'])

    def submit(self, action, path, boundary, guard):
        return self._invoke(['review',action,'--input',path,'--json'],boundary=boundary,guard=guard,review_submission=True)

    def get_review(self, review_id):
        return self._invoke(['review','get','--id',review_id,'--json'])

    def get_body(self, target):
        from uuid import uuid4
        from task_ledger.sessions import reference
        path = self.home / ('readback-'+uuid4().hex+'.json')
        reply, ref = self._invoke(['dataset',target[0],'get','--id',target[1],'--version',target[2],
                                   '--output',str(path),'--json'])
        data = reply.get('data', {})
        if data.get('content') is None and path.is_file() and evidence._read_success(reply):
            data['content'] = _load(self.root,reference(self.root,path))
            reply['body_ref'] = reference(self.root,path)
        return reply, ref


def _preview_matches(preview, native, target, action):
    context = preview.get('context',{})
    data = preview.get('data',{})
    completeness = preview.get('completeness',{})
    return (evidence._read_success(preview) and preview.get('outcome')=='preview_completed'
            and preview.get('operation')=='review '+action and completeness.get('dryRun') is True
            and _native_binding(preview,native) and context.get('requestId')==native['requestId']
            and evidence._subject((data.get('request') or {}).get('target'))==target)


def _confirmed_preflight(preview, native, target, action):
    if not _preview_matches(preview,native,target,action):
        return None
    data = preview.get('data',{})
    completeness = preview.get('completeness',{})
    # A dry-run response that explicitly leaves related scope unchecked cannot
    # authorize unknown cascading state changes.
    related=data.get('related')
    if (completeness.get('relatedScopeComplete') is not True or not isinstance(related,list)
            or 'server_related_scope' in completeness.get('notChecked',())):
        return None
    return sorted({_key(_identity(x)) for x in related} - {_key(target)})


def _read_body(response, native, target, body, states, owner=None):
    data = response.get('data',{})
    subject = data.get('subject') or {}
    complete = response.get('completeness',{})
    context = response.get('context',{})
    actor = context.get('actor',{})
    # The controlled dataset reader reports no independently verified actor in
    # 0.4.5. The review preview/receipt supplies the online actor binding.
    actor_matches = (actor.get('id')==native['expectedActor'] or
                     'id' not in actor and actor.get('status')=='not_independently_verified')
    return (evidence._read_success(response) and context.get('environment')==native['expectedEnvironment']
            and context.get('observation',{}).get('source')=='remote' and actor_matches
            and response.get('outcome')=='dataset_read' and evidence._subject(subject)==target
            and subject.get('stateCode') in states and subject.get('owner')==(owner or native['expectedActor'])
            and complete.get('returned')==1 and complete.get('truncated') is False
            and complete.get('hasMore') is False and evidence.same_review_body(body,data.get('content')))


def _preview_ungranted(driver, chosen, native, target, body, request_ref):
    action = 'resubmit' if native.get('reviewId') else 'submit'
    preview, ref = driver.preview(action,request_ref['path'])
    bound = _preview_matches(preview,native,target,action)
    result = {**chosen,'preflight_ref':ref,'native_preview_bound':bound,
              'native_error_code':(preview.get('error') or {}).get('code'),
              'native_not_checked':(preview.get('completeness') or {}).get('notChecked',[])}
    if bound:
        read, read_ref = driver.get_body(target)
        result.update(_root_preimage(read,read_ref,native,target,body))
    return result


def _root_preimage(read, read_ref, native, target, body):
    matched = _read_body(read,native,target,body,(0,))
    remote = (read.get('data') or {}).get('content')
    return {'preimage_ref':read_ref,'preimage_matches_adoption':matched,
            'preimage_changed_fields':evidence.changed_paths(evidence.canonical_body(body),evidence.canonical_body(remote))
            if evidence.body_identity(remote)==target and not matched else []}


def _one(root, task_id, actor, entry, native_driver, execute):
    with closing(tasks._connect(root,True)) as c:
        task = tasks._row(c,'work_tasks',task_id)
        chosen,native,target,body,request_ref,recovery,approved,consumers=_selection(c,root,task,actor,entry,native_driver)
        if recovery is None and approved is None:
            if execute or not chosen['can_preview_native'] or chosen['requested_scope']=='root_only':
                return chosen
            return _preview_ungranted(native_driver,chosen,native,target,body,request_ref)
        request_id=chosen['request_id']
        adopted=chosen['adoption_id']
        path=request_ref['path']
        if recovery is not None:
            scope={**chosen,'task':task_id,'action':recovery['action']}
            details={'authorized_related':[key for key in recovery['affected'] if key!=scope['object']]}
        else:
            approval,candidate,use=approved
            details,scope=approval['details'],approval['scope']
        action=scope['action']
        history = _history(c,scope)
        old = history.get(request_id)
        if old:
            if old['object']!=chosen['object'] or old['request_sha256']!=request_ref['sha256'] or old.get('action',action)!=action:
                raise ValueError('The stable requestId already identifies another operation')
            if not execute or old['state'] not in PENDING:
                return {**chosen, **old, 'replayed':False}
        native_scope = details.get('related_policy') == NATIVE_SCOPE
        connected = {scope['object'],*details['authorized_related'],*chosen.get('known_references',[])}
        if not old and any(_overlap_pending(status,connected,native_scope) for status in history.values()):
            return {**chosen, 'state':'original_effect_unknown'}
        if not old and _stopped(c,root,task,scope,details['authorized_related']):
            return {**chosen, 'state':'stopped'}
    if old:
        if body is None:
            return {**old,'replayed':False,'reason':'Read the original complete adopted body before verifying this operation'}
        recovered = native_driver.recover(native,target,action)
        if recovered is None:
            return {**old,'replayed':False}
        response, ref = recovered
        return _finish(root,task_id,actor,native_driver,native,target,action,body,old,response,ref)
    usage = independent_use(target,consumers,use,completeness='bounded' if all(x[1] for x in consumers) else 'incomplete')
    if usage['state']!='ready':
        return {**chosen, **usage}
    preview, preflight_ref = native_driver.preview(action,path)
    related = _confirmed_preflight(preview,native,target,action)
    if native_scope and native.get('includeRelated') is not False and _preview_matches(preview,native,target,action):
        reported = (preview.get('data') or {}).get('related') or []
        related = sorted(({_key(_identity(member)) for member in reported} | set(chosen['known_references'])) - {scope['object']})
    elif native_scope:
        related = None
    if related is None or (not native_scope and (
            not set(related).issubset(details['authorized_related']) or native.get('includeRelated') is False and related)):
        reason = ('The native preview did not confirm the complete related scope for this request.'
                  if related is None else 'The preview includes objects outside the selected authorization.')
        return {**chosen, 'state':'related_scope_unverified', 'usage':usage, 'preflight_ref':preflight_ref,
                'native_error_code':(preview.get('error') or {}).get('code'),
                'reason':reason, 'next':'Keep this adopted root pending. Recheck the original request and its grant before submitting.'}
    if not native_scope and not native_driver.binds_related_scope_at_commit:
        return {**chosen, 'state':'related_scope_unverified', 'usage':usage, 'preflight_ref':preflight_ref,
                'reason':'The installed review API cannot require the previewed related scope at commit.'}
    with closing(tasks._connect(root,True)) as c:
        if _stopped(c,root,tasks._row(c,'work_tasks',task_id),scope,related):
            return {**chosen,'state':'stopped'}
    if chosen['known_writers']:
        return {**chosen,'state':'writer_active_or_unknown'}
    pending_writes = native_driver.pending_dataset_writes
    review_objects = (scope['object'],*related)
    if pending := pending_writes(review_objects,native):
        return {**chosen,'state':'original_effect_unknown','dataset_attempts':pending}
    snapshots={}
    for key in related:
        identity=tuple(key.split('/'))
        related_read, related_ref=native_driver.get_body(identity)
        data=related_read.get('data',{})
        subject=data.get('subject',{})
        status=subject.get('stateCode')
        owner=subject.get('owner')
        content=data.get('content')
        # The editor can submit owned drafts. Published/other in-review
        # dependencies must retain their observed state and exact body.
        if (not owner or type(status) is not int or status==0 and owner!=native['expectedActor']
                or not _read_body(related_read,native,identity,content,(status,),owner)):
            return {**chosen,'state':'related_preimage_unverified','object':key,'source_ref':related_ref}
        snapshots[key]={'state':status,'owner':owner,'body_digest':evidence.content_digest(evidence.canonical_body(content)),
                        'source_ref':related_ref,'body_ref':related_read.get('body_ref') or related_ref}
    prior, prior_ref = native_driver.get_body(target)
    preimage = _root_preimage(prior,prior_ref,native,target,body)
    if not preimage['preimage_matches_adoption']:
        return {**chosen,'state':'preimage_changed_or_unverified',**preimage}
    if not execute:
        return {**chosen,'state':'ready','usage':usage,'preflight_ref':preflight_ref,'preimage_ref':prior_ref,
                'related_policy':NATIVE_SCOPE if native_scope else 'exact',
                'related_preimages':snapshots}

    path = native_driver.frozen_input(request_ref)
    from production_review import _save_fixed
    body_digest=evidence.content_digest(evidence.canonical_body(body))
    archive=Path(json.loads(task['plan_ref_json'])['path']).parent/'review-batch-receipts'
    frozen_candidate=_save_fixed(root,archive/('candidate-'+body_digest+'.json'),(tasks._json(body)+'\n').encode())
    intent = {**chosen,'state':'unknown','action':action,'request_sha256':request_ref['sha256'],
              'account':scope['account'],'project':scope['project'],
              'native_request_ref':request_ref,'candidate_ref':candidate,'authorization_ref':approval['source_ref'],
              'body_digest':body_digest,'frozen_candidate_ref':frozen_candidate,
              'authorized_operator':actor,'usage':usage,'affected':sorted({scope['object'],*related}),
              'related_policy':NATIVE_SCOPE if native_scope else 'exact',
              'preflight_ref':preflight_ref,'preimage_ref':prior_ref,'related_preimages':snapshots}
    def check(c):
        task=tasks._row(c,'work_tasks',task_id)
        current=_selected(c,root,scope,approval['subject'])
        adoptions={row['id'] for row in tasks._effective_dispositions(c,task_id) if row['disposition']=='adopt'}
        if (not current or current['id']!=approval['id'] or not _valid(root,current) or not native_driver.matches(scope)
                or _stopped(c,root,task,scope,related) or adopted not in adoptions
                or _known_writers(c,chosen['author_request']) or pending_writes(review_objects,native)):
            tasks._fail('review_root_stopped','Original stop, account, adoption or grant changed before native initiation.',request_id)
        tasks.evidence_ref(root,request_ref)
        tasks.evidence_ref(root,candidate)
        if use:
            if not _valid(root,{'source_ref':use}):
                tasks._fail('review_use_changed','The original independent-use evidence changed before native initiation.',request_id)
        elif target[0] in ('source','flow') and not any(hit['consumer_ref']['adoption_id'] in adoptions
                    and _valid(root,{'source_ref':hit['consumer_ref']}) for hit in usage['known_consumers']):
            tasks._fail('review_use_changed','No adopted consumer still supports this exact review root.',request_id)
    with tasks.transaction(root) as c:
        task = tasks._row(c,'work_tasks',task_id)
        check(c)
        observed = _history(c,scope)
        if request_id in observed:
            return observed[request_id]
        if any(_overlap_pending(value,connected,native_scope) for value in observed.values()):
            return {**chosen,'state':'original_effect_unknown'}
        tasks._event(c,task,INTENT,actor,intent)

    @contextmanager
    def start():
        nonlocal started
        with tasks.transaction(root) as c:
            check(c)
            yield
            started = True

    def guard():
        with closing(tasks._connect(root,True)) as c:
            check(c)

    started = False
    try:
        response, reply_ref = native_driver.submit(action,path,start,guard)
    except (OSError,ValueError) as error:
        result = {**intent,'reason':getattr(error,'code',type(error).__name__)}
        if not started and isinstance(error,tasks.WorkConflict):
            result['state']='stopped_before_send'
        with tasks.transaction(root) as c:
            return _record_result(c,task_id,actor,result)
    return _finish(root,task_id,actor,native_driver,native,target,action,body,intent,response,reply_ref)


def _finish(root,task_id,actor,native_driver,native,target,action,body,intent,response,reply_ref):
    review_id = _receipt(response,native,target,action)
    rows = (((response.get('data') or {}).get('receipt') or {}).get('data') or {}).get('affectedDatasets')
    affected=[]
    malformed=False
    for member in rows if isinstance(rows,list) else []:
        if not isinstance(member,dict) or member.get('table') not in TABLES:
            malformed=True
            continue
        try:
            key=_key(_identity({'type':TABLES[member['table']],'id':member.get('id'),'version':member.get('version')}))
        except ValueError:
            malformed=True
            continue
        if type(member.get('previous_state_code')) is not int or type(member.get('state_code')) is not int:
            malformed=True
        affected.append({'object':key,'before':member.get('previous_state_code'),'after':member.get('state_code')})
    result = {**intent,'receipt_ref':reply_ref,'native_affected':affected}
    original_request = (response.get('data') or {}).get('request') or {}
    if (response.get('outcome')=='service_rejected' and _native_binding(response,native)
            and response.get('context',{}).get('requestId')==native['requestId']
            and evidence._subject(original_request.get('target'))==target
            and ((response.get('data') or {}).get('receipt') or {}).get('ok') is False):
        result['state']='rejected'
    if review_id:
        result.update(state='accepted_readback_pending',review_id=review_id)
        root_key=_key(target)
        root_change=next((member for member in affected if member['object']==root_key),{})
        changed = {member['object'] for member in affected if member['before']!=member['after']}
        expected={root_key,*intent.get('related_preimages',{})}
        unexpected=changed-expected
        if (malformed or not isinstance(rows,list) or len({member['object'] for member in affected})!=len(affected)
                or root_change.get('before')!=0 or root_change.get('after') not in (20,*range(100,200)) or unexpected):
            result.update(state='accepted_effect_unverified',unexpected_related=sorted(unexpected))
            result['affected'] = sorted(set(intent['affected']) | unexpected)
    with tasks.transaction(root) as c:
        result=_record_result(c,task_id,actor,result)
    if result['state']=='accepted_effect_unverified' and review_id:
        review, review_ref = native_driver.get_review(review_id)
        readbacks = {}
        for key in sorted({member['object'] for member in affected} | {_key(target)}):
            observed, ref = native_driver.get_body(tuple(key.split('/')))
            subject = (observed.get('data') or {}).get('subject') or {}
            readbacks[key] = {'source_ref':ref,'state':subject.get('stateCode'),'owner':subject.get('owner')}
        observed_review = (review.get('data') or {}).get('review') or {}
        result = {**result,'effect_readbacks':readbacks,'review_readback_ref':review_ref,
                  'review_readback_verified':evidence._read_success(review) and _native_binding(review,native)
                  and observed_review.get('id')==review_id
                  and (observed_review.get('data_id'),observed_review.get('data_version'))==target[1:]}
        with tasks.transaction(root) as c:
            return _record_result(c,task_id,actor,result)
    if result['state']!='accepted_readback_pending':
        return result
    review, review_ref = native_driver.get_review(review_id)
    read, body_ref = native_driver.get_body(target)
    review_row = (review.get('data') or {}).get('review') or {}
    related_readbacks={}
    transitions={member['object']:member for member in affected}
    for key, snapshot in intent.get('related_preimages',{}).items():
        related = tuple(key.split('/'))
        transition=transitions.get(key)
        expected=transition.get('after') if transition else snapshot['state']
        if ((transition and transition['before']!=snapshot['state'])
                or expected!=snapshot['state'] and snapshot['state']!=0):
            return result
        observed, ref=native_driver.get_body(related)
        content=observed.get('data',{}).get('content')
        if (not _read_body(observed,native,related,content,(expected,),snapshot['owner'])
                or evidence.content_digest(evidence.canonical_body(content))!=snapshot['body_digest']):
            return result
        related_readbacks[key]={'source_ref':ref,'body_ref':observed.get('body_ref') or ref,'state':expected}
    if (evidence._read_success(review) and _native_binding(review,native) and review_row.get('id')==review_id
            and (review_row.get('data_id'),review_row.get('data_version'))==target[1:]
            and _read_body(read,native,target,body,(20,*range(100,200)))):
        result.update(state='accepted',review_readback_ref=review_ref,readback_envelope_ref=body_ref,
                      body_readback_ref=read.get('body_ref') or body_ref,related_readbacks=related_readbacks)
        with tasks.transaction(root) as c:
            adoption=tasks._row(c,'work_dispositions',intent['adoption_id'])
            returned=tasks._row(c,'work_returns',adoption['return_id'])
            result['handoff']={'adoption_id':adoption['id'],'author_request':returned['request_id'],
                'review_id':review_id,'read_command':'review get --id '+review_id,
                'recipient_role':'platform_feedback','source_ref':review_ref}
            return _record_result(c,task_id,actor,result)
    return result


def submit(root, task_id, path=None, *, adoption=None, request=None, use=None, execute=False, driver=None):
    """Independent roots continue after a blocked or unresolved one."""
    from production_review import scientific_path
    actor = tasks.verified_actor(root)
    if path:
        if adoption or request or use:
            raise ValueError('Select one native root or one batch input')
        document=json.loads(scientific_path(root,path).read_bytes())
        roots=document.get('roots') if isinstance(document,dict) else None
    else:
        roots=[{'adoption':adoption,'request':request,**({'use':use} if use else {})}]
    if not isinstance(roots,list) or not roots:
        raise ValueError('Select adopted roots using roots, or one --adoption with --request')
    task = tasks._read_one(root,'work_tasks',task_id)
    home = scientific_path(root,Path(task['plan_ref']['path']).parent/'review-batch-receipts',must_exist=False)
    if driver is None:
        home.mkdir(exist_ok=True)
        driver = NativeReview(root,home)
    results = []
    for entry in roots:
        try:
            results.append(_one(root,task_id,actor,entry,driver,execute))
        except (OSError,ValueError,KeyError,TypeError) as error:
            results.append({'selection':entry,'state':'unavailable','reason':str(error)})
    return {'task_id':task_id,'mode':'execute' if execute else 'preview','roots':results}
