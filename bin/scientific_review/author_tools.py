"""Author-requested review in the author's live Session.

The existing request/run/receipt records remain the durable source. Threads
only transport concurrent work; they are not a second workflow database.
"""
import json
import contextlib
import math
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import threading
import time
import agent_base as runtime
from codex_host import safe_diagnostic


class ReviewService:
    def __init__(self, worker, request, config, run_id, home, guard):
        self.worker, self.root = worker, worker.root
        self.request, self.config, self.run_id = request, config, run_id
        self.home, self.guard = home, guard
        self.jobs, self.collected, self.submissions = {}, {}, {}
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise runtime.StopRequested('Original author invocation stopped.', self.run_id)
        self.guard()

    def availability(self):
        import contextlib,harness_tasks as tasks
        from scientific_review.delivery import projection
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            return projection(c,self.root,tasks.read_request(self.root,self.request['id']))

    def record_delivery(self,handle,opinion):
        import harness_tasks as tasks
        return tasks.mutate(self.root,'run-review-delivered',self.worker.actor,{'id':self.run_id,
            'review_id':handle,'return_id':opinion['return_id'],'opinion_ref':opinion['ref']})

    def request_review(self, args):
        import harness_tasks as tasks
        import production_review as review
        import run_worker as driver
        from pathlib import Path
        self.check()
        question = args.get('question')
        paths = args.get('artifacts')
        if not isinstance(question, str) or not question.strip() or not isinstance(paths, list) or not paths:
            raise ValueError('Provide the saved candidates and the question for the reviewer.')
        run = tasks.read_run(self.root, self.run_id)
        request = tasks.read_request(self.root, self.request['id'])
        scope = request['required_scope']
        artifacts = []
        for value in dict.fromkeys(paths):
            path = review.scientific_path(self.root, value)
            ref = {'id': str(path.relative_to(self.root)), 'path': str(path),
                   'sha256': runtime.digest(path.read_bytes())}
            artifacts.append(ref)
        # The reviewer reads the full brief and selected originals, without extraction.
        evidence = [request['input_ref'], *[{'path': a['path'], 'sha256': a['sha256']} for a in artifacts]]
        packet = {'author_run_id': self.run_id,
                  'authors': [run['native_identity']], 'review_scope': question, 'scope_keys': sorted(scope),
                  'artifact_refs': sorted(artifacts, key=lambda a: a['id']), 'evidence_refs': evidence}
        if 'prior_review_id' in args:
            from scientific_review.delta import baseline
            packet['prior_review'] = baseline(self.root,request,run['native_identity'],packet,args['prior_review_id'])
        old=None
        if 'supersedes_review_id' in args:
            from scientific_review.queue import author_review, subjects
            old,previous=author_review(self.root,request,run['native_identity'],args['supersedes_review_id'])
            selected=subjects(self.root,packet)
            if not selected or selected!=subjects(self.root,previous):
                raise ValueError('The queued replacement must cover the same scientific objects.')
            if driver._review_subject(request['id'],packet)==driver._review_subject(request['id'],previous):
                return {'review_id':old['id'],'state':'identical_original','replay_allowed':False}
            packet['supersedes_review_id']=old['id']
        parent=request
        try:
            request = driver._program_review_request(self.root, self.worker, parent, packet)
        except tasks.WorkConflict as exc:
            if exc.code != 'review_round_budget':
                raise
            return {'state':exc.code,'review_request':exc.review_request,'objects':sorted(exc.review_units),
                    'message':str(exc),'original_author_request':parent['id']}
        handle = request['id']
        packet = driver._review_packet(self.root, request['input_ref'])
        self.submissions[handle] = packet
        supersession=None
        if old:
            from scientific_review.queue import replace
            supersession=replace(self.root,parent,run['native_identity'],request,old,self.worker.actor)
            if supersession['state']!='replaced_before_start':
                return {'review_id':handle,'state':'replacement_withheld','supersession':supersession,'replay_allowed':False}
        from execution_lease import FileLock, atomic_json, process_identity
        from agent_base import child_environment
        from dsh_capacity import DSH, DEVIN, _lock
        from scientific_review.queue import replaced
        directory = Path(self.home) / 'reviews' / handle
        directory.mkdir(parents=True, exist_ok=True)
        launch = directory / 'launch.json'
        author_host = DEVIN if self.config.get('_devin_capacity') else DSH
        with FileLock(self.root, ['review-start', handle]), _lock(self.root, self.check, host=author_host):
            if withdrawn:=replaced(self.root,request):
                return {'review_id':handle,'state':'superseded_before_start','supersession':withdrawn,'replay_allowed':False}
            existing = driver._selected_run(self.root, handle)
            # A retry from the original author can recover a replacement
            # between durable registration and transport startup. The job's
            # exact review-start lease still admits at most one native intent.
            if existing is None and (not launch.exists() or old and self._unowned_launch(launch)):
                bound = json.loads(launch.read_bytes())['config_ref'] if launch.exists() else self.config['config_ref']
                tasks.evidence_ref(self.root,bound)
                intent = {'review_request': handle, 'parent_request': self.request['id'],
                          'config_ref': bound, 'phase': 'launch_intent',
                          'manager': process_identity(os.getpid())}
                atomic_json(launch, intent)
                with (directory / 'stdout.log').open('ab') as stdout, (directory / 'stderr.log').open('ab') as stderr:
                    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / 'review_job.py'),
                        '--root', str(self.root), '--parent-request', self.request['id'],
                        '--review-request', handle, '--config', bound['path']],
                        cwd=self.root, env=child_environment(), stdout=stdout, stderr=stderr,
                        start_new_session=True, close_fds=True)
                atomic_json(launch, {**intent, 'phase': 'spawned', 'child': process_identity(child.pid)})
                threading.Thread(target=child.wait, name='review-reaper', daemon=True).start()
        existing = driver._selected_run(self.root, handle)
        details={'supersession':supersession} if supersession else {}
        if existing:
            return {'review_id': handle, 'state': existing['outcome'], 'run_id': existing['id'],
                    'receipt': existing['latest_receipt'], 'replay_allowed': False,**details}
        return {'review_id': handle, 'state': 'requested',
                'message': 'The independent review is registered. Continue independent work; review_result reports its queue, execution and original opinion in this Session.',**details}

    @staticmethod
    def _unowned_launch(path):
        from execution_lease import matches
        value=json.loads(path.read_bytes())
        owner=value.get('child') if value['phase']=='spawned' else value.get('manager')
        return bool(owner and matches(owner) is False)

    @staticmethod
    def _exited_launcher(request):
        """The exact registered packet identifies the detached child and its logs."""
        from execution_lease import matches
        packet = Path((request.get('input_ref') or {}).get('path', ''))
        if packet.name != 'packet.json' or packet.parent.name != request['id']:
            return None
        launch = packet.with_name('launch.json')
        try:
            value = json.loads(launch.read_bytes())
        except (OSError, ValueError):
            return None
        child = value.get('child') if isinstance(value, dict) else None
        if (not isinstance(child, dict) or type(child.get('pid')) is not int or not 0 < child['pid'] < 2**31
                or value.get('phase') != 'spawned' or value.get('review_request') != request['id']
                or value.get('parent_request') != request['parent_request_id'] or matches(child) is not False):
            return None
        return {'state':'launcher_exited_before_run', 'launch_path':str(launch),
                'stderr_path':str(packet.with_name('stderr.log')), 'replay_allowed':False}

    def result(self, args, *, cancelled=lambda: False):
        import harness_tasks as tasks
        import run_worker as driver
        handle = args.get('review_id')
        request = tasks.read_request(self.root, handle)
        if not request['independent']:
            raise ValueError('Select an independent review request.')
        if request.get('task_id')!=self.request.get('task_id'):
            raise ValueError('Read an original independent review from this author task; cross-task delivery requires its own explicit handoff.')
        delay = args.get('wait_seconds', 0)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 600:
            raise ValueError('wait_seconds must be finite and between 0 and 600; default is 0.')
        yielded=False
        with contextlib.ExitStack() as pending:
            def on_pending():
                nonlocal yielded
                capacity=self.config.get('_devin_capacity') or self.config.get('_dsh_capacity')
                if capacity and not yielded:
                    pending.enter_context(capacity.review_wait(self.check,handle));yielded=True
            value=self._wait_result(handle,delay,cancelled,on_pending)
        # A queued opinion may finish while this invocation fairly reacquires
        # its model seat. Refresh that fact once, without another model prompt.
        if yielded and value.get('state') not in ('received','wait_cancelled','failed','stopped','unknown'):
            value=self._wait_result(handle,0,cancelled,lambda:None)
        if value.get('state')=='received':
            packet={k:v for k,v in value.items() if k not in ('review_id','state')}
            try:
                delivery=({'status':'recorded','fact':self.record_delivery(handle,packet)} if packet.get('return_id') else
                    {'status':'index_pending','reason':'Original native final has not been indexed yet',
                     'recovery':'Read the same review_id again; full original opinion retained, no duplicate review.'})
            except (OSError,sqlite3.Error) as exc:
                delivery={'status':'index_pending','reason':type(exc).__name__,
                    'recovery':'Read the same original review_id again; full opinion retained, no duplicate review.'}
            self.collected[handle]=packet
            self.submissions[handle]=packet['opinion']['submission']
            value['delivery_recording']=delivery
        return value

    def _wait_result(self,handle,delay,cancelled,on_pending):
        import harness_tasks as tasks
        import run_worker as driver
        from execution_lease import tool_activity
        request=tasks.read_request(self.root,handle)
        from scientific_review.queue import _links, withheld
        until = time.monotonic() + delay
        while True:
            self.check()
            with contextlib.closing(tasks._connect(self.root,True)) as c:
                if withdrawal:=_links(c,request['task_id'],'superseded_review_id',handle):
                    return {'review_id':handle,'state':'superseded_before_start','supersession':withdrawal,'replay_allowed':False}
                if pending:=withheld(c,request):
                    return {'review_id':handle,'state':'replacement_withheld','supersession':pending,'replay_allowed':False}
            if cancelled():
                return {'review_id': handle, 'state': 'wait_cancelled', 'replay_allowed': False}
            run = driver._selected_run(self.root, handle)
            if run:
                # A live dictionary/thread is never the durable review authority.
                observation = driver._program_read(self.root, self.worker, request, run)
                run = tasks.read_run(self.root, run['id'])
                if (isinstance(observation, dict) and observation.get('outcome') == run['outcome']
                        and observation.get('execution_state') in ('queued', 'launch_unresolved', 'abandoned_before_start')):
                    on_pending()
                    if time.monotonic() >= until:
                        return {'review_id': handle, 'state': observation['execution_state'], **observation,
                                'run_updated_at': run.get('updated_at'), 'last_tool_activity': None}
                    self.cancelled.wait(min(0.2, max(0, until - time.monotonic())))
                    continue
            if run and run['outcome'] == 'succeeded':
                opinion = driver._program_opinion(self.root, self.worker, self.request, run)
                if opinion:
                    return {'review_id':handle,'state':'received',**opinion}
            if run and run['outcome'] in ('unknown', 'failed', 'stopped'):
                pending = observation.get('automatic_continuation') if isinstance(observation, dict) else None
                if pending and pending.get('state') in ('checking_original', 'claimed', 'cooldown'):
                    on_pending()
                    if time.monotonic() >= until:
                        return {'review_id':handle, 'state':'recovering_original', 'run_id':run['id'],
                                'automatic_continuation':pending, 'receipt':run.get('latest_receipt'),
                                'replay_allowed':False}
                    self.cancelled.wait(min(0.2, max(0, until - time.monotonic())))
                    continue
                value = {'review_id': handle, 'state': run['outcome'], 'run_id': run['id'],
                         'receipt': run.get('latest_receipt'), 'run_updated_at': run.get('updated_at'),
                         'last_tool_activity': tool_activity(self.root, run), 'replay_allowed': False}
                if pending:
                    value['automatic_continuation'] = pending
                original = observation.get('result') if isinstance(observation, dict) else None
                if value['receipt'] and (not isinstance(original, dict) or observation.get('receipt') != value['receipt']):
                    try:
                        original = driver._program_result(self.root, run)
                    except (OSError, ValueError, RuntimeError):
                        original = None
                if isinstance(original, dict):
                    if diagnostic := original.get('safe_diagnostic'):
                        value['safe_diagnostic'] = diagnostic
                    if run.get('target_host') == 'deepseek_harness':
                        from dsh_headless import retained_transport_diagnostic
                        if transport := retained_transport_diagnostic(original, run.get('native_identity')):
                            value['transport_diagnostic'] = transport
                return value
            if not run:
                if direct_id := driver._direct_return_id(self.root, handle):
                    from scientific_review.rounds import opinion_view
                    with contextlib.closing(tasks._connect(self.root, True)) as c:
                        opinion = opinion_view(c, self.root, request, return_id=direct_id)
                    if opinion is not None:
                        return {'review_id':handle, 'state':'received', **opinion}
                    return {'review_id':handle, 'state':'original_return_unavailable',
                            'return_id':direct_id, 'replay_allowed':False}
                if exited := self._exited_launcher(request):
                    if driver._selected_run(self.root, handle):
                        continue  # An intent may have arrived since this loop first read the ledger.
                    return {'review_id':handle, **exited}
                on_pending()
                if time.monotonic() < until:
                    self.cancelled.wait(min(0.2, max(0, until - time.monotonic())))
                    continue
                return {'review_id': handle, 'state': 'not_started_or_launch_unresolved', 'replay_allowed': False,
                        'detail_command': 'python3 bin/context.py --request ' + handle}
            on_pending()
            if time.monotonic() >= until:
                return {'review_id': handle, 'state': run['outcome'], 'run_id': run['id'],
                        'run_updated_at': run.get('updated_at'), 'last_tool_activity': tool_activity(self.root, run),
                        'receipt': run.get('latest_receipt'),
                        'detail_command': 'python3 bin/agent_runtime.py readresult --run-id ' + run['id']}
            self.cancelled.wait(min(0.2, max(0, until - time.monotonic())))

    def report_finding(self, args):
        from task_ledger import maintenance as harness_maintenance
        from production_review import scientific_path
        self.check()
        report = scientific_path(self.root, args.get('report'))
        finding = harness_maintenance.report(self.root, self.worker.actor, self.request['task_id'],
                                            report, native_run_id=self.run_id, finding_id=args.get('finding_id'))
        return {'finding_id': finding['id'], 'state': 'reported'}

    def wait(self):
        return self.finish()

    def finish(self):
        # These facts describe what the author requested and actually read.
        # A later reviewer return is recorded on its own run, not as collected.
        artifacts = {}
        for packet in self.submissions.values():
            for artifact in packet.get('artifact_refs', []):
                artifacts[artifact['id']] = artifact
        try:reviews=self.availability();availability_issue=None
        except (OSError,ValueError,sqlite3.Error) as exc:
            reviews=[];availability_issue={'status':'unavailable','reason':type(exc).__name__,
                'recovery':'Read the original request/review again; native result and collected opinions retained.'}
        return {'requested_review_ids': [r['review_id'] for r in reviews], 'reviewed_artifacts': list(artifacts.values()),
                'review_availability':reviews,
                **({'review_availability_issue':availability_issue,'requested_review_ids':list(self.submissions)} if availability_issue else {}),
                'collected_review_return_ids': [x['return_id'] for x in self.collected.values() if x['return_id']]}

    def close(self):
        return self.finish()
