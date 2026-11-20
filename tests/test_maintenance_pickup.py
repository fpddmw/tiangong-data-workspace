"""Explicit report -> original board -> maintainer -> actual verification."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_harness_tasks as fixtures
import harness_tasks as tasks
import harness_maintenance as maintenance
import harness_feedback as feedback


class MaintenancePickupTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskCoreTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.request()
        self.maintainer = self.f.actor('maintenance')
        self.f.call(self.f.main, 'grant-role', {'id': 'maintainer', 'actor': self.maintainer,
                    'org_role': 'maintainer', 'evidence_ref': self.f.evidence})

    def cli(self, actor, *args, succeeds=True):
        r = subprocess.run([sys.executable, str(Path(tasks.__file__).with_name('harness.py')),
                            '--root', str(self.f.root), *args], capture_output=True, text=True,
                           env={**os.environ, 'CODEX_THREAD_ID': actor['id']})
        self.assertEqual(r.returncode == 0, succeeds, r.stdout + r.stderr)
        return json.loads(r.stdout) if succeeds else r.stderr

    def result(self, **extra):
        return self.f.save('normal-result', {'contribution': 'Original workflow hit a tool defect',
            'coverage': [], 'artifacts': [], 'changes': [], 'remaining_issues': ['Scientific evidence still missing'],
            'maintenance_findings': [{'summary': 'Wrapper fails; preserve original request and retry after candidate validation',
                                      'evidence': ['Exact input and runtime are in this original result']}], **extra})

    def test_explicit_report_pickup_and_resolution_preserve_plain_return(self):
        f = self.f
        ref = self.result()
        returned = self.cli(f.worker,'work-return','--request','request','--result',ref['path'])
        self.assertNotIn('finding_request_ids', returned)
        self.assertEqual(maintenance.queue(f.root)['findings'], [])
        ident = self.cli(f.worker, 'work-finding', '--task', 'task', '--report', ref['path'])['id']
        self.assertEqual(maintenance.queue(f.root)['findings'][0]['pickup'],'unassigned')
        self.cli(f.worker,'work-claim-finding','--request',ident)
        claim = self.cli(self.maintainer,'work-claim-finding','--request',ident)
        self.assertEqual(claim,self.cli(self.maintainer,'work-claim-finding','--request',ident))
        note = f.root / 'repair.md'; note.write_text('Reproduced and repaired the shared wrapper. Original caller verified it.')
        self.cli(self.maintainer,'work-maintenance-note','--request',ident,'--note',str(note),'--status','verified')
        self.assertEqual(maintenance.queue(f.root)['findings'],[])
        self.cli(f.worker,'work-return','--request','request','--result',ref['path'])
        self.assertEqual(maintenance.queue(f.root)['findings'],[])
        self.assertEqual(tasks.read_task(f.root,'task')['dispositions'],[])

    def test_return_fields_never_create_findings_or_format_work(self):
        ref = self.result(maintenance_findings=[{'summary': 'Valid first observation'}, {'wrong': 'missing summary'}])
        returned = self.cli(self.f.worker, 'work-return', '--request', 'request', '--result', ref['path'])
        view = tasks.read_task(self.f.root, 'task')
        self.assertEqual(view['returns'][0]['id'], returned['id'])
        self.assertEqual(view['returns'][0]['result_ref'], ref)
        self.assertNotIn('optional_issues', returned)
        self.assertEqual(maintenance.queue(self.f.root)['findings'], [])
        self.assertEqual([r['id'] for r in view['requests']], ['request'])

    def test_changed_or_missing_report_does_not_prevent_maintenance_pickup(self):
        f = self.f
        for change in ('append', 'missing'):
            with self.subTest(change=change):
                report = f.root / f'finding-{change}.md'
                report.write_text('Original shared tool failure.\n')
                req = self.cli(f.worker, 'work-finding', '--task', 'task', '--report', str(report))
                original_ref = req['input_ref']
                if change == 'append':
                    report.write_text(report.read_text() + 'A later observation.\n')
                else:
                    report.unlink()
                claimed = self.cli(self.maintainer, 'work-claim-finding', '--request', req['id'])
                self.assertEqual(claimed['input_ref'], original_ref)
                item = next(r for r in maintenance.queue(f.root)['findings'] if r['id'] == req['id'])
                self.assertEqual(item['input_integrity']['status'], 'unavailable_or_changed')
                self.assertEqual(item['summary'], req['summary'])
                with self.assertRaises((OSError, ValueError)):
                    tasks.evidence_ref(f.root, original_ref)
                note = f.root / f'diagnosis-{change}.md'
                note.write_text('Examined the recorded symptom and current evidence; repair remains open.')
                self.cli(self.maintainer, 'work-maintenance-note', '--request', req['id'],
                         '--note', str(note), '--status', 'open')
                self.cli(f.other, 'work-claim-finding', '--request', req['id'])
        self.assertEqual(tasks.read_task(f.root, 'task')['runs'], [])

    def test_early_report_executor_without_recipient_and_duplicate_claim(self):
        f = self.f
        ref = f.save('early-finding', {'summary': 'Failed before a normal result is possible'})
        req = self.cli(f.worker, 'work-finding', '--task', 'task', '--report', ref['path'])
        self.cli(self.maintainer, 'work-claim-finding', '--request', req['id'])
        again = self.cli(f.worker, 'work-finding', '--task', 'task', '--report', ref['path'])
        self.assertEqual(req['id'], again['id'])
        f.call(f.main, 'grant-role', {'id': 'other-maintainer', 'actor': f.other,
            'org_role': 'maintainer', 'evidence_ref': f.evidence})
        self.cli(f.other, 'work-claim-finding', '--request', req['id'])
        self.cli(f.reviewer, 'work-finding', '--task', 'task', '--report', ref['path'])
        self.cli(f.worker, 'work-claim-finding', '--request', 'request', succeeds=False)

    def test_explicit_reassignment_preserves_original_finding_and_returns(self):
        f = self.f
        ref = f.save('early-transfer', {'summary': 'Original routing fault'})
        req = self.cli(f.worker, 'work-finding', '--task', 'task', '--report', ref['path'])
        self.cli(self.maintainer, 'work-claim-finding', '--request', req['id'])
        original = f.save('old-diagnosis', {'contribution': 'Original diagnosis remains valid', 'coverage': [], 'artifacts': []})
        returned = self.cli(self.maintainer, 'work-return', '--request', req['id'], '--result', original['path'])
        f.call(f.main, 'grant-role', {'id': 'replacement-maintainer', 'actor': f.other,
               'org_role': 'maintainer', 'evidence_ref': f.evidence})
        doc = {'schema': 'data-workspace.maintenance-reassignment.v1', 'request_id': req['id'],
               'previous_recipient': self.maintainer, 'recipient': f.other,
               'authority': {'kind': 'user', 'thread_id': f.other['id'], 'quote': 'Take over this exact repair', 'date': '2026-11-01'}}
        authority = f.save('transfer-authority', doc)
        self.cli(f.other, 'work-claim-finding', '--request', req['id'])
        self.cli(f.other, 'work-claim-finding', '--request', req['id'], '--authorization', authority['path'])
        self.cli(f.other, 'work-claim-finding', '--request', req['id'], '--authorization', authority['path'])
        view = tasks.read_task(f.root, 'task')
        self.assertEqual([r for r in view['returns'] if r['id'] == returned['id']][0]['author_actor'], self.maintainer)
        self.assertEqual(tasks.read_request(f.root, req['id'])['recipient_actor'], f.other)
        events = [e for e in view['events'] if e['kind'] == 'maintenance-transfer']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['original_recipient'], self.maintainer)

    def test_scientific_review_findings_are_not_engineering(self):
        ref = self.result(maintenance_findings=[], findings=[{'summary': 'Boundary evidence missing'}])
        self.cli(self.f.worker, 'work-return', '--request', 'request', '--result', ref['path'])
        self.assertEqual(maintenance.queue(self.f.root)['findings'], [])

    def test_appointment_records_actual_host_identity_and_plain_original(self):
        f = self.f
        note = f.root / 'appointment.md'; note.write_text('The user appointed this session to maintain shared tools.')
        self.cli(f.other,'work-enroll','--role','maintainer','--authorization',str(note))
        self.cli(f.other,'work-enroll','--role','maintainer','--authorization',str(note))
        with patch.dict(os.environ, CODEX_THREAD_ID='other'):
            self.assertIn('maintainer',tasks.current_view(f.root,f.other)['org_roles'])
        with tasks._connect(f.root,True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM work_roles WHERE actor_key=? AND org_role='maintainer'",(tasks.actor_key(f.other),)).fetchone()[0],1)

    def test_return_after_pause_still_reports_original_friction_once(self):
        f = self.f
        f.call(f.lead, 'pause', {'id': 'pause', 'task_id': 'task', 'expected_task_revision': 1,
                              'evidence_ref': f.evidence, 'summary': 'Pause data operations'})
        ref = self.result()
        self.cli(f.worker, 'work-return', '--request', 'request', '--result', ref['path'])
        self.cli(f.worker, 'work-finding', '--task', 'task', '--report', ref['path'])
        self.assertEqual(maintenance.queue(f.root)['findings'][0]['task_state'], 'paused')
        self.assertEqual(tasks.read_task(f.root, 'task')['task']['state'], 'paused')


    def test_explicit_observations_and_recurrence_reuse_original_finding(self):
        f = self.f
        ref = self.result()
        ident = self.cli(f.worker, 'work-finding', '--task', 'task', '--report', ref['path'])['id']
        self.cli(self.maintainer, 'work-claim-finding', '--request', ident)
        update = f.root / 'observation.md'; update.write_text('Same wrapper failed on a second input.')
        self.cli(f.worker, 'work-finding', '--task', 'task', '--finding', ident, '--report', str(update))
        self.cli(f.worker, 'work-finding', '--task', 'task', '--finding', ident, '--report', str(update))
        item, = maintenance.queue(f.root)['findings']
        self.assertEqual(len(item['observations']), 1)
        note = f.root / 'verified.md'; note.write_text('The original input now succeeds.')
        self.cli(self.maintainer, 'work-maintenance-note', '--request', ident, '--note', str(note), '--status', 'verified')
        self.assertEqual(maintenance.queue(f.root)['findings'], [])
        self.cli(f.worker, 'work-finding', '--task', 'task', '--finding', ident, '--report', str(update))
        self.assertEqual(maintenance.queue(f.root)['findings'], [])
        recurrent = f.root / 'recurrence.md'; recurrent.write_text('After the next upgrade the same operation failed.')
        self.cli(f.worker, 'work-finding', '--task', 'task', '--finding', ident, '--report', str(recurrent))
        item, = maintenance.queue(f.root)['findings']
        self.assertEqual(item['id'], ident)
        self.assertEqual(len(item['observations']), 2)
        self.assertTrue(item['new_observations'])
        self.assertIn(ident, tasks.read_task(f.root, 'task')['pending_request_ids'])
        self.assertEqual(tasks.read_task(f.root, 'task')['returns'], [])

    def test_json_report_summary_is_content_not_a_required_string(self):
        report = self.f.save('report-with-domain-summary', {'summary': {'rows': 3}})
        finding = self.cli(self.f.worker, 'work-finding', '--task', 'task', '--report', report['path'])
        self.assertEqual(finding['summary'], Path(report['path']).read_text())


if __name__ == '__main__':
    unittest.main()
