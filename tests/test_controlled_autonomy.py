"""Public-boundary fixture proofs, including a local original-candidate copy.

Native and scientific receipts are synthetic; no remote operation or real
scientific acceptance is performed by these tests.
"""
import contextlib
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import harness_scope as s
import harness_tasks as t
import test_harness_tasks as fixtures

BIN=Path(__file__).resolve().parents[1]/'bin'

class AcceptanceTests(unittest.TestCase):
    setUp=fixtures.TaskCoreTests.setUp
    tearDown=fixtures.TaskCoreTests.tearDown
    actor=fixtures.TaskCoreTests.actor
    save=fixtures.TaskCoreTests.save
    call=fixtures.TaskCoreTests.call
    request=fixtures.TaskCoreTests.request
    returned=fixtures.TaskCoreTests.returned
    dispose=fixtures.TaskCoreTests.dispose
    def cli(self,*args,actor='lead',check=True):
        r=subprocess.run([sys.executable,str(BIN/'harness.py'),'--root',str(self.root),*args],
            env={**os.environ,'CODEX_THREAD_ID':actor},text=True,capture_output=True)
        if check:self.assertEqual(r.returncode,0,r.stderr)
        return json.loads(r.stdout) if r.returncode==0 else json.loads(r.stderr)
    def agreement(self,endpoint='evidence',refs=()):
        return {'purpose':'A bounded use of the original evidence and exact candidate','endpoint':endpoint,
            'scope_boundary':{'in_scope':'Original finite process and its declared interfaces',
                              'out_of_scope':'No unrelated supply chain, publication or remote calculation'},
            'acceptance':{'text':'Complete evidence, scientific limits and exact version for this use','refs':list(refs)},
            'authority_refs':[self.evidence] if endpoint in ('saved_draft','submitted','published') else [],
            'interfaces':{'text':'Original input and output interfaces only','refs':[]}}
    def configure(self,count=9,endpoint='evidence'):
        ids=['P'+str(i) for i in range(count)]
        value={'items':[{'scope_id':sid,'kind':'deliverable','label':'Responsibility '+sid,'source_refs':[self.brief]} for sid in ids],
            'verified_scope_ids':ids,'source_ref':self.brief}
        path=self.save('scope-input',value)['path']
        self.cli('work-scope','--task','task','--apply',path)
        self.request(scope=ids)
        con=self.agreement(endpoint,[self.brief])
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'task','task',con,self.brief,self.lead)
            s.set_contract(c,self.root,'request','request',con,self.brief,self.lead)
        return ids
    def adopt(self,ident='return',ids=None,acceptance='final',checks=None):
        note=self.save('adoption-'+ident,{'use':'Supported only within original stated limits','scope':ids})['path']
        args=['work-dispose','--return-id',ident,'--use','adopt','--decision',note,'--acceptance',acceptance]
        for sid in ids or []:args+=['--scope',sid]
        if checks:args+=['--checks',str(checks)]
        return self.cli(*args)
    def completion(self,note):return self.cli('work-complete','--task','task','--note',note)

    def test_prepared_adoption_compares_only_relevant_decisions_and_file_versions(self):
        self.configure(2)
        returned=self.returned(coverage=['P0'])
        self.request('independent-package',scope=['P1'])
        prepare=s.prepare_decision
        def unrelated_progress(*args):
            prepared=prepare(*args)
            self.returned('parallel-result','independent-package',coverage=['P1'])
            return prepared
        with patch.object(s,'prepare_decision',side_effect=unrelated_progress):
            accepted=self.dispose(scope=['P0'],acceptance='final')
        self.assertEqual(accepted['scope'],['P0'])
        self.assertEqual(self.dispose(scope=['P0'],acceptance='final')['id'],accepted['id'])

        def change_source(*args):
            prepared=prepare(*args)
            Path(returned['result_ref']['path']).write_text('Changed original after the read snapshot')
            return prepared
        with patch.object(s,'prepare_decision',side_effect=change_source):
            with self.assertRaisesRegex(t.WorkConflict,'Relevant original evidence'):
                self.dispose('second',scope=['P0'],acceptance='final')
        self.assertEqual(len(t.read_task(self.root,'task')['dispositions']),1)

    def test_prepared_adoption_rechecks_parallel_competition_and_task_stop(self):
        for change in ('competing_decision','stop'):
            if change=='stop':
                self.tearDown();self.setUp()
            self.configure(1)
            self.returned(coverage=['P0'])
            other=self.returned('other-return',coverage=['P0'])
            prepare=s.prepare_decision
            def interrupt(*args):
                prepared=prepare(*args)
                if change=='competing_decision':
                    self.dispose('chosen-use',returned=other['id'],scope=['P0'])
                else:
                    snapshot=t.read_task(self.root,'task')['task']
                    self.call(self.lead,'pause',{'id':'hold-task','task_id':'task',
                        'expected_task_revision':snapshot['revision'],'summary':'Stop the original task',
                        'evidence_ref':self.evidence})
                return prepared
            with self.subTest(change=change),patch.object(s,'prepare_decision',side_effect=interrupt):
                with self.assertRaisesRegex(t.WorkConflict,'Relevant original evidence'):
                    self.dispose(scope=['P0'],acceptance='final')
            snapshot=t.read_task(self.root,'task')
            self.assertEqual(len(snapshot['dispositions']),int(change=='competing_decision'))
            if change=='stop':
                self.assertEqual(snapshot['task']['state'],'paused')

    def test_pending_retry_partial_delivery_concurrent_completion_and_reopen(self):
        ids=self.configure();note=self.save('same-completion-note',{'reason':'Complete the original agreed scope'})['path']
        first=self.completion(note);again=self.completion(note)
        self.assertEqual(first['status'],'completion_pending');self.assertEqual(first['completion_id'],again['completion_id'])
        self.returned(coverage=ids[:5]);self.adopt(ids=ids[:5],acceptance='stage')
        self.assertEqual(s.read_scope(self.root,'task')['remaining_count'],9)
        checks=self.cli('work-check','--request','request','--for','handoff')
        self.assertTrue(checks['ready'],checks['missing'])
        self.adopt(ids=ids[:5],checks=checks['check_ref']['path'])
        remaining=s.read_scope(self.root,'task')
        self.assertEqual(remaining['remaining_scope'],ids[5:])
        self.assertEqual(remaining['filters']['unassigned']['total'],0)
        self.assertEqual(self.completion(note)['status'],'completion_pending')
        self.returned(ident='rest',coverage=ids[5:]);self.adopt('rest',ids[5:])
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.completion(note),range(2)))
        self.assertTrue(all(r['status']=='completed' for r in results));self.assertEqual(results[0]['completion_id'],results[1]['completion_id'])
        same=self.completion(note);self.assertEqual(same['completion_id'],results[0]['completion_id'])
        self.cli('work-reopen','--task','task','--note',note)
        value={'items':[{'scope_id':'P9','kind':'deliverable','label':'New necessary responsibility','source_refs':[self.brief]}],
            'verified_scope_ids':['P9'],'source_ref':self.brief}
        path=self.save('added-scope',value)['path'];self.cli('work-scope','--task','task','--apply',path)
        reopened=self.completion(note)
        self.assertEqual(reopened['status'],'completion_pending');self.assertNotEqual(reopened['completion_id'],first['completion_id'])
        self.request(ident='second',scope=['P9'])
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:s.set_contract(c,self.root,'request','second',self.agreement('evidence',[self.brief]),self.brief,self.lead)
        self.returned(ident='new-result',request_id='second',coverage=['P9']);self.adopt('new-result',['P9'])
        finished=self.completion(note);self.assertEqual(finished['status'],'completed');self.assertNotEqual(finished['lifecycle'],same['lifecycle'])
        events=t.read_task(self.root,'task')['events'];success=[e for e in events if e['kind']=='complete']
        self.assertEqual(len(success),2)
        self.assertTrue(all(e['payload']['id'].startswith(e['kind']+':') for e in events if e['kind'] in ('complete','completion-pending')))
    def test_unchanged_transaction_pending_and_turn_identity_reuse(self):
        self.configure(1);payload={'id':'caller-same-id','task_id':'task','evidence_ref':self.evidence,'summary':'Finish original responsibility'}
        first=self.call(self.lead,'complete',payload)
        with patch.dict(os.environ,{'CODEX_TURN_ID':'another-turn'}):again=self.call(self.lead,'complete',payload)
        self.assertEqual(first['completion_id'],again['completion_id'])
        self.returned(coverage=['P0']);self.adopt(ids=['P0'])
        done=self.call(self.lead,'complete',payload)
        self.assertEqual(done['status'],'completed');self.assertNotEqual(done['completion_id'],first['completion_id'])
    def test_draft_missing_information_blocks_new_dispatch_but_not_return(self):
        self.configure(1)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:s.set_contract(c,self.root,'request','request',{'purpose':'Draft'},self.brief,self.lead)
        check=self.cli('work-check','--request','request','--for','dispatch')
        self.assertFalse(check['ready']);self.assertTrue(any(m['id'].endswith('.contract.endpoint') for m in check['missing']))
        report=self.save('partial-failure',{'error':'External source unavailable; useful local evidence retained'})['path']
        returned=self.cli('work-return','--request','request','--result',report,'--scope','P0',actor='worker')
        self.assertEqual(returned['coverage'],['P0'])
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:s.set_contract(c,self.root,'request','request',self.agreement('evidence',[self.brief]),self.brief,self.lead)
        self.assertTrue(self.cli('work-check','--request','request','--for','dispatch')['ready'])

    def test_public_subdelegation_only_overrides_child_endpoint_and_keeps_frozen_parent(self):
        self.configure(2,'submitted')
        brief=self.root/'narrow-brief.md'
        brief.write_text('Create the first draft and retain the original qualifications.\n')
        delta=self.save('draft-only',{'endpoint':'saved_draft'})
        created=self.cli('work-request','--task','task','--brief',str(brief),'--scope','P0','--contract',delta['path'])
        check=self.cli('work-check','--request',created['id'],'--for','dispatch')
        self.assertTrue(check['ready'],check['missing'])
        terms=check['effective_delegations'][created['id']]
        self.assertEqual(terms['body']['endpoint'],'saved_draft')
        self.assertIn('No unrelated',terms['body']['scope_boundary']['out_of_scope'])
        self.assertEqual(terms['body']['authority_refs'],[self.evidence])
        self.assertEqual(self.cli('work-contract','--request',created['id'])['effective'],terms)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'task','task',{'purpose':'A later, broader use','endpoint':'published'},self.brief,self.lead)
        self.assertEqual(self.cli('work-contract','--request',created['id'])['effective'],terms)
        outside=self.cli('work-request','--task','task','--brief',str(brief),'--scope','outside','--contract',delta['path'])
        self.assertNotEqual(outside['id'],created['id'])
        denied=self.cli('work-check','--request',outside['id'],'--for','dispatch')
        self.assertFalse(denied['ready'])
        self.assertTrue(any(row['id'].endswith('.scope') for row in denied['missing']))

    def test_ordinary_author_report_can_be_final_adopted_by_exact_lead_scope(self):
        self.configure(2)
        report=self.root/'plain-scope.md'
        report.write_text('P0 的原测量清单已经交付，P1 的年度分母仍待提供。\n')
        adopted_note=self.root/'use.md'
        adopted_note.write_text('采用原 P0 测量证据；P1 仍未完成。\n')
        returned=self.cli('work-return','--request','request','--result',str(report),actor='worker')
        self.assertEqual(returned['coverage'],[])
        action=['work-dispose','--return-id',returned['id'],'--decision',str(adopted_note),'--use','adopt','--acceptance','final']
        self.assertEqual(self.cli(*action,'--scope','P2',check=False)['code'],'coverage_required')
        self.cli(*action,'--scope','P0')
        progress=self.cli('work-show','--id','request')['request_progress']['request']
        self.assertEqual(progress['remaining_scope'],['P1'])
        self.assertEqual(self.cli('work-scope','--task','task')['remaining_scope'],['P1'])
        self.assertEqual(t.read_return(self.root,returned['id'])['coverage'],[])
        self.cli('work-pause','--task','task','--note',str(adopted_note))
        self.assertEqual(self.cli(*action,'--scope','P1',check=False)['code'],'original_stopped')
    def candidate_delivery(self,endpoint,body=None,native_report=None,*,index_opinion=True,public_workflow=False):
        self.configure(1,endpoint)
        body=body or {'processDataSet':{'processInformation':{'dataSetInformation':{'common:UUID':'synthetic-object'}},
            'administrativeInformation':{'publicationAndOwnership':{'common:dataSetVersion':'01.00.000'}},
            'exchanges':{'exchange':[{'@dataSetInternalID':'0','meanAmount':'1','resultingAmount':'1'}]}}}
        from delivery_evidence import body_identity,content_digest
        kind,object_id,version=body_identity(body)
        candidate=self.save('candidate',body);artifact={'id':'candidate','version':version,**candidate}
        native=self.save('native-validation',native_report or {'status':'completed','completeness':{'truncated':False,'hasMore':False}})
        gate=self.save('gate-report',{'schema':'data-workspace.lca-gate.v4','inputs':{candidate['path']:candidate['sha256']},'outcome':'warning',
            'checks':[{'status':'passed','native_report':Path(native['path']).name,'native_report_sha256':native['sha256']}],
            'not_checked':['Scientific quantity and bilingual meaning require original independent opinion']})
        packet=self.save('review-packet',{'review_scope':'Review the exact candidate and bounded original use, units and bilingual meaning',
            'artifact_refs':[artifact],'evidence_refs':[self.brief]})
        self.call(self.lead,'request',{'id':'review','task_id':'task','kind':'review','recipient_role':'executor','recipient':self.reviewer,
            'input_ref':packet,'summary':'Independent scientific use assessment','plan_revision':1,'required_scope':['P0'],'independent':True,'parent_request_id':'request'})
        if index_opinion:
            opinion=self.save('opinion',{'conclusion':'supported_with_limits','limits':'A local synthetic fixture only; not scientific production'})
        else:
            from task_ledger.sessions import reference
            plain=self.root/'opinion.md'
            plain.write_text('我核对了原换算。干基数量可以复算，请在候选说明里明确这个用法及边界。\n',encoding='utf-8')
            opinion=reference(self.root,plain)
        if public_workflow:
            original_opinion=self.cli('work-return','--request','review','--result',opinion['path'],actor='reviewer')
        else:
            original_opinion=self.call(self.reviewer,'return',{'id':'opinion','request_id':'review','result_ref':opinion,
                'summary':'Supported with stated limits','artifact_refs':[],'coverage':['P0'],'remaining_scope':[]})
        if index_opinion:
            self.cli('work-review-record','--return-id',original_opinion['id'],'--conclusion','supported_with_limits','--basis',opinion['path'])
        receipts=[]
        if endpoint in ('saved_draft','submitted','published'):
            context={'actor':{'id':'account' if public_workflow else 'synthetic-owner'},
                'environment':'https://isolated.invalid' if public_workflow else 'https://example.invalid','requestId':'synthetic-request',
                'observation':{'source':'remote','time':'2026-01-01T12:00:01Z'}}
            subject={'type':kind,'id':object_id,'version':version,'stateCode':100 if endpoint=='published' else 20 if endpoint=='submitted' else 0,
                'modifiedAt':'2026-01-01T12:00:00Z'}
            receipts.append(self.save('native-save',{'schemaVersion':1,'operation':'dataset process create','status':'completed','outcome':'dataset_created','error':None,
                'context':context,'data':{'subject':subject,'contentDigest':content_digest(body)}}))
            receipts.append(self.save('native-readback',{'schemaVersion':1,'operation':'dataset process get','status':'completed','outcome':'dataset_read',
                'context':context,'data':{'subject':subject,'content':body},'completeness':{'returned':1,'truncated':False,'hasMore':False,'completeContent':True}}))
            if endpoint in ('submitted','published'):
                receipts.append(self.save('native-submission',{'schemaVersion':1,'operation':'review submit','status':'accepted','outcome':'review_submitted','error':None,
                    'context':context,'data':{'request':{'target':subject},'reviewId':'fixture-review',
                    'receipt':{'data':{'review':{'id':'fixture-review','created_at':'2026-01-01T12:00:00Z'}}}}}))
        refs=[self.brief,gate,*receipts]
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'request','request',self.agreement(endpoint,refs),self.brief,self.lead)
        if public_workflow:
            return candidate,original_opinion
        final=self.save('author-handling',{'response':'Read original independent limits, checked current candidate and retained exact intended use'})
        self.call(self.worker,'return',{'id':'return','request_id':'request','result_ref':final,'summary':'Original final with author handling',
            'artifact_refs':[artifact,{'id':'gate','version':'v1',**gate}],'coverage':['P0'],'remaining_scope':[]})
        return candidate,receipts

    def test_public_return_and_final_adoption_carry_through_native_review_and_next_owner(self):
        import harness
        from platform_review import submission as batch
        from delivery_evidence import body_identity
        from review_fixtures import process
        from test_wave_delivery import LocalReviewReceiver

        body=process()
        candidate,opinion=self.candidate_delivery('saved_draft',body,index_opinion=False,public_workflow=True)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'task','task',self.agreement('submitted'),self.brief,self.lead)
        report=self.root/'author-result.md'
        report.write_text('P0 uses the verified dry-mass conversion. I retained this use and the original moisture limits.\n')
        returned=self.cli('work-return','--request','request','--result',str(report),'--candidate',candidate['path'],actor='worker')
        self.assertEqual(returned['coverage'],[])
        use=self.root/'adoption.md'
        use.write_text('Adopt P0 for the bounded dry-product comparison, using the reviewed conversion and stated limits.\n')
        adopted=self.cli('work-dispose','--return-id',returned['id'],'--use','adopt','--acceptance','final','--scope','P0',
                         '--decision',str(use),'--apply-review',opinion['id'],'--handling',str(report))
        self.assertEqual(s.read_scope(self.root,'task')['remaining_count'],1)
        target=body_identity(body)
        native=self.save('native-review-request',{'requestId':'final-p0','expectedActor':'account',
            'expectedEnvironment':'https://isolated.invalid','targets':[dict(zip(('type','id','version'),target))],'includeRelated':True})
        authorize=self.save('permission-for-root',{'id':'authorize-p0','scope':{'task':'task','object':'/'.join(target),'action':'submit',
            'account':'account','project':'project'},'subject':batch.SUBMISSION,'statement':'allow',
            'source_ref':{'path':self.evidence['path'],'sha256':self.evidence['sha256'],'locator':'file','decided_by':'lead'},
            'details':{'operator':self.lead,'authorized_related':[]}})
        self.cli('work-decision-record','--decision',authorize['path'])
        selection={'adoption':adopted['id'],'request':native['path']}
        batch_input=self.save('selected-roots',{'roots':[selection]})['path']
        driver=LocalReviewReceiver(self,[body],missing='final-p0')
        def submit(*args):
            with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),patch.object(batch,'NativeReview',return_value=driver),patch('builtins.print') as output:
                harness.main(['--root',str(self.root),'work-submit-batch','--task','task',*args])
            return json.loads(output.call_args.args[0])['roots'][0]

        direct=submit('--adoption',adopted['id'],'--request',native['path'])
        self.assertEqual(direct,submit('--input',batch_input))
        self.assertEqual(direct['state'],'ready')
        self.assertEqual(submit('--input',batch_input,'--execute')['state'],'unknown')
        self.assertEqual(self.cli('work-show','--id','request')['review_submissions'][0]['state'],'unknown')
        completed=submit('--input',batch_input,'--execute')
        self.assertEqual(completed['state'],'accepted')
        self.assertEqual(driver.sent,['final-p0'])
        platform_handoff=self.cli('work-show','--id','request')['review_submissions'][0]['handoff']
        self.assertEqual(platform_handoff,completed['handoff'])
        self.assertEqual(platform_handoff['read_command'],'review get --id review-final-p0')
        review,_=driver.get_review(platform_handoff['review_id'])
        self.assertEqual(review['data']['review']['data_id'],target[1])
        self.assertEqual(self.completion(str(use))['status'],'completed')

    def test_scope_submission_stays_open_after_child_draft_and_advances_on_root_receipt(self):
        candidate,_=self.candidate_delivery('saved_draft')
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'task','task',self.agreement('submitted'),self.brief,self.lead)
        self.adopt(ids=['P0'])
        item=s.read_scope(self.root,'task')['scope_items'][0]
        self.assertEqual((item['accepted_endpoint'],item['endpoint'],item['state']),('saved_draft','submitted','active'))
        note=self.save('complete-mixed',{'evidence':'Original parent closure'})['path']
        self.assertEqual(self.completion(note)['status'],'completion_pending')
        context={'actor':{'id':'synthetic-owner'},'environment':'https://example.invalid','requestId':'new-request',
                 'observation':{'source':'remote','time':'2026-01-01T12:00:01Z'}}
        subject={'type':'process','id':'synthetic-object','version':'01.00.000','stateCode':20,
                 'modifiedAt':'2026-01-01T12:00:00Z'}
        after=self.save('after-review',{'schemaVersion':1,'operation':'dataset process get','status':'completed',
            'outcome':'dataset_read','context':context,'data':{'subject':subject,'content':json.loads(Path(candidate['path']).read_bytes())},
            'completeness':{'returned':1,'truncated':False,'hasMore':False}})
        root=self.save('root-review',{'schemaVersion':1,'operation':'review submit','status':'accepted','outcome':'review_submitted',
            'context':context,'data':{'request':{'target':subject},'reviewId':'root',
            'receipt':{'data':{'review':{'id':'root','created_at':'2026-01-01T12:00:00Z'}}}}})
        with contextlib.closing(t._connect(self.root,True)) as c:
            report=t._decode(t._row(c,'work_returns','return'))['result_ref']['path']
        self.cli('work-return','--request','request','--result',report,'--artifact',after['path'],actor='worker')
        status=self.cli('work-check','--request','request','--for','handoff')['scope_endpoints']['return']['P0']
        self.assertEqual(status['checked_endpoint'],'saved_draft')
        self.cli('work-return','--request','request','--result',report,'--artifact',root['path'],actor='worker')
        self.adopt(ids=['P0'])
        self.assertEqual(s.read_scope(self.root,'task')['scope_items'][0]['state'],'submitted')
        self.assertEqual(self.completion(note)['status'],'completed')

    def test_historical_acceptance_keeps_missing_plan_authority_visible(self):
        self.candidate_delivery('submitted')
        task=t.read_task(self.root,'task')['task']
        old=Path(task['plan_ref']['path']); ref=task['plan_ref']
        original=s.read_contract(self.root,'request','request')['contract']
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'request','request',{**original['body'],'authority_refs':[ref,self.evidence]},self.brief,self.lead)
        t.store.plan_history_path(old,ref['sha256']).unlink()
        old.write_text('Current working page with a different purpose statement.')
        check=self.cli('work-check','--request','request','--for','handoff')
        self.assertTrue(check['ready'],check['missing'])
        history=next(item for item in check['checks'] if item['id'].endswith('.named_authority'))
        self.assertEqual(history['status'],'historical_unavailable')
        self.assertTrue(any(r['status']=='unknown' and r['ref']['sha256']==ref['sha256'] for r in check['sources']))
        self.adopt(ids=['P0'])
        self.assertEqual(s.read_scope(self.root,'task')['scope_items'][0]['state'],'submitted')

    def test_explicit_current_candidate_preserves_older_version_and_plan_as_evidence(self):
        candidate,_=self.candidate_delivery('candidate')
        with contextlib.closing(t._connect(self.root,True)) as c:
            task=t._decode(t._row(c,'work_tasks','task'))
            report=t._decode(t._row(c,'work_returns','return'))['result_ref']['path']
            plan=task['plan_ref']
        original=Path(plan['path']).read_bytes()
        Path(plan['path']).write_text('Current plan now includes the next authorized scope.')
        self.assertEqual(Path(t.evidence_ref(self.root,plan)['path']).read_bytes(),original)
        new=json.loads(Path(candidate['path']).read_bytes())
        new['processDataSet']['administrativeInformation']['publicationAndOwnership']['common:dataSetVersion']='01.00.001'
        successor=self.save('exact-successor',new)
        old_get=self.save('old-get',json.loads(Path(candidate['path']).read_bytes()))
        self.cli('work-return','--request','request','--result',report,'--candidate',successor['path'],
                 '--artifact',old_get['path'],actor='worker')
        result=self.cli('work-check','--request','request','--for','handoff','--summary')
        self.assertEqual([row['subject'][2] for row in result['members']],['01.00.001'])
        self.assertEqual(result['members'][0]['checks']['scientific_review']['status'],'unknown')
        self.assertEqual([item['return_id'] for item in result['members'][0]['prior_opinions_with_changes']],['opinion'])
        self.assertEqual(Path(candidate['path']).read_bytes(),Path(old_get['path']).read_bytes())
    def test_all_five_endpoints_have_distinct_evidence(self):
        # Separate original responsibility for each endpoint, never promote a
        # submission into publication or force an evidence task to save data.
        for endpoint in s.ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                if endpoint!='evidence':
                    self.tearDown();self.setUp()
                    self.candidate_delivery(endpoint)
                else:self.configure(1);self.returned(coverage=['P0'])
                check=self.cli('work-check','--request','request','--for','handoff')
                self.assertTrue(check['ready'],check['missing'])
                self.adopt(ids=['P0'])
                view=s.read_scope(self.root,'task');self.assertEqual(view['remaining_count'],0)
                self.assertEqual(view['scope_items'][0]['state'],'submitted' if endpoint in ('submitted','published') else 'accepted')

    def test_explicit_complete_rereview_resolves_prior_issue_without_latest_wins(self):
        self.complete_rereview()

    def test_plain_review_supersession_reuses_its_frozen_index_with_supporting_reports(self):
        self.complete_rereview(plain=True)

    def test_complete_rereview_cannot_drop_a_previous_dataset_target(self):
        self.complete_rereview(drop_prior_target=True)

    def complete_rereview(self,plain=False,drop_prior_target=False):
        candidate,_=self.candidate_delivery('candidate')
        artifact={'id':'candidate','version':'01.00.000',**candidate}
        support=self.root/'complete-scientific-rationale.md'
        support.write_text('Original supporting report, retaining the complete use limitations.')
        support_ref={'id':'support','path':str(support),'sha256':hashlib.sha256(support.read_bytes()).hexdigest()}
        for name,conclusion in [('issue','material_issue'),('resolved','supported_with_limits')]:
            packet_artifacts=[artifact,support_ref]
            if drop_prior_target and name=='issue':
                other=json.loads(Path(candidate['path']).read_bytes())
                other['processDataSet']['processInformation']['dataSetInformation']['common:UUID']='other-prior-target'
                packet_artifacts.append({'id':'other','version':'01.00.000',**self.save('other-prior-target',other)})
            if plain:
                brief=self.root/(name+'-original-review.md');brief.write_text('Read the complete explicit candidate and supporting report from this original contract.')
                packet={'path':str(brief),'sha256':hashlib.sha256(brief.read_bytes()).hexdigest()}
            else:
                packet=self.save(name+'-packet',{'review_scope':'Recheck the same complete candidate and original purpose; address the prior issue.',
                    'artifact_refs':packet_artifacts,'evidence_refs':[self.brief]})
            self.call(self.lead,'request',{'id':name+'-review','task_id':'task','kind':'review','recipient_role':'executor',
                'recipient':self.reviewer,'input_ref':packet,'summary':'Full independent re-review','plan_revision':1,
                'required_scope':['P0'],'independent':True,'parent_request_id':'request',
                **({'delivery_contract':self.agreement('evidence',[candidate,support_ref])} if plain else {})})
            opinion=self.save(name+'-opinion',{'conclusion':conclusion,'finding':'Original issue' if name=='issue' else 'All original issues were assessed and resolved; full original use limits retained.'})
            self.call(self.reviewer,'return',{'id':name+'-return','request_id':name+'-review','result_ref':opinion,
                'summary':'Actual original full opinion','artifact_refs':[],'coverage':['P0'],'remaining_scope':[]})
            self.cli('work-review-record','--return-id',name+'-return','--conclusion',conclusion,'--basis',opinion['path'])
        current=self.cli('work-check','--request','request','--for','handoff')
        self.assertTrue(any(c['id'].endswith('.scientific_review') for c in current['missing']),
            'A newer supported opinion alone must not erase an unresolved original issue')
        if drop_prior_target:
            refused=self.cli('work-review-record','--return-id','resolved-return','--conclusion','supported_with_limits',
                '--basis',opinion['path'],'--supersedes','issue-return',check=False)
            self.assertIn('every prior target identity',refused['error'])
            return
        self.cli('work-review-record','--return-id','resolved-return','--conclusion','supported_with_limits',
            '--basis',opinion['path'],'--supersedes','issue-return')
        checked=self.cli('work-check','--request','request','--for','handoff')
        self.assertFalse(any(c['id'].endswith('.scientific_review') for c in checked['missing']))
        history=__import__('harness_tasks').read_task(self.root,'task')['events']
        original=[e for e in history if e['kind']=='review-conclusion-indexed' and e['payload']['return_id']=='issue-return']
        self.assertEqual(original[-1]['payload']['conclusion'],'material_issue')
    def test_changed_quantity_invalidates_exact_report_and_review_binding(self):
        candidate,_=self.candidate_delivery('candidate')
        self.assertTrue(self.cli('work-check','--request','request','--for','handoff')['ready'])
        body=json.loads(Path(candidate['path']).read_bytes());body['processDataSet']['exchanges']['exchange'][0]['meanAmount']='2'
        Path(candidate['path']).write_text(json.dumps(body))
        changed=self.cli('work-check','--request','request','--for','handoff')
        self.assertFalse(changed['ready'])
        ids=[x['id'] for x in changed['missing']]
        for name in ('structure_references','scientific_review'):
            self.assertTrue(any(name in ident for ident in ids),(name,ids))
        self.assertTrue((self.root/'gate-report.json').is_file())

    def changed_review_delivery(self, *, index_opinion=True):
        original,_=self.candidate_delivery('candidate',index_opinion=index_opinion)
        body=json.loads(Path(original['path']).read_bytes())
        body['processDataSet']['processInformation']['dataSetInformation']['common:generalComment']='Author clarified the original use limit after reading the independent opinion.'
        current=self.save('current-canonical-candidate',body)
        report=self.save('current-author-handling',{'response':'Handled the stated clarification while preserving the original quantities and use limits.'})
        validation=self.save('current-native-validation',{'type':'process','id':'synthetic-object','version':'01.00.000',
            'status':'passed','ok':True,'payload_sha256':__import__('delivery_evidence').content_digest(body)})
        self.call(self.worker,'return',{'id':'changed-return','request_id':'request','result_ref':report,
            'summary':'Actual author handling and exact revised candidate','artifact_refs':[
                {'id':'candidate','version':'01.00.000',**current},{'id':'native','version':'current',**validation}],
            'coverage':['P0'],'remaining_scope':[]})
        note=self.save('current-use-adoption',{'use':'I read the full original independent opinion and author handling. The original conditions apply to this exact clarified body for the original use; this is my adoption judgment, not a new reviewer conclusion.'})
        argv=['work-dispose','--return-id','changed-return','--use','adopt','--decision',note['path'],'--scope','P0',
            '--apply-review','opinion','--handling',report['path']]
        return original,current,report,argv

    def test_one_plain_opinion_and_author_report_reach_final_adoption_without_index(self):
        _,_,report,argv=self.changed_review_delivery(index_opinion=False)
        from scientific_review.rounds import opinion_view
        with contextlib.closing(t._connect(self.root,True)) as c:
            handoff=opinion_view(c,self.root,t.read_request(self.root,'review'))
            self.assertIn('我核对了原换算',handoff['opinion']['text'])
            self.assertEqual(handoff['opinion']['tracking']['budget_state'],'used')
        self.cli(*argv,'--acceptance','final')
        after=self.cli('work-check','--request','request','--for','handoff','--summary')
        self.assertTrue(after['ready'],after['remaining_checks'])
        applied,=after['members'][0]['lead_applications']
        self.assertEqual(applied['review_return_id'],'opinion')
        self.assertEqual(applied['handling_refs'],[report])
        self.assertEqual(after['members'][0]['checks']['scientific_review']['source_refs'],[handoff['ref']])
        all_checks=self.cli('work-check','--request','request','--for','handoff')['checks']
        self.assertFalse(any(row['id'].endswith(('unit_quantity_and_meaning','bilingual_meaning')) for row in all_checks))
        self.assertFalse(any(e['kind']=='review-conclusion-indexed' for e in t.read_task(self.root,'task')['events']))

    def test_explicit_lead_application_binds_handling_body_and_use_without_rewriting_review(self):
        original,current,report,argv=self.changed_review_delivery()
        before=self.cli('work-check','--request','request','--for','handoff')
        self.assertFalse(before['ready']);self.assertTrue(any(c['id'].endswith('.scientific_review') for c in before['missing']))
        first=self.cli(*argv);again=self.cli(*argv);self.assertEqual(first['id'],again['id'])
        after=self.cli('work-check','--request','request','--for','handoff');self.assertTrue(after['ready'],after['missing'])
        detail=json.loads(next(c['detail'] for c in after['checks'] if '.member.' in c['id']))
        self.assertEqual(detail['scientific_applications'][0]['adopter']['id'],'lead')
        self.assertTrue(detail['prior_opinions_with_body_changes'])
        indexed=[e for e in t.read_task(self.root,'task')['events'] if e['kind']=='review-conclusion-indexed']
        self.assertEqual(indexed[0]['payload']['conclusion'],'supported_with_limits')
        packet=json.loads(Path(indexed[0]['payload']['submission_ref']['path']).read_bytes())
        self.assertEqual(packet['artifact_refs'][0]['sha256'],original['sha256'])
        self.adopt('changed-return',ids=['P0'])
        self.assertEqual(s.read_scope(self.root,'task')['remaining_count'],0)
        changed=json.loads(Path(current['path']).read_bytes());changed['processDataSet']['exchanges']['exchange'][0]['meanAmount']='2'
        later=self.save('later-quantity',changed)
        later_report=self.save('later-quantity-report',{'response':'A new quantity proposal needs its own evidence and applicability.'})
        self.call(self.worker,'return',{'id':'later-return','request_id':'request','result_ref':later_report,'summary':'New quantity requires its own exact evidence',
            'artifact_refs':[{'id':'candidate','version':'01.00.000',**later}],'coverage':['P0'],'remaining_scope':[]})
        later_check=self.cli('work-check','--request','request','--for','handoff')
        self.assertFalse(later_check['ready'])
        self.assertTrue(any(c['id'].endswith('.scientific_review') for c in later_check['missing']))

    def test_final_adoption_accepts_explicit_application_in_same_call(self):
        _, _, _, argv = self.changed_review_delivery()
        before = self.cli('work-check', '--request', 'request', '--for', 'handoff')
        self.assertFalse(before['ready'])
        self.cli(*argv, '--acceptance', 'final')
        after = self.cli('work-check', '--request', 'request', '--for', 'handoff')
        self.assertTrue(after['ready'], after['missing'])
        member = json.loads(next(c['detail'] for c in after['checks'] if '.member.' in c['id']))
        self.assertEqual(member['automatic_review_match'], 'unknown')
        self.assertEqual(member['scientific_applications'][0]['adopter']['id'], 'lead')

    def test_handoff_summary_points_to_current_and_prior_evidence(self):
        original, current, author_report, argv = self.changed_review_delivery()
        command = ('work-check', '--request', 'request', '--for', 'handoff')
        brief = self.cli(*command, '--summary')
        complete = json.loads(Path(brief['check_ref']['path']).read_bytes())
        self.assertFalse(brief['ready'])
        self.assertEqual(brief['ready'], complete['ready'])
        item, = brief['members']
        self.assertEqual(item['candidate_ref']['sha256'], current['sha256'])
        self.assertEqual(item['author_report_refs'], [author_report])
        self.assertEqual(item['prior_opinions_with_changes'][0]['frozen_candidate']['sha256'], original['sha256'])
        self.assertEqual(item['checks']['scientific_review']['status'], 'unknown')
        self.cli(*argv, '--acceptance', 'final')
        brief = self.cli(*command, '--summary')
        self.assertTrue(brief['ready'])
        self.assertEqual(brief['members'][0]['lead_applications'][0]['handling_refs'], [author_report])

    def test_final_application_cannot_cover_missing_frozen_bytes_or_remote_save(self):
        _, _, _, argv = self.changed_review_delivery()
        with t.transaction(self.root) as c:
            agreement = s.contract(c, 'request', 'request')['body']
            agreement['endpoint'] = 'saved_draft'
            agreement['authority_refs'] = [self.evidence]
            s.set_contract(c, self.root, 'request', 'request', agreement, self.brief, self.lead)
        refused = self.cli(*argv, '--acceptance', 'final', check=False)
        self.assertEqual(refused['code'], 'delivery_not_ready')
        self.assertEqual(t.read_task(self.root, 'task')['dispositions'], [])
        packet_ref = t.read_request(self.root, 'review')['input_ref']
        packet = json.loads(Path(packet_ref['path']).read_bytes())
        Path(packet['artifact_refs'][0]['path']).unlink()
        refused = self.cli(*argv, '--acceptance', 'final', check=False)
        self.assertIn('frozen review artifacts', refused['error'])

    def test_review_body_preserves_semantic_context_across_wrapping(self):
        from delivery_evidence import body_identity, same_body, same_review_body
        original, _ = self.candidate_delivery('candidate')
        body = json.loads(Path(original['path']).read_bytes())
        kind, ident, version = body_identity(body)
        context = {'basis': 'per cubic metre'}
        wrapped = {'type': kind, 'id': ident, 'version': version, 'json_ordered': body,
                   'semantic_context': context}
        self.assertEqual(body_identity(wrapped), (kind, ident, version))
        self.assertTrue(same_review_body(wrapped, {**body, 'semantic_context': context}))
        different = {**wrapped, 'semantic_context': {'basis': 'per kilogram'}}
        self.assertTrue(same_body(wrapped, different))
        self.assertFalse(same_review_body(wrapped, different))
        self.assertIsNone(body_identity({**wrapped, 'id': 'different-target'}))

    def test_review_application_requires_owner_handling_and_preserves_withdrawal(self):
        _,_,_,argv=self.changed_review_delivery()
        refused=self.cli(*argv,actor='worker',check=False);self.assertEqual(refused['code'],'scope_authority_required')
        refused=self.cli(*argv[:-2],check=False);self.assertIn('complete original author handling',refused['error'])
        self.cli(*argv)
        hold=self.save('hold-current-use',{'reason':'Withdraw this use pending a new scientific question'})
        self.cli('work-dispose','--return-id','changed-return','--use','hold','--decision',hold['path'],'--scope','P0')
        self.assertFalse(self.cli('work-check','--request','request','--for','handoff')['ready'])

    def test_review_application_does_not_float_to_a_new_purpose(self):
        _,_,_,argv=self.changed_review_delivery();self.cli(*argv)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            agreement=s.contract(c,'request','request')['body'];agreement['purpose']='A materially different consumer use'
            s.set_contract(c,self.root,'request','request',agreement,self.brief,self.lead)
        check=self.cli('work-check','--request','request','--for','handoff')
        self.assertTrue(any(c['id'].endswith('.scientific_review') for c in check['missing']))

    def test_review_application_ignores_technical_authority_change_but_requires_intact_handling(self):
        _,_,report,argv=self.changed_review_delivery();self.cli(*argv)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            agreement=s.contract(c,'request','request')['body'];agreement['endpoint']='saved_draft';agreement['authority_refs']=[self.evidence]
            s.set_contract(c,self.root,'request','request',agreement,self.brief,self.lead)
        check=self.cli('work-check','--request','request','--for','handoff')
        self.assertFalse(any(c['id'].endswith('.scientific_review') for c in check['missing']))
        Path(report['path']).write_text('Different handling body, not the accepted original.')
        changed=self.cli('work-check','--request','request','--for','handoff')
        self.assertTrue(any(c['id'].endswith('.scientific_review') for c in changed['missing']))

    def test_not_assessed_cannot_become_an_applied_independent_assessment(self):
        _,_,_,argv=self.changed_review_delivery()
        packet=t.read_request(self.root,'review')['input_ref']
        self.call(self.lead,'request',{'id':'unassessed-review','task_id':'task','kind':'review','recipient_role':'executor','recipient':self.reviewer,
            'input_ref':packet,'summary':'Independent unassessed original','plan_revision':1,'required_scope':['P0'],'independent':True,'parent_request_id':'request'})
        opinion=self.save('unassessed-opinion',{'conclusion':'not_assessed','reason':'No assessment of this candidate was performed.'})
        self.call(self.reviewer,'return',{'id':'unassessed-opinion','request_id':'unassessed-review','result_ref':opinion,'summary':'Not assessed',
            'artifact_refs':[],'coverage':['P0'],'remaining_scope':[]})
        self.cli('work-review-record','--return-id','unassessed-opinion','--conclusion','not_assessed','--basis',opinion['path'])
        refused=self.cli(*argv,'--apply-review','unassessed-opinion',check=False)
        self.assertIn('not_assessed',refused['error'])
        self.assertEqual(t.read_task(self.root,'task')['dispositions'],[])

    def test_review_application_cannot_borrow_an_unrelated_target_identity(self):
        _,current,report,argv=self.changed_review_delivery()
        body=json.loads(Path(current['path']).read_bytes());body['processDataSet']['processInformation']['dataSetInformation']['common:UUID']='unreviewed-object'
        unrelated=self.save('unreviewed-object',body)
        unrelated_report=self.save('unrelated-report',{'response':'This is a separate new scientific target.'})
        actual=self.call(self.worker,'return',{'id':'unrelated-return','request_id':'request','result_ref':unrelated_report,'summary':'A different scientific target',
            'artifact_refs':[{'id':'candidate','version':'01.00.000',**unrelated}],'coverage':['P0'],'remaining_scope':[]})
        argv[argv.index('changed-return')]=actual['id']
        refused=self.cli(*argv,check=False);self.assertIn('no canonical target',refused['error'])

    def test_original_material_issue_remains_until_its_handling_is_explicitly_adopted(self):
        _,_,report,argv=self.changed_review_delivery()
        original=t.read_request(self.root,'review')['input_ref']
        self.call(self.lead,'request',{'id':'issue-review','task_id':'task','kind':'review','recipient_role':'executor','recipient':self.reviewer,
            'input_ref':original,'summary':'Independent original clarification issue','plan_revision':1,'required_scope':['P0'],'independent':True,'parent_request_id':'request'})
        issue=self.save('original-material-issue',{'conclusion':'material_issue','finding':'Clarify the current use limitation before adoption.'})
        self.call(self.reviewer,'return',{'id':'issue-opinion','request_id':'issue-review','result_ref':issue,'summary':'Original material issue',
            'artifact_refs':[],'coverage':['P0'],'remaining_scope':[]})
        self.cli('work-review-record','--return-id','issue-opinion','--conclusion','material_issue','--basis',issue['path'])
        self.cli(*argv)
        self.assertFalse(self.cli('work-check','--request','request','--for','handoff')['ready'])
        self.cli(*argv,'--apply-review','issue-opinion')
        after=self.cli('work-check','--request','request','--for','handoff');self.assertTrue(after['ready'],after['missing'])
        original_index=[e for e in t.read_task(self.root,'task')['events'] if e['kind']=='review-conclusion-indexed' and e['payload']['return_id']=='issue-opinion']
        self.assertEqual(original_index[-1]['payload']['conclusion'],'material_issue')
    def test_partial_readback_and_forged_check_cannot_be_final_adoption(self):
        _,receipts=self.candidate_delivery('saved_draft')
        body=json.loads(Path(receipts[-1]['path']).read_bytes());body['completeness']['hasMore']=True
        Path(receipts[-1]['path']).write_text(json.dumps(body))
        checked=self.cli('work-check','--request','request','--for','handoff');self.assertFalse(checked['ready'])
        forged={k:v for k,v in checked.items() if k!='check_ref'};forged['ready']=True;forged['missing']=[]
        path=self.save('forged-check',forged)['path'];note=self.evidence['path']
        result=self.cli('work-dispose','--return-id','return','--use','adopt','--decision',note,'--acceptance','final','--scope','P0','--checks',path,check=False)
        self.assertEqual(result['code'],'stale_or_untrusted_check')
        self.assertEqual(s.read_scope(self.root,'task')['remaining_count'],1)

    def test_quantity_change_keeps_originals_and_demands_affected_evidence(self):
        from review_fixtures import process, save
        body = process()
        source = self.root/'original-process.json'
        save(source, body)
        original_bytes = source.read_bytes()
        candidate,_=self.candidate_delivery('candidate',body)
        original_gate=(self.root/'gate-report.json').read_bytes()
        original_opinion=(self.root/'opinion.json').read_bytes()
        self.assertTrue(self.cli('work-check','--request','request','--for','handoff')['ready'])
        changed=json.loads(Path(candidate['path']).read_bytes())
        exchanges=changed['processDataSet']['exchanges']['exchange']
        for index,exchange in enumerate(exchanges):
            try:amount=Decimal(str(exchange['meanAmount']))
            except (KeyError,InvalidOperation):continue
            if amount.is_finite():break
        else:self.fail('The original candidate has no explicit finite exchange quantity')
        old_amount=exchange['meanAmount'];old_resulting=exchange.get('resultingAmount')
        exchange['meanAmount']=str(amount+Decimal('1'))
        self.assertNotEqual(Decimal(str(old_amount)),Decimal(exchange['meanAmount']))
        self.assertEqual(exchange.get('resultingAmount'),old_resulting)
        Path(candidate['path']).write_text(json.dumps(changed))
        check=self.cli('work-check','--request','request','--for','handoff')
        self.assertFalse(check['ready'])
        missing=[item['id'] for item in check['missing']]
        for name in ('structure_references','scientific_review'):
            self.assertTrue(any(name in ident for ident in missing),(index,name,missing))
        self.assertEqual((self.root/'gate-report.json').read_bytes(),original_gate)
        self.assertEqual((self.root/'opinion.json').read_bytes(),original_opinion)
        self.assertEqual(source.read_bytes(),original_bytes)

    def test_gate_label_cannot_override_a_complete_native_failure(self):
        failures=[{'status':'failed','error':{'code':'NATIVE_VALIDATION_FAILED'}},
            {'ok':False},{'status':'completed','data':{'validation':{'ok':False}}},
            {'status':'unknown'},{'status':'completed','data':{'validation':{'status':'incomplete'}}},
            {'status':'completed','completeness':['not a complete native envelope']}]
        for index,body in enumerate(failures):
            with self.subTest(native=body):
                if index:self.tearDown();self.setUp()
                self.candidate_delivery('candidate',native_report=body)
                check=self.cli('work-check','--request','request','--for','handoff')
                self.assertFalse(check['ready'])
                self.assertTrue(any('structure_references' in row['id'] for row in check['missing']))
                self.assertTrue(all(source['status']=='pass' for source in check['sources']),
                    'This must reject the actual native conclusion, not stale source hashes')

    def test_corrected_reading_of_original_report_gets_new_check_without_overwriting_old(self):
        self.candidate_delivery('candidate',native_report={'ok':False})
        with patch('lca_gate.native_validation_failed',return_value=False):
            prior=s.check(self.root,'request','request','handoff')
        self.assertTrue(prior['ready'])
        retained=Path(prior['check_ref']['path']).read_bytes()
        current=s.check(self.root,'request','request','handoff')
        self.assertFalse(current['ready'])
        self.assertNotEqual(current['check_ref']['path'],prior['check_ref']['path'])
        self.assertEqual(Path(prior['check_ref']['path']).read_bytes(),retained)

    def test_paused_completion_note_is_durable_idempotent_and_never_resumes_task(self):
        self.configure(1);self.returned(coverage=['P0']);self.adopt(ids=['P0'])
        note=self.save('same-paused-completion-note',{'reason':'Original completion explanation'})['path']
        self.cli('work-pause','--task','task','--note',self.evidence['path'])
        first=self.completion(note);again=self.completion(note)
        self.assertEqual(first['state'],'paused');self.assertTrue(first['stop_preserved'])
        self.assertEqual(first['completion_id'],again['completion_id'])
        self.assertTrue(any(item['id']=='original_stop' for item in first['missing']))
        events=[e for e in t.read_task(self.root,'task')['events'] if e['kind']=='completion-pending']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['payload']['evidence_ref']['path'],note)
        self.cli('work-resume','--task','task','--note',self.evidence['path'])
        self.assertEqual(self.completion(note)['status'],'completed')

    def test_plain_public_review_uses_explicit_contract_candidates_and_keeps_index_binding(self):
        self.configure(1,'candidate')
        body={'processDataSet':{'processInformation':{'dataSetInformation':{'common:UUID':'plain-review-object'}},
            'administrativeInformation':{'publicationAndOwnership':{'common:dataSetVersion':'01.00.000'}},
            'exchanges':{'exchange':[{'@dataSetInternalID':'0','meanAmount':'1','resultingAmount':'1'}]}}}
        candidate=self.save('plain-review-candidate',body)
        artifact={'id':'plain-review-candidate','version':'01.00.000',**candidate}
        native=self.save('plain-review-validation',{'status':'completed'})
        gate=self.save('plain-review-gate',{'schema':'data-workspace.lca-gate.v4','inputs':{candidate['path']:candidate['sha256']},
            'outcome':'checks_completed','checks':[{'status':'passed','native_report':Path(native['path']).name,'native_report_sha256':native['sha256']}]})
        brief=self.root/'plain-review.md';brief.write_text('Assess the exact candidate and its declared units and use. Explain the conditions directly to the author.')
        review_contract=self.save('plain-review-contract',self.agreement('evidence',[candidate]))
        created=self.cli('work-request','--task','task','--review-of','request','--brief',str(brief),'--contract',review_contract['path'])
        self.assertEqual(created['input_ref']['path'],str(brief))
        from production_review import independent_task_input
        frozen=independent_task_input(self.root,created,'scientific_reviewer',t)['frozen_submission_ref']
        packet=json.loads(Path(frozen['path']).read_bytes())
        self.assertEqual(packet['original_brief_ref'],created['input_ref'])
        self.assertEqual(json.loads(Path(packet['artifact_refs'][0]['path']).read_bytes()),body)
        opinion=self.root/'plain-opinion.md';opinion.write_text('Supported for the original local fixture use; no scientific production claim.')
        returned=self.cli('work-return','--request',created['id'],'--result',str(opinion),actor='reviewer')
        self.assertEqual(brief.read_text(),'Assess the exact candidate and its declared units and use. Explain the conditions directly to the author.')
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'request','request',self.agreement('candidate',[gate]),self.brief,self.lead)
        report=self.save('plain-author-handling',{'response':'Handled the original opinion and retained its exact use limits'})
        self.call(self.worker,'return',{'id':'return','request_id':'request','result_ref':report,'summary':'Original author handling',
            'artifact_refs':[artifact],'coverage':['P0'],'remaining_scope':[]})
        self.cli('work-dispose','--return-id','return','--decision',self.evidence['path'],'--use','adopt',
                 '--scope','P0','--apply-review',returned['id'],'--handling',report['path'])
        self.assertTrue(self.cli('work-check','--request','request','--for','handoff')['ready'])
        indexed=self.cli('work-review-record','--return-id',returned['id'],'--conclusion','supported_with_limits','--basis',str(opinion))
        self.assertEqual(indexed['submission_ref'],frozen)
        changed=json.loads(Path(candidate['path']).read_bytes())
        changed['processDataSet']['exchanges']['exchange'][0]['meanAmount']='2'
        Path(candidate['path']).write_text(json.dumps(changed))
        same=self.cli('work-review-record','--return-id',returned['id'],'--conclusion','supported_with_limits','--basis',str(opinion))
        self.assertEqual(same['submission_ref'],indexed['submission_ref'])
        self.assertFalse(self.cli('work-check','--request','request','--for','handoff')['ready'])
        self.assertEqual(json.loads(Path(packet['artifact_refs'][0]['path']).read_bytes()),body)
