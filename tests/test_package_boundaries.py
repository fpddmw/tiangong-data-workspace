"""Declared responsibility/endpoint facts; these fixtures do no scientific judging."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import harness_scope as scope
import harness_tasks as tasks
import harness_snapshot as snapshot
import test_controlled_autonomy as acceptance

BIN=Path(__file__).resolve().parents[1]/'bin'


class PackageBoundaryTests(unittest.TestCase):
    setUp=acceptance.AcceptanceTests.setUp;tearDown=acceptance.AcceptanceTests.tearDown
    actor=acceptance.AcceptanceTests.actor;save=acceptance.AcceptanceTests.save;call=acceptance.AcceptanceTests.call
    request=acceptance.AcceptanceTests.request;returned=acceptance.AcceptanceTests.returned;dispose=acceptance.AcceptanceTests.dispose
    cli=acceptance.AcceptanceTests.cli;agreement=acceptance.AcceptanceTests.agreement;configure=acceptance.AcceptanceTests.configure
    adopt=acceptance.AcceptanceTests.adopt;completion=acceptance.AcceptanceTests.completion
    candidate_delivery=acceptance.AcceptanceTests.candidate_delivery

    def relation(self, ids, *, endpoints=None):
        value={'items':[{'scope_id':sid,'kind':'deliverable','label':sid,'source_refs':[self.brief],
                         **({'target_endpoint':endpoint} if endpoint else {})}
                        for sid,endpoint in (endpoints or {}).items()],
               'links':[{'request_id':'request','scope_ids':ids}],
               'source_ref':self.brief}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):
            return scope.apply_scope(self.root,'task',value,self.lead)

    def test_finite_child_can_finish_while_parent_keeps_unassigned_powder_supply(self):
        self.configure(2);self.relation(['P0'],endpoints={'P0':'evidence','P1':'candidate'})
        parent=self.agreement('candidate');parent['purpose']='Finished powder supply, including calcining and powder delivery'
        child=self.agreement('evidence');child['purpose']='Bounded kiln-gate calcining evidence'
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),tasks.transaction(self.root) as c:
            scope.set_contract(c,self.root,'task','task',parent,self.brief,self.lead)
            scope.set_contract(c,self.root,'request','request',child,self.brief,self.lead)
        # Synthetic observed binding, through the original typed receipt path.
        # A request name alone must never authenticate a native Hook caller.
        config=self.save('hook-fixture-config',{'offline_fixture':True})
        self.call(self.worker,'run-intent',{'id':'observed-fixture','request_id':'request',
            'config_ref':config,'input_refs':[self.brief],'target_host':'codex','operation':'start'})
        native={**self.worker,'turn_id':'fixture-turn'}
        receipt=self.save('hook-fixture-receipt',{'schema':'data-workspace.agent-runtime-observation.v1',
            'run_id':'observed-fixture','request_id':'request','host':'codex','source':'native_rpc',
            'operation':'start','native_identity':native,'config_sha256':config['sha256'],
            'native_status':'completed','observed_at':'2026-11-17T00:00:00Z'})
        self.call(self.worker,'run-bind',{'id':'observed-fixture','native_identity':native,'receipt':receipt})
        self.call(self.worker,'run-outcome',{'id':'observed-fixture','outcome':'succeeded','receipt':receipt})
        self.returned(coverage=['P0']);self.adopt(ids=['P0'])
        self.assertTrue(self.cli('work-check','--request','request','--for','complete')['ready'])
        view=self.cli('work-scope','--task','task')
        self.assertEqual(view['remaining_scope'],['P1'])
        self.assertEqual(view['filters']['unassigned']['scope_ids'],['P1'])
        self.assertFalse(self.cli('work-check','--task','task','--for','complete')['ready'])
        bundle=snapshot.bundle(self.root,self.worker,request_id='request');card=snapshot.render(bundle)
        self.assertIn('父任务声明终点=candidate',card);self.assertIn('子请求终点=evidence',card)
        bound=bundle['tasks'][0]
        self.assertEqual(bound['request_contract']['body']['purpose'],child['purpose'])
        self.assertEqual(bound['request_brief'],self.brief)
        self.assertEqual(bound['scope']['contract']['body']['purpose'],parent['purpose'])

    def test_limited_child_wording_and_final_report_do_not_remove_registered_emissions(self):
        self.configure(2)
        child=self.agreement();child['scope_boundary']['in_scope']='Only drying energy; supported_with_limits'
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),tasks.transaction(self.root) as c:
            scope.set_contract(c,self.root,'request','request',child,self.brief,self.lead)
        self.returned(coverage=['P0'],summary='Final: all work completed, supported_with_limits')
        self.adopt(ids=['P0'])
        view=self.cli('work-scope','--task','task')
        self.assertEqual(view['remaining_scope'],['P1'])
        self.assertEqual(next(r for r in view['scope_items'] if r['scope_id']=='P1')['state'],'active')
        self.assertEqual(self.completion(self.brief['path'])['status'],'completion_pending')

    def test_shared_supply_is_one_original_file_and_each_consumer_adopts_independently(self):
        self.configure(1)
        shared=self.save('one-shared-module',{'identity':'one selected supplier version','limits':'Original scope only'})
        artifact={'id':'file:'+shared['sha256'],'version':shared['sha256'],**shared}
        self.call(self.main,'create',{'id':'task-two','owner_actor':self.lead,'original_ref':self.brief,
            'plan_ref':self.plan,'summary':'Second consumer, separate use','required_scope':[],'delivery_contract':self.agreement()})
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):
            scope.apply_scope(self.root,'task-two',{'items':[{'scope_id':'supplier','kind':'requirement',
                'label':'Consumer must adopt this exact supplier','source_refs':[shared],'target_endpoint':'evidence'}],
                'verified_scope_ids':['supplier'],'source_ref':self.brief},self.lead)
        self.request(ident='consumer-two',task_id='task-two',scope=['supplier'],delivery_contract=self.agreement())
        self.returned(ident='consumer-one-return',coverage=['P0'],artifact_refs=[artifact])
        self.returned(ident='consumer-two-return',request_id='consumer-two',coverage=['supplier'],artifact_refs=[artifact])
        self.adopt('consumer-one-return',['P0'])
        reported=self.cli('work-finding','--task','task-two','--report',self.evidence['path'])
        self.assertEqual(reported['task_id'],'task-two')
        second=self.cli('work-scope','--task','task-two')
        self.assertEqual(second['remaining_scope'],['supplier'])
        # Cross-Task request links cannot impersonate a consumer's own adoption.
        bad=self.save('foreign-link',{'items':[],'links':[{'request_id':'consumer-two','scope_ids':['P0']}],
                                     'source_ref':self.brief})
        refused=self.cli('work-scope','--task','task','--apply',bad['path'],check=False)
        self.assertIn('error',refused)
        self.adopt('consumer-two-return',['supplier'])
        self.assertEqual(self.cli('work-scope','--task','task-two')['remaining_scope'],[])
        current=self.agreement(refs=[shared])
        newer=self.save('new-supplier-version',{'identity':'new supplier version','limits':'Different wet basis'})
        current['interfaces']={'text':'This use will separately verify the newer wet-basis interface.','refs':[newer]}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),tasks.transaction(self.root) as c:
            scope.set_contract(c,self.root,'request','consumer-two',current,self.brief,self.lead)
        unchanged=self.cli('work-scope','--task','task')
        self.assertEqual(unchanged['remaining_scope'],[])
        self.assertEqual(self.cli('work-check','--task','task','--for','complete')['ready'],True)
        self.assertEqual(Path(shared['path']).read_text(),json.dumps({'identity':'one selected supplier version','limits':'Original scope only'}))

    def test_connected_same_name_powder_keeps_owner_declared_incompatibility_open(self):
        self.configure(2)
        supplier=self.save('supplier-powder',{'name':'Powder','water_fraction':0.15,'basis':'wet mass'})
        offered=self.save('consumer-powder',{'name':'Powder','water_fraction':0.00,'basis':'dry mass'})
        connection=self.save('fixture-graph',{'direction':'supply-to-consumer','vertices':[supplier,offered]})
        agreement=self.agreement(refs=[connection])
        reason='The connected same-name powder requires a measured wet-to-dry conversion for this use.'
        agreement['interfaces']={'text':reason,'refs':[supplier,offered]}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),tasks.transaction(self.root) as c:
            scope.set_contract(c,self.root,'request','request',agreement,self.brief,self.lead)
        self.returned(coverage=['P0'],summary='Graph exists. Wet-to-dry conversion remains an open requirement.')
        self.adopt(ids=['P0'])
        checked=self.cli('work-check','--task','task','--for','complete')
        self.assertFalse(checked['ready'])
        self.assertEqual(self.cli('work-scope','--task','task')['remaining_scope'],['P1'])
        recorded=self.cli('work-contract','--request','request')
        self.assertEqual(recorded['contract']['body']['interfaces']['text'],reason)

    def test_all_returned_still_shows_unassigned_and_uncovered_in_every_view(self):
        self.configure(3);self.relation(['P0','P2']);self.returned(coverage=['P0'])
        views=[self.cli('work-show','--id','task')['scope'],self.cli('work-scope','--task','task'),
               self.cli('work-packages','--task','task')['task_scopes'][0]]
        queried=subprocess.run([sys.executable,str(BIN/'context.py'),'--root',str(self.root),'--task','task'],
            env={**os.environ,'CODEX_THREAD_ID':'lead'},capture_output=True,text=True)
        self.assertEqual(queried.returncode,0,queried.stderr)
        views.append(json.loads(queried.stdout)['current']['work']['scope'])
        for view in views:
            self.assertEqual(view['remaining_count'],3)
            self.assertEqual(view['filters']['unassigned']['scope_ids'],['P1'])
            self.assertIn('P2',view['filters']['no_explicit_coverage']['scope_ids'])
        card=snapshot.render(snapshot.bundle(self.root,self.main))
        self.assertIn('全部未达终点余项=3',card);self.assertIn('无承接1',card)

    def test_exploration_evidence_can_finish_without_forcing_process_model_or_lcia(self):
        self.configure(1)
        self.returned(coverage=['P0'],artifact_refs=[],summary='Bounded evidence supports no further investment; no dataset manufactured')
        self.adopt(ids=['P0'])
        checked=self.cli('work-check','--task','task','--for','complete')
        self.assertTrue(checked['ready'],checked['missing'])
        self.assertEqual(self.completion(self.brief['path'])['status'],'completed')

    def test_finite_process_candidate_finishes_at_its_declared_endpoint(self):
        self.candidate_delivery('candidate')
        self.adopt(ids=['P0'])
        checked=self.cli('work-check','--task','task','--for','complete')
        self.assertTrue(checked['ready'],checked['missing'])
        self.assertEqual(self.completion(self.brief['path'])['status'],'completed')

    def test_plain_original_report_pause_and_unknown_survive_views_without_replay(self):
        self.configure(1)
        original=self.root/'old-original.txt';raw='Original plain report, no JSON contract.\nScientific limitations remain.\n';original.write_text(raw)
        result=self.cli('work-return','--request','request','--result',str(original),actor='worker')
        config=self.save('retained-config',{'engineering_fixture':True})
        self.call(self.worker,'run-intent',{'id':'old-unknown','request_id':'request','config_ref':config,
            'input_refs':[self.brief],'target_host':'codex','operation':'start'})
        self.call(self.worker,'run-outcome',{'id':'old-unknown','outcome':'unknown','summary':'Original interrupted attempt'})
        self.cli('work-pause','--task','task','--note',self.brief['path'])
        self.cli('work-show','--id','task');self.cli('work-packages','--task','task')
        self.assertEqual(original.read_text(),raw)
        self.assertEqual(tasks.read_run(self.root,'old-unknown')['outcome'],'unknown')
        self.assertEqual(tasks.read_task(self.root,'task')['task']['state'],'paused')
        checked=self.cli('work-check','--task','task','--for','complete')
        self.assertFalse(checked['ready']);self.assertTrue(any(c['id']=='original_stop' for c in checked['missing']))


if __name__=='__main__':unittest.main()
