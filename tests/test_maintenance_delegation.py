"""Bounded engineering uses original findings, identities and actual entry guards.

All native runs below use offline adapters in disposable fixture stores.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_harness_tasks as fixtures
import test_agent_runtime as native
import harness_tasks as t
from task_ledger import maintenance
from task_ledger import sessions
import harness_feedback as feedback
import context
import agent_runtime


class MaintenanceDelegationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskCoreTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.request()
        self.maintainer = self.f.actor('maintenance')
        self.worker = self.f.actor('engineering-sol')
        self.f.call(self.f.main, 'grant-role', {'id': 'maintainer', 'actor': self.maintainer,
            'org_role': 'maintainer', 'evidence_ref': self.f.evidence})
        report = self.f.save('original-finding', {'summary': 'Exact engineering defect, original input and return point'})
        self.parent = self.cli(self.f.worker, 'work-finding', '--task', 'task', '--report', report['path'])
        self.cli(self.maintainer, 'work-claim-finding', '--request', self.parent['id'])
        self.counter = 0

    def cli(self, actor, *args, succeeds=True):
        r = subprocess.run([sys.executable, str(Path(t.__file__).with_name('harness.py')),
            '--root', str(self.f.root), *args], capture_output=True, text=True,
            env={**os.environ, 'CODEX_THREAD_ID': actor['id']})
        self.assertEqual(r.returncode == 0, succeeds, r.stdout + r.stderr)
        return json.loads(r.stdout) if succeeds else r.stderr

    def assign(self, text='Repair the shared entry and report original-use verification.'):
        self.counter += 1
        p = self.f.root / ('repair-' + str(self.counter) + '.md'); p.write_text(text)
        return str(p)

    def delegate(self, brief=None):
        return self.cli(self.maintainer,'work-maintenance-delegate','--request',self.parent['id'],
                        '--brief',brief or self.assign(),'--recipient',self.worker['id'])

    def test_plain_assignment_reuses_original_and_keeps_science_paused(self):
        f = self.f
        f.call(f.lead,'pause',{'id':'pause','task_id':'task','expected_task_revision':1,
            'evidence_ref':f.evidence,'summary':'Pause scientific operations.'})
        brief = self.assign(); req = self.delegate(brief)
        self.assertEqual(req['id'],self.delegate(brief)['id'])
        self.assertEqual(req['recipient_actor'],self.worker)
        prepared = self.cli(self.maintainer,'work-dispatch','--request',req['id'])
        self.assertNotIn('continuation',prepared)
        note = self.assign('Repaired; the original public entry now passes.')
        self.cli(self.worker,'work-return','--request',req['id'],'--result',note)
        snapshot = t.read_task(f.root,'task')
        self.assertEqual(snapshot['task']['state'],'paused')
        self.assertEqual(snapshot['task']['owner_actor'],f.lead)
        item = maintenance.queue(f.root)['findings'][0]
        self.assertEqual(item['executions'][0]['request']['id'],req['id'])
        self.assertEqual(len(item['executions'][0]['returns']),1)
        self.assertNotIn('next_action',item['executions'][0])

    def test_new_chat_can_record_assignment_but_actual_repair_writer_is_exclusive(self):
        brief = self.assign()
        req = self.delegate(brief)
        same = self.cli(self.worker, 'work-maintenance-delegate', '--request', self.parent['id'],
                        '--brief', brief, '--recipient', self.worker['id'])
        self.assertEqual(same['id'], req['id'])
        another = self.delegate(self.assign('Another explicit assignment for the same repair'))
        config = self.f.save('runtime', native.configuration())
        def start(request, ident):
            return self.f.call(self.maintainer, 'run-intent', {'id': ident, 'request_id': request['id'],
                'plan_revision': 1, 'config_ref': config, 'input_refs': [request['input_ref']],
                'target_host': 'codex', 'operation': 'start'})
        start(req, 'repair-run')
        with self.assertRaisesRegex(t.WorkConflict, 'original repair attempt'):
            start(another, 'duplicate-writer')
        self.assertEqual(len(t.read_task(self.f.root, 'task')['runs']), 1)

    def test_explicit_finding_verification_records_the_actual_observer(self):
        req = self.delegate(); note = self.assign('Native execution repaired.')
        self.cli(self.worker,'work-return','--request',req['id'],'--result',note)
        self.cli(self.worker,'work-maintenance-note','--request',self.parent['id'],
            '--note',note,'--status','verified')
        self.assertEqual(len(maintenance.queue(self.f.root)['findings']),0)
        with t._connect(self.f.root, True) as c:
            self.assertEqual(maintenance.state(c, self.parent['id'])['actor'], self.worker)

    def test_owner_can_end_unstarted_repair_without_a_return_envelope(self):
        req = self.delegate(); note = self.assign('Withdraw this unused repair assignment.')
        self.cli(self.maintainer, 'work-end', '--request', req['id'], '--note', note)
        with t._connect(self.f.root, True) as c:
            with self.assertRaisesRegex(ValueError, 'assignment was ended'):
                t._admit_request(c, t._row(c, 'work_tasks', 'task'),
                    t._row(c, 'work_requests', req['id']), side_effect=True)
        next_req = self.delegate(self.assign('Continue the repair under this new brief.'))
        self.assertNotEqual(req['id'], next_req['id'])
        self.assertEqual(t.read_task(self.f.root, 'task')['runs'], [])

    def test_reassignment_checks_original_execution_facts(self):
        req = self.delegate()
        f = self.f
        f.call(f.main, 'grant-role', {'id':'another-maintainer', 'actor':f.other,
            'org_role':'maintainer', 'evidence_ref':f.evidence})
        note = self.assign('The user transfers this finding to the new maintainer.')
        self.cli(f.other, 'work-claim-finding', '--request', self.parent['id'], '--authorization', note)
        self.assertEqual(t.read_request(f.root, req['id'])['recipient_actor'], self.worker)
        self.cli(self.worker, 'work-return', '--request', req['id'], '--result', self.assign('Completed original repair.'))
        self.cli(f.other, 'work-claim-finding', '--request', self.parent['id'], '--authorization', note)
        self.assertEqual(t.read_request(f.root, self.parent['id'])['recipient_actor'], f.other)

    def test_cancelled_business_is_not_started_by_delegation(self):
        f = self.f
        f.call(f.lead,'cancel',{'id':'cancel','task_id':'task','expected_task_revision':1,
            'evidence_ref':f.evidence,'summary':'User cancelled original work.'})
        req = self.cli(self.maintainer,'work-maintenance-delegate','--request',self.parent['id'],'--brief',self.assign())
        with t._connect(f.root, True) as c:
            with self.assertRaisesRegex(t.WorkConflict, 'cancelled'):
                t._admit_request(c, t._row(c, 'work_tasks', 'task'), t._row(c, 'work_requests', req['id']), side_effect=True)
        self.assertEqual(t.read_task(f.root,'task')['runs'],[])

if __name__ == '__main__':
    unittest.main()
