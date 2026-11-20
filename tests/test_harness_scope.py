"""Typed scope migration, partial coverage and immutable source facts."""
import contextlib
import copy
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_harness_tasks as fixtures
import harness_scope as s
import harness_tasks as t

class ScopeTests(unittest.TestCase):
    setUp=fixtures.TaskCoreTests.setUp
    tearDown=fixtures.TaskCoreTests.tearDown
    actor=fixtures.TaskCoreTests.actor
    save=fixtures.TaskCoreTests.save
    call=fixtures.TaskCoreTests.call
    request=fixtures.TaskCoreTests.request
    returned=fixtures.TaskCoreTests.returned
    dispose=fixtures.TaskCoreTests.dispose
    def payload(self,count=9):
        return {'items':[{'scope_id':'P'+str(i),'kind':'deliverable','label':'Original responsibility '+str(i),'source_refs':[self.brief]} for i in range(count)],
                'source_ref':self.brief,'verified_scope_ids':['P'+str(i) for i in range(count)]}
    def apply(self,value,**kw):
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):return s.apply_scope(self.root,'task',value,self.lead,**kw)
    def cli(self,*argv):
        return subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'bin/harness.py'),'--root',str(self.root),*argv],
            env={**os.environ,'CODEX_THREAD_ID':'lead'},capture_output=True,text=True)
    def test_nine_assigned_five_returned_and_four_still_missing(self):
        self.apply(self.payload());self.request(scope=['P'+str(i) for i in range(9)])
        view=s.read_scope(self.root,'task')
        self.assertEqual(view['remaining_count'],9);self.assertEqual(view['filters']['unassigned']['total'],0)
        self.returned(coverage=['P'+str(i) for i in range(5)])
        view=s.read_scope(self.root,'task')
        self.assertEqual(view['remaining_count'],9)
        self.assertEqual(view['filters']['coverage_pending_adoption']['total'],5)
        self.assertEqual(view['filters']['no_explicit_coverage']['total'],4)
        self.assertTrue(all(x['state']=='active' for x in view['scope_items']))
    def test_unknown_mapping_not_unassigned_and_repeated_upsert_keeps_scope(self):
        value=self.payload(3);value['verified_scope_ids']=[]
        self.apply(value);view=s.read_scope(self.root,'task')
        self.assertTrue(all(x['state'] is None for x in view['scope_items']))
        before=t.read_task(self.root,'task')['task']['revision']
        self.assertEqual(self.apply(value)['status'],'unchanged')
        self.assertEqual(t.read_task(self.root,'task')['task']['revision'],before)
        value['items']=value['items'][:1]
        self.apply(value)
        self.assertEqual(s.read_scope(self.root,'task')['total'],3)
    def test_public_cli_and_draft_contract_atomic_registration(self):
        value=self.payload(2);path=self.save('scope-input',value)['path']
        result=self.cli('work-scope','--task','task','--apply',path,'--dry-run')
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['status'],'dry_run')
        result=self.cli('work-scope','--task','task','--apply',path)
        self.assertEqual(result.returncode,0,result.stderr)
        contract=self.save('contract',{'purpose':'Draft research','endpoint':'evidence'})['path']
        brief=self.save('new-brief',{'original':'Full original scope and background'})['path']
        created=self.cli('work-request','--task','task','--brief',brief,'--contract',contract,'--scope','P0')
        self.assertEqual(created.returncode,0,created.stderr)
        req=json.loads(created.stdout)
        self.assertEqual(req['required_scope'],['P0'])
        self.assertEqual(s.read_contract(self.root,'request',req['id'])['contract']['body']['endpoint'],'evidence')
    def test_missing_store_not_initialized_and_migration_backed_up(self):
        with self.assertRaises(t.WorkConflict):s.migrate(self.root/'missing')
        with t.transaction(self.root) as c:
            c.execute('DROP TABLE work_scope_items');c.execute('DROP TABLE work_delivery_contracts')
        migrated=s.migrate(self.root)
        self.assertTrue(Path(migrated['backup']['path']).is_file())
        self.assertEqual(s.migrate(self.root)['status'],'already_installed')
        self.assertEqual(t.read_task(self.root,'task')['task']['required_scope'],['a','b'])
    def test_concurrent_source_change_rejected_without_half_import(self):
        value=self.payload();original=s._scope_input;calls=[]
        def changed(root,doc):
            result=original(root,doc);calls.append(1)
            if len(calls)==1:Path(self.brief['path']).write_text('Concurrent source change')
            return result
        with patch.object(s,'_scope_input',side_effect=changed),self.assertRaises(ValueError):self.apply(value)
        self.assertEqual(s.read_scope(self.root,'task')['total'],2)
    def test_apple_source_has_36_requirements_and_73_branches(self):
        root=Path(__file__).resolve().parents[1]
        with (root/'materials/packages/apple-lca-delivery-20261004/outputs/coverage.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),73)
        self.assertEqual(len({r['requirement_id'] for r in rows}),36)


class DispatchBasisTests(unittest.TestCase):
    tearDown=fixtures.TaskCoreTests.tearDown
    actor=fixtures.TaskCoreTests.actor
    save=fixtures.TaskCoreTests.save
    call=fixtures.TaskCoreTests.call
    request=fixtures.TaskCoreTests.request
    returned=fixtures.TaskCoreTests.returned
    dispose=fixtures.TaskCoreTests.dispose

    def setUp(self):
        fixtures.TaskCoreTests.setUp(self)
        self.agreement={'purpose':'Read the exact frozen evidence for this request','endpoint':'evidence',
            'scope_boundary':{'in_scope':'The selected original scope','out_of_scope':'No other work or remote action'},
            'acceptance':{'text':'Return the original evidence and applicable limits','refs':[self.brief]},
            'authority_refs':[self.evidence],'interfaces':{'text':'','refs':[]}}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):
            s.apply_scope(self.root,'task',{'items':[{'scope_id':sid,'kind':'deliverable','label':sid,
                'source_refs':[self.brief]} for sid in ('P0','P1')],
                'verified_scope_ids':['P0','P1'],'source_ref':self.brief},self.lead)
        self.request(scope=['P0'],delivery_contract=self.agreement)

    def report(self):
        with contextlib.closing(t._connect(self.root,True)) as c:
            c.execute('BEGIN')
            return s.build_check(c,self.root,'request','request','dispatch')

    def test_unrelated_returns_progress_contracts_and_scopes_leave_launch_basis_unchanged(self):
        first=self.report();self.assertTrue(first['ready'])
        self.request(ident='other-request',scope=['P1'],delivery_contract=self.agreement)
        self.returned(ident='late-return',coverage=['P0'])
        self.dispose(returned='late-return',scope=['P0'])
        changed=copy.deepcopy(self.agreement);changed['purpose']='An unrelated package purpose changed'
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'request','other-request',changed,self.brief,self.lead)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):
            s.apply_scope(self.root,'task',{'items':[{'scope_id':'P1','kind':'deliverable',
                'label':'Updated unrelated responsibility','source_refs':[self.brief]}],
                'source_ref':self.brief},self.lead)
        self.assertEqual(self.report(),first)

    def test_exact_contract_scope_and_stop_changes_still_change_launch_basis(self):
        first=self.report()
        changed=copy.deepcopy(self.agreement);changed['purpose']='Changed exact reviewer question'
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}),t.transaction(self.root) as c:
            s.set_contract(c,self.root,'request','request',changed,self.brief,self.lead)
        contract_changed=self.report()
        self.assertTrue(contract_changed['ready'])
        self.assertNotEqual(contract_changed['input_fingerprint'],first['input_fingerprint'])
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'lead'}):
            s.apply_scope(self.root,'task',{'items':[{'scope_id':'P0','kind':'deliverable',
                'label':'Changed exact scope boundary','source_refs':[self.brief]}],
                'source_ref':self.brief},self.lead)
        scope_changed=self.report()
        self.assertNotEqual(scope_changed['input_fingerprint'],contract_changed['input_fingerprint'])
        revision=t._read_one(self.root,'work_tasks','task')['revision']
        self.call(self.main,'pause',{'id':'explicit-pause','task_id':'task','expected_task_revision':revision,'evidence_ref':self.evidence,
            'summary':'Original sponsor paused this exact task'})
        stopped=self.report()
        self.assertFalse(stopped['ready'])
        self.assertNotEqual(stopped['input_fingerprint'],scope_changed['input_fingerprint'])

    def test_changed_exact_input_or_authority_bytes_remain_unready(self):
        first=self.report()
        path=Path(self.brief['path']);original=path.read_bytes()
        path.write_text('The frozen scientific candidate/question changed')
        changed=self.report()
        self.assertFalse(changed['ready'])
        self.assertNotEqual(changed['input_fingerprint'],first['input_fingerprint'])
        path.write_bytes(original)
        Path(self.evidence['path']).write_text('Authority source changed')
        self.assertFalse(self.report()['ready'])
