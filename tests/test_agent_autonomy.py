"""Collaboration behavior through isolated original-task stores; no model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))

import agent_runtime as runtime
import dsh_headless
import harness_package
import harness_tasks as tasks
import production_review
import run_worker
import test_dsh_headless as dsh_fixtures
import test_harness_tasks as task_fixtures
import os
import local_check
import threading
import time
import subprocess
from types import SimpleNamespace
from scientific_review.author_tools import ReviewService


class AgentAutonomyTests(unittest.TestCase):
    def test_plain_report_and_malformed_optional_metadata_are_received_without_reformatting(self):
        f = task_fixtures.TaskCoreTests('runTest')
        f.setUp()
        self.addCleanup(f.tearDown)
        request = f.request()
        body = {'actual_findings': 'The source supports a limited local result.',
                'coverage': 'Not a management scope list', 'artifacts': 'Paths remain in the original report.'}
        original = f.save('original-unformatted-return', body)
        with patch.dict(os.environ, CODEX_THREAD_ID=f.worker['id']):
            returned = run_worker.return_result(f.root, f.worker, original['path'], request_id=request['id'])
        self.assertEqual(returned['result_ref'], original)
        self.assertEqual(returned['author_actor'], f.worker)
        self.assertEqual(returned['coverage'], [])
        self.assertEqual(json.loads(Path(original['path']).read_text()), body)
        self.assertEqual(tasks.read_task(f.root, 'task')['dispositions'], [])

    def test_resumed_author_collects_original_review_candidate_without_new_review(self):
        f = task_fixtures.TaskCoreTests('runTest')
        f.setUp()
        self.addCleanup(f.tearDown)
        artifact={'id':'candidate', **f.save('frozen-candidate', {'amount': '1'})}
        packet={'artifact_refs':[artifact]}
        opinion={'return_id':'original-review-return','opinion':{'submission':packet,'text':'Original opinion'}}
        worker=SimpleNamespace(root=f.root)
        service=ReviewService(worker,{'id':'parent','task_id':'task'},{},'resumed-author',f.root,lambda:None)
        completed={'id':'review-run','outcome':'succeeded'}
        with patch.object(tasks,'read_request',return_value={'id':'historical-review','parent_request_id':'parent',
                          'task_id':'task','independent':True}), \
             patch.object(run_worker,'_selected_run',return_value=completed), \
             patch.object(tasks,'read_run',return_value=completed), \
             patch.object(run_worker,'_program_read',return_value={'outcome':'succeeded'}), \
             patch.object(run_worker,'_program_opinion',return_value=opinion), \
             patch.object(service,'record_delivery',return_value={'return_id':'original-review-return'}) as delivered, \
             patch.object(service,'availability',return_value=[{'review_id':'historical-review','state':'ready'}]), \
             patch.object(run_worker,'_program_turn') as launch:
            received=service.result({'review_id':'historical-review'})
            finished=service.finish()
        self.assertEqual(received['state'],'received')
        self.assertEqual(finished['reviewed_artifacts'],[artifact])
        self.assertEqual(finished['collected_review_return_ids'],['original-review-return'])
        delivered.assert_called_once_with('historical-review',opinion)
        self.assertIs(service.submissions['historical-review'],packet)
        launch.assert_not_called()

    def test_plain_report_entry_preserves_json_scalar_and_array_text(self):
        f=task_fixtures.TaskCoreTests('runTest');f.setUp();self.addCleanup(f.tearDown)
        request=f.request()
        for index,text in enumerate(('42','"检查完成"','["first finding", "second finding"]')):
            path=f.root/f'report-{index}.txt';path.write_text(text)
            with patch.dict(os.environ,CODEX_THREAD_ID=f.worker['id']):
                returned=run_worker.return_result(f.root,f.worker,str(path),request_id=request['id'])
            self.assertEqual(returned['summary'],text)
            self.assertEqual(returned['coverage'],[])


    def test_author_close_is_bounded_and_retains_independent_review_until_its_own_finish(self):
        finished = threading.Event()
        adapter = Mock()
        worker = SimpleNamespace(root=Path.cwd(), actor={'id': 'author'},
                                 live_lock=threading.RLock(), live_adapters={'review-run': adapter})
        service = ReviewService(worker, {'id': 'parent'}, {}, 'author-run', Path.cwd(), lambda: None)
        thread = threading.Thread(target=lambda: finished.wait(2))
        thread.start()
        service.jobs['review-request'] = {'thread': thread}
        closer = threading.Thread(target=service.close)
        with patch.object(tasks, 'mutate') as mutation, patch.object(service,'availability',return_value=[]):
            closer.start()
            closer.join(1)
            self.assertFalse(closer.is_alive(),'Author close must not wait for the independent reviewer')
            self.assertTrue(thread.is_alive(),'The independent review retains its own execution')
            self.assertFalse(service.cancelled.is_set())
            finished.set()
            thread.join(1)
            mutation.assert_not_called()
        adapter.close.assert_not_called()
        self.assertFalse(thread.is_alive())

    def test_dsh_close_terminates_pipe_holding_descendants(self):
        adapter=object.__new__(dsh_headless.DeepSeekAdapter)
        adapter.process_lock=threading.RLock()
        adapter.control=None
        child="import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(30)"
        script="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1]]);time.sleep(30)"
        adapter.process=subprocess.Popen([sys.executable,'-c',script,child],stdout=subprocess.PIPE,
                                          text=True,start_new_session=True)
        self.addCleanup(adapter.close)
        self.assertEqual(adapter.process.stdout.readline().strip(),'ready')
        started=time.monotonic();adapter.close()
        self.assertEqual(adapter.process.stdout.read(),'')
        self.assertLess(time.monotonic()-started,5)
        adapter.process.stdout.close()


    @unittest.skipUnless(local_check.available(), 'Requires the macOS offline sandbox')
    def test_reviewer_can_compute_but_cannot_mutate_candidates_or_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();candidate=root/'candidate.json';candidate.write_text('{"quantity":8}')
            script="""import json, os, pathlib, socket
p=pathlib.Path('candidate.json'); result={'quantity':json.loads(p.read_text())['quantity']*2}
try: p.write_text('changed'); result['write']='allowed'
except PermissionError: result['write']='denied'
try: socket.create_connection(('127.0.0.1',9),timeout=1); result['network']='allowed'
except PermissionError: result['network']='denied'
pathlib.Path(os.environ['HTAM_CHECK_OUTPUT'],'check.json').write_text(json.dumps(result))
print(json.dumps(result))
"""
            observed=local_check.execute(root,root,[sys.executable,'-c',script],env={'PATH':os.environ['PATH']})
            self.assertEqual(observed['exit_code'],0,observed['stderr'])
            self.assertEqual(json.loads(observed['stdout']),{'quantity':16,'write':'denied','network':'denied'})
            self.assertEqual(candidate.read_text(),'{"quantity":8}')
            self.assertTrue((Path(observed['output_directory'])/'check.json').is_file())

    def author(self):
        case = dsh_fixtures.DeepSeekProgramTests('test_markdown_final_recovers_own_checked_delivery_from_original_native_turn')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case






    def test_scientific_filename_is_not_a_credential_classifier(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for name in ('measurement-session.json', 'measurement.session.json', '.evidence/measurement.json'):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('{"quantity":8}')
                self.assertEqual(production_review.scientific_path(root, name), file)
            for name in ('.secrets/editor.json', '.state/session-data.json', '.env'):
                with self.assertRaises(ValueError):
                    production_review.scientific_path(root, name, must_exist=False)



if __name__ == '__main__':
    unittest.main()
