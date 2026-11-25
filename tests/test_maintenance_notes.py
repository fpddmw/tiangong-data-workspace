"""Explicit maintenance decisions on original findings; no model or remote writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_harness_tasks as fixtures
from task_ledger import maintenance
import harness_tasks as tasks


class MaintenanceNotesTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskCoreTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        f = self.f
        f.call(f.main, 'grant-role', {'id':'maintenance-role','actor':f.other,
            'org_role':'maintainer','evidence_ref':f.evidence})
        for ident in ('finding','original'):
            f.call(f.lead,'request',{'id':ident,'task_id':'task','kind':'finding','recipient':f.other,
                'recipient_role':'maintainer','input_ref':f.brief,'summary':'Shared tool failure',
                'plan_revision':1,'required_scope':[]})
        self.note=f.root/'repair.md'
        self.note.write_text('The original report now loads. The caller has not yet verified production use.\n')

    def note_as(self, actor, status, request='finding', **kwargs):
        with patch.dict(os.environ, CODEX_THREAD_ID=actor['id']):
            return maintenance.record_note(self.f.root,actor,request,self.note,status,**kwargs)

    def test_plain_repair_note_stays_open_until_explicit_verification(self):
        first=self.note_as(self.f.other,'fixed')
        self.assertEqual(self.note_as(self.f.other,'fixed')['event_id'],first['event_id'])
        self.assertIn('finding',{q['id'] for q in maintenance.queue(self.f.root)['findings']})
        self.note.write_text('Original caller read the original report and continued successfully.\n')
        result=self.note_as(self.f.lead,'verified')
        self.assertEqual(Path(result['source_ref']['path']).read_bytes(),self.note.read_bytes())
        self.assertNotIn('finding',{q['id'] for q in maintenance.queue(self.f.root)['findings']})
        self.assertEqual(tasks.read_task(self.f.root,'task')['task']['state'],'active')
        self.note_as(self.f.lead,'open')
        self.assertIn('finding',{q['id'] for q in maintenance.queue(self.f.root)['findings']})

    def test_duplicate_keeps_original_target_and_rejects_cycle(self):
        recorded = self.note_as(self.f.worker,'fixed')
        self.assertEqual(recorded['actor'], self.f.worker)
        self.note_as(self.f.other,'duplicate',duplicate_of='original')
        self.assertEqual([q['id'] for q in maintenance.queue(self.f.root)['findings']],['original'])
        with self.assertRaisesRegex(ValueError,'circular'):
            self.note_as(self.f.other,'duplicate',request='original',duplicate_of='finding')

    def test_new_observation_is_visible_without_erasing_previous_verification(self):
        self.note_as(self.f.lead,'verified')
        with tasks.transaction(self.f.root) as c:
            tasks._event(c,tasks._row(c,'work_tasks','task'),'maintenance-observation',self.f.worker,
                {'request_id':'finding','summary':'The original public input fails again','result_ref':self.f.evidence})
        item=next(q for q in maintenance.queue(self.f.root)['findings'] if q['id']=='finding')
        self.assertEqual(item['decision']['state'],'verified')
        self.assertEqual(len(item['new_observations']),1)

    def test_public_cli_records_plain_note_without_a_resolution_envelope(self):
        result=subprocess.run([sys.executable,str(fixtures.BIN/'harness.py'),'--root',str(self.f.root),
            'work-maintenance-note','--request','finding','--note',str(self.note),'--status','fixed'],
            capture_output=True,text=True,env={**os.environ,'CODEX_THREAD_ID':self.f.other['id']})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'],'fixed')
        self.assertEqual(len(tasks.read_task(self.f.root,'task')['returns']),0)
