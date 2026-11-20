"""Whole responsibility denominators, complete blocks, navigation and retrieval."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import harness_scope as s
import harness_snapshot as snap
import harness_tasks as t
import test_harness_tasks as fixtures

BIN=Path(__file__).resolve().parents[1]/'bin'

class SnapshotTests(unittest.TestCase):
    setUp=fixtures.TaskCoreTests.setUp;tearDown=fixtures.TaskCoreTests.tearDown
    actor=fixtures.TaskCoreTests.actor;save=fixtures.TaskCoreTests.save;call=fixtures.TaskCoreTests.call
    request=fixtures.TaskCoreTests.request;returned=fixtures.TaskCoreTests.returned;dispose=fixtures.TaskCoreTests.dispose
    def scoped(self,task='task',count=9,kind='deliverable'):
        ids=[('P' if kind=='deliverable' else 'A')+str(i) for i in range(count)]
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):
            s.apply_scope(self.root,task,{'items':[{'scope_id':sid,'kind':kind,'label':'Responsibility '+sid,'source_refs':[self.brief]} for sid in ids],
                'verified_scope_ids':ids,'source_ref':self.brief},self.lead)
        return ids
    def bundle(self):return snap.bundle(self.root,self.main)
    def test_assigned_nine_with_only_five_returns_remains_in_cli_dsh_and_card(self):
        ids=self.scoped();self.request(scope=ids);self.returned(coverage=ids[:5])
        view=snap.read(self.root,task_id='task',limit=1)['work']
        self.assertEqual(view['scope']['remaining_count'],9);self.assertEqual(view['scope']['filters']['unassigned']['total'],0)
        self.assertEqual(view['scope']['filters']['no_explicit_coverage']['scope_ids'],ids[5:])
        packages=snap.package_snapshot(self.root,'task')
        self.assertEqual(packages['task_scopes'][0]['remaining_count'],9)
        card=snap.render(self.bundle());self.assertIn('全部未达终点余项=9',card);self.assertIn('有返回待最终采用5',card)
        self.assertIn('尚无明确覆盖4',card);self.assertLessEqual(len(card.encode()),8192)
        self.assertEqual(len(view['scope']['scope_items']),9)
    def test_two_normal_tasks_keep_both_complete_base_cards(self):
        self.scoped()
        self.call(self.main,'create',{'id':'task-two','owner_actor':self.lead,'original_ref':self.brief,'plan_ref':self.plan,
            'summary':'Second complete original goal','required_scope':[]})
        self.scoped('task-two',36,'requirement')
        card=snap.render(self.bundle())
        self.assertIn('T1 task=task',card);self.assertIn('T2 task=task-two',card)
        self.assertIn('交付责任9',card);self.assertIn('需求36',card)
        self.assertNotIn('完整任务基础卡未展开',card);self.assertLessEqual(len(card.encode()),8192)
    def test_whole_tail_condition_is_inlined_or_whole_goal_is_retrievable(self):
        self.scoped( count=1)
        tail='允许保存草稿，但尚未授权正式送审；以干基为分母，除非原来源明确另报。'
        body={'purpose':'原目标 '+('长科学说明 '*3000)+tail,'endpoint':'candidate',
            'scope_boundary':{'in_scope':'bounded','out_of_scope':'No expansion'},'acceptance':{'text':'Actual use and limits','refs':[]},'authority_refs':[]}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:s.set_contract(c,self.root,'task','task',body,self.brief,self.lead)
        first=self.bundle();card=snap.render(first)
        if tail not in card:self.assertIn('原目标、验收及范围边界未展开',card)
        self.assertEqual(s.read_contract(self.root,'task','task')['contract']['body']['purpose'],body['purpose'])
        body['purpose']+=' 最后限制变更，数量不变。'
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:s.set_contract(c,self.root,'task','task',body,self.brief,self.lead)
        second=self.bundle();self.assertNotEqual(first['snapshot_hash'],second['snapshot_hash'])
        self.assertEqual(first['tasks'][0]['scope']['counts'],second['tasks'][0]['scope']['counts'])
    def test_extreme_ids_and_paths_switch_whole_navigation_without_short_ids(self):
        self.scoped(count=1);value=self.bundle();view=value['tasks'][0]
        huge='VERY_LONG_NATIVE_TASK:'+('科学全文'*10000)
        view['task']['id']=huge;view['detail_command']='/'+('超长路径/'*10000)+'work-show --id '+huge
        card=snap.render(value)
        self.assertIn('完整任务基础卡未展开',card);self.assertNotIn('VERY_LONG_NATIVE_TASK:',card)
        self.assertIn('work-list',card);self.assertNotIn('--mine',card);self.assertLessEqual(len(card.encode()),8192)
    def test_history_and_output_keep_full_scope_and_complete_events(self):
        self.scoped()
        for n in range(14):
            self.request(ident='request-'+str(n),scope=['P0'])
        path=self.root/'full-view.json'
        result=subprocess.run([sys.executable,str(BIN/'harness.py'),'--root',str(self.root),'work-show','--id','task','--limit','2','--output',str(path)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(result.stdout);saved=json.loads(path.read_bytes())
        self.assertEqual(saved['scope']['total'],9);self.assertEqual(receipt['counts']['deliverable']['total'],9)
        self.assertEqual(saved['pagination']['shown'],2);self.assertEqual(saved['pagination']['total'],14)
        self.assertEqual(len(saved['history']['events']),2)
        ids=[];offset=0
        while True:
            page=snap.read(self.root,task_id='task',limit=2,offset=offset)['work']['history']
            ids.extend(e['id'] for e in page['events'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(len(ids),saved['history']['total'])
        with self.assertRaises(ValueError):snap.export(self.root,'.control/harness.sqlite',saved)
    def test_optional_package_metadata_failure_preserves_original_pointer_and_hook_stays_ledger_only(self):
        self.scoped(count=1)
        ref=self.save('unrelated-native-receipt',{'run_id':'another-run','request_id':'request',
            'result':{'native_goal_observation':{'goal':{'phase':'complete'}}}})
        value=snap._package_native_metadata(self.root,{'id':'exact-run','request_id':'request',
            'input_refs':[],'latest_receipt':ref})
        self.assertIsNone(value['goalObservation'])
        self.assertEqual(value['issues'][0]['ref'],ref)
        self.assertIn('exact run',value['issues'][0]['reason'])
        other=self.save('mismatched-goal-binding',{'run_id':'exact-run','request_id':'request',
            'result':{'native_goal_observation':{'sessionId':'another-native','turn':1,
                'source':'dsh_live_agent','goal':{'phase':'complete'}}}})
        invalid=snap._package_native_metadata(self.root,{'id':'exact-run','request_id':'request',
            'native_identity':{'id':'expected-native','turn_id':'1'},'input_refs':[],'latest_receipt':other})
        self.assertIsNone(invalid['goalObservation'])
        self.assertIn('exact native Session',invalid['issues'][0]['reason'])
        with patch.object(snap,'_package_native_metadata',side_effect=AssertionError('Hook must not scan receipts')):
            self.assertEqual(self.bundle()['tasks'][0]['scope']['remaining_count'],1)
