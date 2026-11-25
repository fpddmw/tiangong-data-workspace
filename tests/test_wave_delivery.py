"""Isolated delivery, scientific round and batch execution checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import delivery_evidence as evidence
import harness
from task_ledger import decisions
from platform_review import submission as batch
import harness_scope as scope
import harness_tasks as tasks
import harness_wave as wave
import review_fixtures
from scientific_review import rounds
import run_worker
import test_wave_coordination as fixture


class DeliveryTests(unittest.TestCase):
    setUp = fixture.WaveTests.setUp
    tearDown = fixture.WaveTests.tearDown
    actor = fixture.WaveTests.actor
    save = fixture.WaveTests.save
    call = fixture.WaveTests.call
    request = fixture.WaveTests.request
    cli = fixture.WaveTests.cli
    decision = fixture.WaveTests.decision
    current = fixture.WaveTests.current

    def reviewer_return(self, review, label, text):
        source = self.root / (label + '.md')
        source.write_text(text, encoding='utf-8')
        result = self.cli('work-return', '--request', review['id'], '--result', str(source), actor=self.reviewer)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def register(self, number, previous=None, *, second_object=False):
        parent=tasks.read_request(self.root,'request')
        candidate=self.save('candidate-'+str(number),review_fixtures.process(amount=str(number)))
        artifacts=[candidate]
        if second_object:
            artifacts.append(self.save('flow-'+str(number),review_fixtures.flow(label='Input '+str(number))))
        packet = {'authors':[self.worker], 'review_scope':('按原含水率换算，湿料 0.8 t 对应 0.6 t 干基，请复核。'
                                                           if number==2 else '核实这一版每吨干产品的上游负担。'),
                  'artifact_refs':artifacts, 'evidence_refs':[candidate]}
        if previous:
            opinion = tasks.read_return(self.root, run_worker._direct_return_id(self.root, previous['id']))
            packet['prior_review']={'review_id':previous['id'],'submission_ref':previous['input_ref'],
                                    'return_id':opinion['id'],'opinion_ref':opinion['result_ref']}
        with patch.dict('os.environ',CODEX_THREAD_ID='lead'):
            return run_worker._program_review_request(self.root,SimpleNamespace(actor=self.lead),parent,packet)

    def test_real_recurrence_and_one_explicit_extra_round(self):
        self.request()
        defect = '投入按吨湿料、产品按干基，这会改变消费者的单位负担，请按原含水率换算并复算。\n'
        first=self.register(1)
        self.reviewer_return(first,'first',defect)
        second=self.register(2,first)
        corrected=self.reviewer_return(second,'second','我用原含水率复算了第二版，产品和投入已统一到同一干基。')
        self.assertFalse(any(item['action']=='review_correction' for item in self.current()['actionable']))
        third=self.register(3,second)
        returned=self.reviewer_return(third,'third',defect+'第二版已按原含水率修复，第三版重新用了吨湿料。')
        self.assertFalse(any(item['action']=='review_correction' for item in self.current()['actionable']))
        result=self.cli('work-review-record','--return-id',returned['id'],'--conclusion','material_issue',
                        '--basis',returned['result_ref']['path'],'--recurs-after',corrected['id'],actor=self.reviewer)
        self.assertEqual(result.returncode,0,result.stderr)
        pending=self.current()['actionable']
        raised, = [item for item in pending if item['action']=='review_correction']
        self.assertEqual(raised['return_id'],returned['id'])
        self.assertEqual(raised['recurs_after'],corrected['id'])
        with closing(tasks._connect(self.root,True)) as c:
            self.assertEqual([state for _,state in rounds.unit_state(c,'task','request')],['used']*3)
            readback=rounds.opinion_view(c,self.root,second)['opinion']
        self.assertIn('已统一',readback['text'])
        self.assertEqual(readback['tracking']['prior_review']['review_id'],first['id'])
        self.assertEqual(readback['author_handling'][0]['pointer'],'/review_scope')
        self.assertEqual(readback['tracking']['current']['conclusions'],[])
        self.assertEqual(self.register(3,second)['id'],third['id'])
        with self.assertRaises(tasks.WorkConflict) as blocked:
            self.register(4,third)
        self.assertEqual(blocked.exception.code,'review_round_budget')
        unit=blocked.exception.record_id
        self.assertIn(unit,{item['object'] for item in self.current()['actionable']})
        args=('work-decision-record','--review',blocked.exception.review_request,'--add','1','--reason','复核原干基分母')
        self.assertNotEqual(self.cli(*args,actor=self.worker).returncode,0)
        grant=self.cli(*args)
        self.assertEqual(grant.returncode,0,grant.stderr)
        self.assertEqual(json.loads(self.cli(*args).stdout),json.loads(grant.stdout))
        four=self.register(4,third)
        self.reviewer_return(four,'four',defect)
        with self.assertRaises(tasks.WorkConflict):
            self.register(5,four)
        handoff=self.save('same-unit-handoff',{'purpose':'The same use and original remaining revision.'})
        assigned=self.cli('work-request','--task','task','--brief',handoff['path'],'--continues','request',
                          '--scope','a','--scope','b')
        self.assertEqual(assigned.returncode,0,assigned.stderr)
        successor=json.loads(assigned.stdout)['id']
        another=self.save('same-unit-review',{'purpose':'Review the same open scientific issue.'})
        blocked=self.cli('work-request','--task','task','--review-of',successor,'--brief',another['path'])
        self.assertEqual(json.loads(blocked.stderr)['code'],'review_round_budget')

    def test_public_review_shares_atomic_budget_and_incomplete_original_frees_round(self):
        self.request()
        early=self.register(1)
        stopped=self.reviewer_return(early,'summary','The tool stopped before assessing the evidence.')
        marked=self.cli('work-review-record','--return-id',stopped['id'],'--conclusion','not_assessed',
                        '--basis',stopped['result_ref']['path'],actor=self.reviewer)
        self.assertEqual(marked.returncode,0,marked.stderr)
        self.register(2)
        self.register(3)
        a=self.save('public-a',{'goal':'Review original public A'})
        b=self.save('public-b',{'goal':'Review original public B'})
        def submit(ref):
            return self.cli('work-request','--task','task','--review-of','request','--brief',ref['path'])
        with ThreadPoolExecutor(2) as pool:
            one,two=pool.map(submit,(a,b))
        self.assertEqual(sorted((one.returncode,two.returncode)),[0,1])
        rejected=one if one.returncode else two
        self.assertEqual(json.loads(rejected.stderr)['code'],'review_round_budget')

    def test_three_plain_opinions_block_a_fourth_without_an_index(self):
        self.request()
        text = '投入按吨湿料、产品按干基，这会改变消费者的单位负担，请按原含水率换算并复算。'
        originals = [self.reviewer_return(self.register(number), str(number), text) for number in range(1,4)]
        with closing(tasks._connect(self.root, True)) as c:
            self.assertFalse(rounds.recorded_assessments(c, 'task'))
            self.assertEqual([state for _, state in rounds.unit_state(c,'task','request')], ['used']*3)
        with patch.dict('os.environ', CODEX_THREAD_ID='worker'):
            readback = run_worker.programmatic(self.root, originals[-1]['request_id'], read_result=True)
        self.assertEqual(readback['original_opinion']['opinion']['text'], text)
        with self.assertRaises(tasks.WorkConflict) as blocked:
            self.register(4)
        self.assertEqual(blocked.exception.code, 'review_round_budget')

    def test_held_review_selects_exact_object_and_extra_round_is_atomic(self):
        self.request()
        for number in range(1,4):
            original=self.register(number,second_object=True)
            self.reviewer_return(original,'original-'+str(number),'The dry-mass denominator needs the original moisture correction.')
        with self.assertRaises(tasks.WorkConflict) as denied:
            self.register(4,second_object=True)
        args=('work-decision-record','--review',denied.exception.review_request,'--add','1','--reason','Assess the amended denominator.')
        choices=json.loads(self.cli(*args).stdout)
        self.assertEqual(choices['state'],'select_object')
        self.assertEqual(set(choices['objects']),{'process/'+review_fixtures.PROCESS_ID,'flow/'+review_fixtures.FLOW_ID})
        for object_id in choices['objects']:
            granted=self.cli(*args,'--object',object_id)
            self.assertEqual(granted.returncode,0,granted.stderr)
        def compete(number):
            try:
                return self.register(number,second_object=True)['id']
            except tasks.WorkConflict as error:
                return error.code
        with patch.dict('os.environ',CODEX_THREAD_ID='lead'), ThreadPoolExecutor(2) as pool:
            result=list(pool.map(compete,(4,5)))
        self.assertEqual(result.count('review_round_budget'),1,result)

    def test_reserve_requires_core_endpoint_or_named_external_blocker(self):
        setup={'items':[{'scope_id':sid,'kind':'deliverable','label':sid,'source_refs':[self.brief],
                         'target_endpoint':'evidence'} for sid in ('a','b')],
                 'verified_scope_ids':['a','b'],'source_ref':self.evidence}
        with patch.dict('os.environ',CODEX_THREAD_ID='lead'):
            scope.apply_scope(self.root,'task',setup,self.lead)
            with tasks.transaction(self.root) as c:
                scope.set_contract(c,self.root,'task','task',{'purpose':'Deliver both declared source uses.','endpoint':'evidence',
                    'reserve':{'scope_ids':['b'],'core_scope_ids':['a'],'max_requests':1}},self.evidence,self.lead)
        self.request('core',scope=['a'])
        req=self.request(scope=['b'])
        def gate():
            with patch.dict('os.environ',CODEX_THREAD_ID='lead'):
                return tasks.admission(self.root,'task',self.lead,request_id=req['id'],side_effect=True)
        with self.assertRaisesRegex(tasks.WorkConflict,'Core commitments'):
            gate()
        from task_ledger.sessions import reference
        missing=self.root/'external-measurement.md'
        report=self.root/'core.md';report.write_text('The external measurement is still outstanding.')
        self.call(self.worker,'return',{'id':'core-report','request_id':'core','result_ref':reference(self.root,report),
             'summary':'Original external dependency','artifact_refs':[],'coverage':[]})
        item=next(i for i in self.current()['actionable'] if i['return_id']=='core-report')
        deferred=self.cli('work-wait','--task','task','--item',item['item_id'],'--for-file',str(missing),'--external')
        self.assertEqual(deferred.returncode,0,deferred.stderr)
        self.assertEqual(gate()['state'],'active')
        missing.write_text('Original data are now available.')
        with self.assertRaisesRegex(tasks.WorkConflict,'Core commitments'):
            gate()

    def approved(self, name, body, *, use=False, related=(), authorize=True, action='submit',
                 prior=None, select=False, native_scope=False, include_related=True):
        request=self.request(ident='author-'+name,scope=['a'])
        original=self.save('candidate-for-'+name,body)
        target=evidence.body_identity(body)
        refs = [{'id':name,'version':target[2],**original}]
        if prior is not None:
            refs.append({'id':name+'-prior','version':target[2],**self.save(name+'-prior', prior)})
        if select:
            refs.extend(evidence.selected_files(self.root, [original['path']], home=self.root))
        returned=self.call(self.worker,'return',{'id':'return-'+name,'request_id':request['id'],
             'result_ref':self.evidence,'artifact_refs':refs,'summary':'Exact original candidate'})
        snap=tasks.read_task(self.root,'task')['task']
        adoption_note=self.root/f'adopt-{name}.md'
        adoption_note.write_text('Adopt the exact candidate as an independent reference for comparing dry inputs.\n')
        from task_ledger.sessions import reference
        adopted=self.call(self.lead,'dispose',{'id':'adoption-'+name,'return_id':returned['id'],'disposition':'adopt',
            'expected_task_revision':snap['revision'],'plan_revision':snap['plan_revision'],'scope':['a'],
            'evidence_ref':reference(self.root,adoption_note),'summary':'Adopt accuracy and use of the whole candidate'})
        native = self.save('native-request-'+name,{'requestId':name,'expectedActor':'account','expectedEnvironment':'https://isolated.invalid',
                          'targets':[dict(zip(('type','id','version'),target))],'includeRelated':include_related})
        selected={'task':'task','object':'/'.join(target),'action':action,'account':'account','project':'project'}
        body={'id':'approve-'+name,'scope':selected,'subject':batch.SUBMISSION,'statement':'allow',
              'source_ref':{**self.evidence,'locator':'file','decided_by':'lead'},'details':{
                   'operator':self.lead,'authorized_related':list(related),
                   **({'related_policy':batch.NATIVE_SCOPE,'adoption_id':adopted['id'],'request_ref':native}
                      if native_scope else {})}}
        if authorize:
            with patch.dict('os.environ',CODEX_THREAD_ID='lead'):
                decisions.add(self.root,body)
        return {'adoption':adopted['id'],'request':native['path'],**({'use':'L1'} if use else {})}

    def batch_call(self, driver, ids, *, execute=False):
        path=self.save('batch',{'roots':ids})['path']
        argv=['--root',str(self.root),'work-submit-batch','--task','task','--input',path]
        if execute:
            argv.append('--execute')
        with patch.dict('os.environ',CODEX_THREAD_ID='lead'),patch.object(batch,'NativeReview',return_value=driver),patch('builtins.print') as output:
            harness.main(argv)
        return json.loads(output.call_args.args[0])['roots']

    def test_batch_preserves_partial_success_and_recovers_one_lost_reply(self):
        bodies = [review_fixtures.process(identifier=ident) for ident in
                  ('f78c724a-cb91-47a2-9d70-bd673a3edf81','f78c724a-cb91-47a2-9d70-bd673a3edf82')]
        flow = review_fixtures.flow('00.00.009')
        approvals=[self.approved('lost',bodies[0]),self.approved('unused',flow),self.approved('good',bodies[1])]
        driver=LocalReviewReceiver(self,[*bodies,flow],missing='lost')
        preview=self.batch_call(driver,approvals)
        self.assertEqual([r['state'] for r in preview],['ready','usage_missing','ready'])
        self.assertEqual(driver.sent,[])
        results=self.batch_call(driver,approvals,execute=True)
        self.assertEqual([r['state'] for r in results],['unknown','usage_missing','accepted'])
        self.assertEqual(driver.sent,['lost','good'])
        results=self.batch_call(driver,approvals,execute=True)
        self.assertEqual([r['state'] for r in results],['accepted','usage_missing','accepted'])
        self.assertEqual(driver.sent,['lost','good'])

    def test_single_and_batch_use_exact_adoption_and_known_consumers(self):
        flow=review_fixtures.flow()
        process=review_fixtures.process()
        standalone=review_fixtures.flow('00.00.009')
        root=self.approved('flow-linked',flow)
        self.approved('process-consumer',process)
        independent=self.approved('flow-own-use',standalone,use=True)
        driver=LocalReviewReceiver(self,[flow,process,standalone])
        direct=['--root',str(self.root),'work-submit-batch','--task','task',
                '--adoption',root['adoption'],'--request',root['request']]
        with patch.dict('os.environ',CODEX_THREAD_ID='lead'),patch.object(batch,'NativeReview',return_value=driver),patch('builtins.print') as output:
            harness.main(direct)
        result=json.loads(output.call_args.args[0])['roots'][0]
        self.assertEqual(result,self.batch_call(driver,[root])[0])
        self.assertEqual(result['state'],'ready')
        self.assertEqual({tuple(hit['consumer']) for hit in result['usage']['known_consumers']},{evidence.body_identity(process)})
        own=self.batch_call(driver,[independent])[0]
        self.assertEqual(own['state'],'ready')
        self.assertEqual(own['usage']['independent_use']['adoption_id'],independent['adoption'])
        driver.related_unknown=True
        held=self.batch_call(driver,[root],execute=True)[0]
        self.assertEqual(held['state'],'related_scope_unverified')
        self.assertIn('complete related scope',held['reason'])
        self.assertEqual(driver.sent,[])

    def test_stage_preview_uses_chosen_revision_among_same_version_bodies(self):
        current = review_fixtures.process(amount='2')
        prior = review_fixtures.process(amount='1')
        driver = LocalReviewReceiver(self, [current])
        selected = self.approved('selected', current, prior=prior, select=True, authorize=False)
        ambiguous = self.approved('ambiguous', current, prior=prior, authorize=False)
        self.assertEqual(self.batch_call(driver, [selected])[0]['state'], 'authorization_missing')
        with closing(tasks._connect(self.root, True)) as c:
            task = tasks._row(c, 'work_tasks', 'task')
            with self.assertRaisesRegex(ValueError, 'unambiguous'):
                batch._selection(c, self.root, task, self.lead, ambiguous, driver)
        self.assertEqual(driver.sent, [])

    def test_scientific_adoption_does_not_authorize_native_submission(self):
        body=review_fixtures.process()
        root=self.approved('no-grant',body,authorize=False)
        driver=LocalReviewReceiver(self,[body])
        result=self.batch_call(driver,[root],execute=True)[0]
        self.assertEqual(result['state'],'authorization_missing')
        self.assertEqual(driver.sent,[])

    def test_ungranted_preview_reports_exact_remote_body_drift(self):
        body = review_fixtures.process()
        adopted = self.approved('ungranted-preview',body,authorize=False)
        driver = LocalReviewReceiver(self,[review_fixtures.process(amount='0.999')])
        result = self.batch_call(driver,[adopted])[0]
        self.assertEqual(result['state'],'authorization_missing')
        self.assertTrue(result['native_preview_bound'])
        self.assertFalse(result['preimage_matches_adoption'])
        self.assertTrue(any(path.endswith('/meanAmount') for path in result['preimage_changed_fields']))
        self.assertEqual(driver.sent,[])

    def test_resubmit_uses_its_exact_grant_and_the_cli_submitted_outcome(self):
        body=review_fixtures.process()
        selected=self.approved('retry-review',body,action='resubmit')
        driver=LocalReviewReceiver(self,[body])
        result=self.batch_call(driver,[selected],execute=True)[0]
        self.assertEqual(result['state'],'accepted')
        self.assertEqual(result['action'],'resubmit')
        self.assertEqual(driver.sent,['retry-review'])

    def test_unexpected_related_effect_stays_unverified_on_repeated_recovery(self):
        process=review_fixtures.process()
        flow=review_fixtures.flow()
        related=evidence.body_identity(flow)
        selected=self.approved('cascade',process,related=['/'.join(related)])
        driver=LocalReviewReceiver(self,[process,flow])
        driver.related=[related]
        selected_without_grant=self.approved('no-cascade',review_fixtures.process(identifier='f78c724a-cb91-47a2-9d70-bd673a3edf82'))
        held=self.batch_call(driver,[selected_without_grant],execute=True)[0]
        self.assertEqual(held['state'],'related_scope_unverified')
        self.assertIn('outside',held['reason'])
        self.assertEqual(driver.sent,[])
        driver.related=[]  # A stale preview failed to report the change applied by the receiver.
        driver.unexpected=[related]
        for _ in range(2):
            result=self.batch_call(driver,[selected],execute=True)[0]
            self.assertEqual(result['state'],'accepted_effect_unverified')
            self.assertEqual(result['unexpected_related'],['/'.join(related)])
        self.assertEqual(driver.sent,['cascade'])

    def test_atomic_scope_change_refuses_zero_effects_and_missing_binding_stays_local(self):
        process,flow=review_fixtures.process(),review_fixtures.flow()
        related=evidence.body_identity(flow)
        selected=self.approved('atomic-cascade',process,related=['/'.join(related)])
        driver=LocalReviewReceiver(self,[process,flow])
        driver.binds_related_scope_at_commit=False
        held=self.batch_call(driver,[selected],execute=True)[0]
        self.assertEqual(held['state'],'related_scope_unverified')
        self.assertIn('at commit',held['reason'])
        self.assertEqual(driver.sent,[])
        driver.binds_related_scope_at_commit=True
        driver.enforce_preview=True
        driver.before_start=lambda: driver.related.append(related)
        rejected=self.batch_call(driver,[selected],execute=True)[0]
        self.assertEqual(rejected['state'],'rejected')
        self.assertEqual(driver.reviewed,{})
        self.assertEqual(driver.sent,['atomic-cascade'])
        repeated=self.batch_call(driver,[selected],execute=True)[0]
        self.assertEqual(repeated['state'],'rejected')
        self.assertEqual(driver.sent,['atomic-cascade'])

    def test_explicit_native_scope_uses_bound_preview_and_preserves_root_only(self):
        first = review_fixtures.process()
        second = review_fixtures.process(identifier='f78c724a-cb91-47a2-9d70-bd673a3edf82')
        native = self.approved('native',first,native_scope=True)
        exact = self.approved('root-only',second,include_related=False)
        driver = LocalReviewReceiver(self,[first,second,review_fixtures.flow()])
        driver.binds_related_scope_at_commit = False
        driver.related_unknown = True
        preview = self.batch_call(driver,[native,exact])
        self.assertEqual([root['state'] for root in preview],['ready','related_scope_unverified'])
        self.assertEqual(driver.sent,[])
        self.assertEqual(self.batch_call(driver,[native,exact],execute=True)[0]['state'],'accepted')
        self.assertEqual(driver.sent,['native'])

    def test_native_surprise_records_actual_effect_and_holds_later_roots(self):
        first = review_fixtures.process()
        second = review_fixtures.process(identifier='f78c724a-cb91-47a2-9d70-bd673a3edf82')
        flow = review_fixtures.flow('00.00.002')
        native = self.approved('native-surprise',first,native_scope=True)
        later = self.approved('later',second,native_scope=True)
        driver = LocalReviewReceiver(self,[first,second,flow,review_fixtures.flow()])
        driver.binds_related_scope_at_commit = False
        driver.related_unknown = True
        driver.unexpected = [evidence.body_identity(flow)]
        surprise, held = self.batch_call(driver,[native,later],execute=True)
        key = '/'.join(evidence.body_identity(flow))
        self.assertEqual(surprise['state'],'accepted_effect_unverified')
        self.assertEqual(surprise['effect_readbacks'][key]['state'],20)
        self.assertTrue(surprise['review_readback_verified'])
        self.assertEqual(held['state'],'original_effect_unknown')
        self.assertEqual(driver.sent,['native-surprise'])

    def test_batch_stops_immediately_before_native_initiation(self):
        body=review_fixtures.process()
        ident=self.approved('stop',body)
        driver=LocalReviewReceiver(self,[body])
        revision=tasks.read_task(self.root,'task')['task']['revision']
        driver.before_start=lambda: self.call(self.main,'pause',{'id':'pause','task_id':'task','expected_task_revision':revision,
            'evidence_ref':self.evidence,'summary':'Original stop before the root sends.'})
        outputs=self.batch_call(driver,[ident],execute=True)
        self.assertEqual(outputs[0]['state'],'stopped_before_send')
        self.assertEqual(driver.sent,[])


class LocalReviewReceiver:
    binds_related_scope_at_commit = True

    def __init__(self,test,bodies,*,missing=None):
        self.bodies={evidence.body_identity(b):b for b in bodies}
        self.sent=[]
        self.responses={}
        self.reviewed={}
        self.test=test
        self.missing=missing
        self.related_unknown=False
        self.related=[]
        self.unexpected=[]
        self.enforce_preview=False
        self.previews={}
        self.before_start=lambda:None

    def matches(self, selection):
        return (selection['account'],selection['project'])==('account','project')

    def pending_dataset_writes(self, objects, native):
        return []

    def frozen_input(self, ref):
        return ref['path']

    def env(self, **kwargs):
        return {'schemaVersion':1,'context':{'environment':'https://isolated.invalid','actor':{'id':'account'},
                'observation':{'source':'remote','time':'2026-11-22T20:00:00Z'}},**kwargs}

    def preview(self, action, path):
        value=json.loads(Path(path).read_bytes())
        target,=value['targets']
        self.previews[value['requestId']]=set(self.related)
        output=self.env(status='completed',outcome='preview_completed',operation='review '+action,
                        data={'request':{'target':target},'related':[dict(zip(('type','id','version'),key)) for key in self.related]},
                        completeness={'dryRun':True,'relatedScopeComplete':not self.related_unknown,
                                      'notChecked':['server_related_scope'] if self.related_unknown else []})
        output['context']['requestId']=value['requestId']
        return output,self.test.save('preview-'+value['requestId']+str(self.related_unknown),output)

    def get_body(self, target):
        status=20 if target in self.reviewed else 0
        row=dict(zip(('type','id','version'),target)) | {'stateCode':status,'owner':'account','modifiedAt':'2026-11-22T20:00:00Z'}
        doc=self.env(status='completed',outcome='dataset_read',operation='dataset '+target[0]+' get',
                     data={'content':self.bodies[target],'subject':row},completeness={'returned':1,'truncated':False,'hasMore':False})
        doc['context']['actor'] = {'status':'not_independently_verified'}
        return doc,self.test.save('read-'+target[1]+'-'+target[2]+'-'+str(status),doc)

    def submit(self, action, path, boundary, guard):
        value=json.loads(Path(path).read_bytes())
        name=value['requestId']
        raw,=value['targets']
        target=evidence._subject(raw)
        self.before_start()
        with boundary():
            self.sent.append(name)
            if self.enforce_preview and set(self.related)!=self.previews[name]:
                result=self.env(status='failed',outcome='service_rejected',operation='review '+action,
                                data={'request':{'target':raw},'receipt':{'ok':False,'code':'REVIEW_STATE_CONFLICT'}})
                result['context']['requestId']=name
                self.responses[name]=result
                return result,self.test.save('review-rejection-'+name,result)
            review_id='review-'+name
            affected=[]
            for member in (target,*self.related,*self.unexpected):
                self.reviewed[member]=review_id
                table=next(key for key,kind in batch.TABLES.items() if kind==member[0])
                affected.append({'id':member[1],'version':member[2],'table':table,'previous_state_code':0,'state_code':20})
            result=self.env(status='accepted',outcome='review_submitted',operation='review '+action,
                            data={'reviewId':review_id,'request':{'target':raw},'receipt':{'data':{
                                'affectedDatasets':affected}}})
            result['context']['requestId']=name
            self.responses[name]=result
            ref=self.test.save('review-native-'+name,result)
        if name==self.missing:
            raise OSError('The isolated first reply was dropped after acceptance.')
        return result,ref

    def recover(self,native,target,action):
        value=self.responses.get(native['requestId'])
        return (value,self.test.save('review-native-'+native['requestId'],value)) if value else None

    def get_review(self,review_id):
        target=next(key for key,value in self.reviewed.items() if value==review_id)
        doc=self.env(status='completed',outcome='review_records_read',operation='review get',
                     data={'review':{'id':review_id,'data_id':target[1],'data_version':target[2]}})
        return doc,self.test.save('read-'+review_id,doc)
