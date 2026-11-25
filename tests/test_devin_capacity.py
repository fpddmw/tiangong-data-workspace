"""Isolated real dispatch, review job and role contention over a synthetic ACP peer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import dsh_capacity as capacity
import harness_tasks as tasks
from python_executable_fixture import python_entrypoint
import test_devin_runtime as devin_fixture
import test_dsh_capacity as pool_fixture


class DevinCapacityTests(unittest.TestCase):
    setUp = pool_fixture.CapacityTests.setUp
    ref = pool_fixture.CapacityTests.ref
    create_request = pool_fixture.CapacityTests.create_request

    stop_test_processes = pool_fixture.CapacityTests.stop_test_processes
    start = pool_fixture.CapacityTests.start
    seed = pool_fixture.CapacityTests.seed
    until = pool_fixture.CapacityTests.until
    release = pool_fixture.CapacityTests.release

    def view(self):
        return capacity.snapshot(self.root, capacity.DEVIN)

    def test_dead_unadmitted_manager_frees_capacity_without_replay(self):
        holders = [self.start(host=capacity.DEVIN) for _ in range(6)]
        rid, waiter, _ = self.start(host=capacity.DEVIN, ready=False)
        self.until(lambda: any(e['run_id'] == rid for e in self.view()['waiting']), 'original queued ticket')
        waiter.kill()
        waiter.wait(timeout=5)
        self.managers.remove(waiter)
        self.release(*holders[0][:2])
        replacement = self.start(host=capacity.DEVIN)
        observed = self.view()
        self.assertEqual(observed['counts']['occupied']['author'], 6)
        self.assertFalse(any(e['run_id'] == rid for e in observed['active'] + observed['waiting']))
        self.assertFalse((self.root/(rid+'.native-ready')).exists())
        self.assertEqual(tasks.read_run(self.root, rid)['outcome'], 'intent')
        self.release(*replacement[:2])

    def test_public_author_review_reentry_and_same_session_resume(self):
        devin_fixture.DevinRuntimeTests.select_devin(self)
        config = json.loads(self.config_path.read_bytes())
        executable = self.home / 'fixture-acp'
        python_entrypoint(executable, Path(__file__).with_name('devin_capacity_acp_fixture.py'))
        config['hosts']['devin']['binary'] = str(executable)
        config['roles']['executor']['model'] = 'swe-2-max'
        config['roles']['scientific_reviewer'].update(host='devin', provider='devin', model='swe-2-max')
        self.config_path.write_text(json.dumps(config))
        holders = {kind: [self.start(kind, host=capacity.DEVIN) for _ in range(6)]
                   for kind in ('author', 'review')}
        dsh_holder = self.start(host=capacity.DSH)
        base = [sys.executable, str(pool_fixture.REPO/'bin/harness.py'), '--root', str(self.root)]
        cmd = [*base, 'work-dispatch', '--request', 'request', '--config', str(self.config_path), '--execute']
        with (self.root/'author.output').open('w') as output, (self.root/'author.stderr').open('w') as error:
            author = subprocess.Popen(cmd, stdout=output, stderr=error, env=os.environ.copy())
        self.managers.append(author)
        def queued(kind):
            return next((e for e in self.view()['waiting'] if e['kind'] == kind), None)
        self.until(lambda: queued('author'), 'seventh Devin author queued')
        read = json.loads(subprocess.check_output([*base, 'work-capacity', '--host', 'devin']))
        self.assertEqual(read['counts']['occupied'], {'author':6, 'review':6, 'total':12})
        self.assertEqual(capacity.snapshot(self.root)['counts']['occupied']['total'], 1)
        self.release(*holders['author'][0][:2])
        self.until(lambda: queued('review') and self.view()['waiting_review'], 'review queue and yielded author')
        waiting = self.view()['waiting_review'][0]
        self.assertTrue(waiting['execution_protected'])
        self.assertFalse(waiting['seat_held'])
        other = self.start(host=capacity.DEVIN)
        self.release(*holders['review'][0][:2])
        self.until(lambda: queued('author') and queued('author')['run_id'] == waiting['run_id'],
                   'same author queues for reentry')
        self.assertFalse((self.home/'received').exists())
        self.release(*other[:2])
        author.wait(timeout=20)
        self.assertEqual(author.returncode, 0, (self.root/'author.stderr').read_text())
        original = json.loads((self.root/'author.output').read_bytes())
        self.assertEqual(original['native_identity']['id'], 'fixture-author')
        review_id = (self.home/'received').read_text()
        from run_worker import _selected_run
        reviewed = _selected_run(self.root, review_id)
        self.assertEqual(reviewed['native_identity']['id'], 'fixture-reviewer')
        note = self.home/'continue.md'
        note.write_text('Continue the original offline author Session.')
        resumed = json.loads(subprocess.check_output([*cmd,'--run-id',original['run_id'],'--instructions',str(note)], timeout=20))
        self.assertEqual(resumed['native_identity']['id'], 'fixture-author')
        for run, kind, expected in ((original,'author',['queued','admitted','waiting_review','queued','admitted','released']),
                    ({'run_id':reviewed['id']},'review',['queued','admitted','released']),
                    (resumed,'author',['queued','admitted','released'])):
            events = [e['payload'] for e in tasks.read_run(self.root,run['run_id'])['events'] if e['kind']=='capacity']
            self.assertEqual([e['phase'] for e in events], expected)
            self.assertTrue(all(e['kind']==kind and Path(e['queue_source']).name=='devin-capacity.json' for e in events))
        self.release(*dsh_holder[:2])
