"""Exact original bytes and native operation evidence; no remote writes/models."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import sqlite3

import delivery_evidence as delivery
import harness_scope as scope
import harness_tasks as tasks
import test_agent_runtime as fixture

REPO = Path(__file__).resolve().parents[1]


def candidate(ident='process-one', amount='1'):
    return {'processDataSet': {'processInformation': {'dataSetInformation': {'common:UUID': ident}},
        'administrativeInformation': {'publicationAndOwnership': {'common:dataSetVersion': '01.00.000'}},
        'exchanges': {'exchange': [{'@dataSetInternalID': '0', 'meanAmount': amount}]}}}


class DeliveryEvidenceTests(unittest.TestCase):
    ref = fixture.RuntimeTests.ref
    create_request = fixture.RuntimeTests.create_request
    setUp = fixture.RuntimeTests.setUp

    def save(self, name, value):
        path = self.home / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)
        return self.ref(path)

    def artifact(self, ref):
        return {'id': 'file:' + ref['sha256'], 'version': ref['sha256'], **ref}

    def returned(self, name, artifacts):
        report = self.save(name + '.md', name + ': original complete report, with its use conditions.')
        return tasks.mutate(self.root, 'return', self.actor, {'id': name, 'request_id': 'request',
            'result_ref': report, 'summary': 'Original report',
            'artifact_refs': [self.artifact(a) for a in artifacts]})

    def facts(self, body=None, documents=()):
        ref = self.save('candidate.json', body or candidate())
        doc_refs = [self.save('native-' + str(i) + '.json', d) for i, d in enumerate(documents)]
        returned = self.returned('delivery', [ref, *doc_refs])
        reader = scope.Evidence(self.root)
        with contextlib.closing(tasks._connect(self.root, True)) as c:
            pool = delivery.EvidenceSet(c, self.root, tasks.read_request(self.root, 'request'), returned, reader)
            return pool.native_facts(pool.artifacts[0]), reader

    def validation(self, body):
        return {'type': 'process', 'id': body['processDataSet']['processInformation']['dataSetInformation']['common:UUID'],
                'version': '01.00.000', 'status': 'passed', 'ok': True,
                'validator': 'Original native typed validator', 'payload_sha256': delivery.content_digest(body),
                'checks': ['schema', 'business'], 'notChecked': ['remote_references']}

    def receipt(self, operation, body):
        ident = delivery.body_identity(body)
        return {'schemaVersion': 1, 'operation': operation, 'status': 'completed',
                'outcome': 'dataset_read' if operation.endswith(' get') else 'dataset_written',
                'data': {'subject': dict(zip(('type','id','version'), ident))},
                'context': {'actor': {'id':'original-owner'}, 'environment':'original-project',
                    'requestId':'original-operation', 'observation':{'source':'remote'}},
                'completeness': {'returned':1,'truncated':False,'hasMore':False}, 'error':None}

    def test_canonical_body_equivalence_does_not_hide_quantity_or_version_change(self):
        first = candidate()
        reordered = json.loads(json.dumps(first,sort_keys=True))
        self.assertTrue(delivery.same_body(first,reordered))
        self.assertFalse(delivery.same_body(first,candidate(amount='2')))
        self.assertFalse(delivery.same_body(first,candidate(ident='other-object')))

    def test_result_group_needs_same_revision_members_and_independent_observation(self):
        model={'lifeCycleModelDataSet':{'lifeCycleModelInformation':{'dataSetInformation':{'common:UUID':'model'}},
                'administrativeInformation':{'publicationAndOwnership':{'common:dataSetVersion':'01.00.000'}}}}
        member=candidate()
        _,member_id,version=delivery.body_identity(member)
        target={'modelId':'model','version':version}
        binding={'expectedActor':'owner','expectedEnvironment':'https://fixture.invalid'}
        members=[{'id':member_id,'version':version,'type':'primary'}]
        snapshot={'revision':'fixture-revision','model':{'id':'model','version':version,'json_ordered':model},
                  'members':members,'processes':[{'id':member_id,'version':version,'model_id':'model',
                      'model_version':version,'json_ordered':member}]}
        saved={**target,**binding,'status':'verified','snapshot':snapshot,'receipt':{
            'operationId':'original-operation','savedAt':'2026-01-01T00:00:00Z',
            'revision':snapshot['revision'],'members':members}}
        different={**target,**binding,'status':'observed','snapshot':{**snapshot,'revision':'different'}}
        facts,_=self.facts(model,[saved,different])
        self.assertFalse(facts['saved'])
        self.assertTrue(facts['full_readback'])
        inspected={**different,'snapshot':snapshot}
        receipt={**saved,'status':'unknown','operationId':'original-operation'}
        facts,_=self.facts(model,[saved,inspected,receipt])
        self.assertEqual(len(facts['saved']),1)
        self.assertEqual(len(facts['full_readback']),1)
        self.assertFalse(facts['submitted'])
        self.assertFalse(facts['unknown_effects'])

    def test_direct_native_report_without_lca_gate_retains_actual_coverage(self):
        body = candidate()
        facts, reader = self.facts(body,[self.validation(body)])
        self.assertEqual(len(facts['structure']),1)
        self.assertEqual(facts['validation_coverage'][0]['not_checked'],['remote_references'])
        self.assertTrue(all(s['status']=='pass' for s in reader.sources.values()))

    def test_native_envelope_and_unknown_schema_do_not_invent_a_pass(self):
        body = candidate();doc=self.receipt('dataset process validate',body)
        doc.update(outcome='validation_passed',data={'validation':{'status':'passed','checks':['schema'],
            'local':self.validation(body)}})
        facts,_ = self.facts(body,[doc]);self.assertTrue(facts['structure'])
        doc['schemaVersion']=99;doc['data']['subject']=dict(zip(('type','id','version'),delivery.body_identity(body)))
        # A different return version is used; raw original evidence remains intact.
        ref=self.save('unknown-envelope.json',doc);r=self.returned('unknown-version',[self.ref(self.home/'candidate.json'),ref])
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),r,scope.Evidence(self.root))
            checked=pool.native_facts(pool.artifacts[0])
        self.assertTrue(checked['reading_limits'])

    def test_full_native_validation_output_survives_truncated_envelope_preview(self):
        body=candidate();full={'status':'passed','checks':['schema','business','references'],
            'local':self.validation(body),'notChecked':['scientific_reference_suitability'],
            'references':{'counts':{'checked':13,'references':13,'blockers':0}}}
        facts,_=self.facts(body,[full]);self.assertTrue(facts['structure'])
        self.assertEqual(facts['validation_coverage'][0]['checks'],['schema','business','references'])

    def test_only_explicit_same_hash_copy_recovers_a_moved_original(self):
        original=self.save('original-body.json',candidate())
        moved=self.save('moved-body.json',candidate())
        r=self.returned('selected-original',[original]);Path(original['path']).unlink()
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            missing=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),r,scope.Evidence(self.root))
            self.assertTrue(missing.selection_missing)
            recovered=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),r,scope.Evidence(self.root),extra_refs=[moved])
            self.assertFalse(recovered.selection_missing);self.assertEqual(len(recovered.artifacts),1)

    def test_wrong_content_same_identity_has_no_old_validation_support(self):
        old=candidate(amount='1');current=candidate(amount='2')
        facts,_=self.facts(current,[self.validation(old)])
        self.assertEqual(facts['structure'],[])

    def test_empty_coverage_artifact_association_is_independent_and_omission_preserves_it(self):
        returned=self.returned('original',[])
        body=self.save('selected.json',candidate());artifact=self.artifact(body)
        payload={'id':'association','request_id':'request','result_ref':returned['result_ref'],
                 'summary':returned['summary'],'artifact_refs':[artifact]}
        tasks.mutate(self.root,'return',self.actor,payload)
        tasks.mutate(self.root,'return',self.actor,{k:v for k,v in payload.items() if k!='artifact_refs'})
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            current=next(r for r in scope._returns(c,'task') if r['id']=='original')
        self.assertEqual(current['artifact_refs'],[artifact]);self.assertEqual(current['coverage'],[])

    def test_save_only_turn_inherits_exact_previous_selection(self):
        body=self.save('selected.json',candidate());self.returned('first',[body])
        current=self.returned('saved-only',[])
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),current,scope.Evidence(self.root))
            selected=delivery.return_artifacts(c,self.root,tasks.read_request(self.root,'request'),{'result':'Only saved now.'})
        self.assertEqual([a['sha256'] for a in pool.artifacts],[body['sha256']])
        self.assertEqual(selected,pool.artifacts)

    def test_native_write_requires_original_request_account_environment_and_content(self):
        body=candidate();doc=self.receipt('dataset process create',body)
        doc['data']['contentDigest']=delivery.content_digest(body,ordered=True)
        facts,_=self.facts(body,[doc]);self.assertTrue(facts['saved'])
        wrong=self.receipt('dataset process create',body);wrong['data']['contentDigest']='0'*64
        ref=self.save('wrong-write.json',wrong);r=self.returned('wrong-write',[self.ref(self.home/'candidate.json'),ref])
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),r,scope.Evidence(self.root))
            saved=pool.native_facts(pool.artifacts[0])['saved']
            self.assertTrue(saved,'A later unrelated bad receipt must not erase the original successful write')
            self.assertTrue(all(r['sha256']!=ref['sha256'] for r in saved))

    def test_native_dry_run_is_never_a_saved_draft(self):
        body=candidate();doc=self.receipt('dataset process create',body)
        doc['data']['contentDigest']=delivery.content_digest(body);doc['outcome']='preview_completed'
        facts,_=self.facts(body,[doc]);self.assertEqual(facts['saved'],[])

    def test_local_output_failure_does_not_erase_an_explicit_native_written_effect(self):
        body=candidate();doc=self.receipt('dataset process save',body)
        doc.update(status='partial',warnings=[{'code':'OUTPUT_UNAVAILABLE'}])
        doc['data']['contentDigest']=delivery.content_digest(body)
        facts,_=self.facts(body,[doc]);self.assertTrue(facts['saved'])

    def test_native_get_output_artifact_without_complete_content_flag(self):
        body=candidate();output=self.save('fresh-canonical-output.json',body)
        doc=self.receipt('dataset process get',body)
        doc['artifacts']=[{'path':output['path'],'purpose':'complete_result','bytes':Path(output['path']).stat().st_size}]
        facts,_=self.facts(body,[doc]);self.assertTrue(facts['full_readback'])

    def test_partial_output_failure_never_reads_old_output_but_keeps_fresh_stdout(self):
        body=candidate();old=self.save('old-output.json',candidate(amount='999'))
        doc=self.receipt('dataset process get',body);doc['status']='partial'
        doc['warnings']=[{'code':'OUTPUT_UNAVAILABLE','message':'Output already exists'}]
        doc['artifacts']=[{'path':old['path'],'purpose':'complete_result'}]
        facts,_=self.facts(body,[doc]);self.assertEqual(facts['full_readback'],[])
        doc['data']['content']=body
        ref=self.save('fresh-stdout.json',doc);r=self.returned('stdout-body',[self.ref(self.home/'candidate.json'),ref])
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),r,scope.Evidence(self.root))
            self.assertTrue(pool.native_facts(pool.artifacts[0])['full_readback'])
        self.assertEqual(json.loads(Path(old['path']).read_bytes()),candidate(amount='999'))

    def test_public_file_association_is_idempotent_and_freezes_original(self):
        result=self.save('report.txt','Plain report; no invented field statuses.')
        artifact=self.save('working-body.json',candidate())
        argv=[sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),'work-return',
              '--request','request','--result',result['path'],'--artifact',artifact['path']]
        def invoke():
            cp=subprocess.run(argv,capture_output=True,text=True,env=os.environ.copy(),timeout=10)
            self.assertEqual(cp.returncode,0,cp.stderr);return json.loads(cp.stdout)
        first=invoke();second=invoke();self.assertEqual(first['id'],second['id'])
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            r=scope._returns(c,'task')[-1]
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),r,scope.Evidence(self.root))
            self.assertEqual(delivery.body_identity(pool.peek(pool.artifacts[0])),delivery.body_identity(candidate()))
            archived=pool.artifacts[0]['path']
        self.assertNotEqual(archived,artifact['path'])
        Path(artifact['path']).write_text(json.dumps(candidate(amount='2')))
        self.assertEqual(json.loads(Path(archived).read_bytes()),candidate())
        second_artifact=self.save('second-body.json',candidate(ident='process-two'))
        cp=subprocess.run([*argv[:-1],second_artifact['path']],capture_output=True,text=True,timeout=10)
        self.assertEqual(cp.returncode,0,cp.stderr)
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            current=scope._returns(c,'task')[-1]
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),current,scope.Evidence(self.root))
            self.assertEqual(len(pool.artifacts),2)

    def test_a07_live_named_stop_at_public_launcher(self):
        import os
        import shutil
        import harness_decisions as decisions

        materials = REPO / 'materials/packages/tiangong-package-gap-global-main-20261110'
        original = materials / ('apple-scm-reuse-20261110/apple/material-dispatch-20261115/'
                                'program-38e55011e83b6769')
        saved_home = original / ('runs/program:f9d4f2d30d5a99535b2c784e580d486e/delivery/'
                                 'operation-65c19f2e43c14a4cb790f64b8e53c2d4')
        unknown_home = original / ('runs/program:cdace02e7d7451affb15acfaac4be2dd/delivery/'
                                   'operation-be7d7f88676248f4a92f31fab102bf84')
        if not (saved_home/'stdout.bin').is_file() or not (unknown_home/'stdout.bin').is_file():
            self.skipTest('Requires the retained A07 source artifacts in the original workspace')
        original_receipt = json.loads((saved_home/'stdout.bin').read_bytes())
        unknown_receipt = json.loads((unknown_home/'stdout.bin').read_bytes())
        self.assertEqual(original_receipt['outcome'], 'dataset_written')
        self.assertEqual(unknown_receipt['status'], 'unknown')
        a07 = (saved_home/'input.json').read_bytes()
        value = json.loads(a07)
        self.assertEqual(value['id'], '682319ae-2cba-4b07-9e56-1ec02bf45697')
        source = self.home/'original-stop.md'
        shutil.copyfile(original/'work/AUTHORIZATION-VIOLATION-REPORT-20261118.md', source)
        stop_ref = {**self.ref(source), 'locator': 'file', 'decided_by': self.actor['id']}
        scope = {'task': 'task', 'package': 'request', 'object': 'process/'+value['id']+'/'+value['version'],
                 'action':'save', 'project':'isolated-project', 'account':value['expectedActor']}
        snapshot = {'scope': scope, 'subject': decisions.DATASET_WRITE, 'source_ref': stop_ref}
        shutil.copytree(REPO/'bin', self.root/'bin', ignore=shutil.ignore_patterns('__pycache__'))
        native = self.home/'isolated-cli.py'
        native.write_text("""import hashlib,json,sys
from pathlib import Path
log=Path(__file__).with_name('transport.jsonl')
args=sys.argv[1:]
if args[:3] == ['dataset','process','get']:
    print('{"outcome":"dataset_read"}')
else:
    assert args[args.index('--input')+1]=='-'
    raw=sys.stdin.buffer.read()
    item=json.loads(raw)
    with log.open('a') as stream:
        stream.write(json.dumps({'id':item['id'], 'sha256':hashlib.sha256(raw).hexdigest()})+'\\n')
    print('{"outcome":"dataset_written"}')
""")
        (self.root/'.control/runtime.json').write_text(json.dumps({'node':sys.executable, 'cli':str(native)}))
        (self.root/'.control/accounts.json').write_text(json.dumps({'project_ref':scope['project'],
            'editor':{'user_id':scope['account']}}))
        role_env = self.root/'.env'
        role_env.write_text('DATA_EDITOR_SESSION_FILE=isolated-no-credentials\nTIANGONG_LCA_API_BASE_URL='
                            +value['expectedEnvironment']+'\n')
        role_env.chmod(0o600)
        local_home = self.home/'isolated-home'
        journals = local_home/'.local/state/tiangong-lca/dataset-operations'
        journals.mkdir(parents=True)
        (journals/'unrelated-old.json').write_text('{not parseable')
        launch = self.save('admission-launch.json',{'run_id':'admission-run','request_id':'request',
            'native_cwd':str(self.root),'work_home':str(self.home)})
        tasks.mutate(self.root,'run-intent',self.actor,{'id':'admission-run','request_id':'request',
            'config_ref':self.ref(self.config_path),'input_refs':[launch,self.input_ref],
            'target_host':'codex','operation':'start'})
        environment = {**os.environ, 'HOME':str(local_home), 'HTAM_DELIVERY_ROOT':str(self.root),
            'HTAM_DELIVERY_RUN_ID':'admission-run', 'HTAM_DELIVERY_REQUEST_ID':'request',
            'HTAM_DELIVERY_RUN_HOME':str(self.home/'runs/admission-run')}
        pending = self.home/'source-input.json'
        log = self.home/'transport.jsonl'

        def public(*argv, raw=None, env=environment):
            return subprocess.run([sys.executable, str(self.root/'bin/launch.py'), '--role', 'editor',
                'cli', '--', *argv], input=raw, env=env, capture_output=True, timeout=15)

        def decide(document, env=environment):
            return subprocess.run([sys.executable, str(self.root/'bin/harness.py'), '--root', str(self.root),
                'work-decision-record', '--decision', '-'], input=json.dumps(document).encode(), env=env,
                capture_output=True, timeout=15)

        def save(raw, prefix=(), via_stdin=False):
            pending.write_bytes(raw)
            return public(*prefix,'dataset','process','save','--input','-' if via_stdin else str(pending),
                          '--json', raw=raw if via_stdin else None)

        original_id = value['requestId']
        stopped = {**snapshot,'id':'named-stop','statement':'stop'}
        unauthorized = decide(stopped, env={**environment,'CODEX_THREAD_ID':'other-author'})
        self.assertNotEqual(unauthorized.returncode, 0)
        registered = decide(stopped)
        self.assertEqual(registered.returncode, 0, registered.stderr)
        refused = save(a07, ('--json',))
        self.assertEqual(json.loads(refused.stderr)['status'],'stopped')
        self.assertEqual(json.loads(save(a07, ('--',), via_stdin=True).stderr)['status'],'stopped')
        self.assertFalse(log.exists())

        retry = json.dumps({**json.loads((unknown_home/'input.json').read_bytes()),
                            'requestId':'substituted-id'}).encode()
        self.assertEqual(json.loads(save(retry).stderr)['status'],'stopped')
        self.assertFalse(log.exists())
        self.assertEqual(json.loads(public('dataset','process','get','--id',value['id'],
            '--version',value['version'],'--json').stdout)['outcome'],'dataset_read')
        a05 = json.loads((original/'work/candidates/A05Li2CO3.json').read_bytes())
        kind, ident, version = delivery.body_identity(a05)
        self.assertEqual(kind, 'process')
        positive = json.dumps({**value,'id':ident,'version':version,'content':a05,'expectedContent':a05,
                               'requestId':'isolated-a05-control'}).encode()
        self.assertEqual(json.loads(save(positive).stdout)['outcome'],'dataset_written')
        self.assertEqual(json.loads(log.read_bytes())['sha256'], hashlib.sha256(positive).hexdigest())
        conflict = decide({**snapshot,'id':'conflicting-release','statement':'release'})
        self.assertEqual(conflict.returncode, 0, conflict.stderr)
        conflict_result = json.loads(save(a07).stderr)
        self.assertEqual(conflict_result['status'],'conflict')
        self.assertEqual(len(log.read_text().splitlines()),1)
        records = [json.loads(p.read_text()) for p in (self.home/'runs/admission-run/delivery').glob('*/record.json')]
        refused_records = [row for row in records if row.get('dispatch') == 'not_started']
        self.assertEqual(len(refused_records),4)
        if directory := os.environ.get('HTAM_1010_EVIDENCE'):
            proof = {'source_saved_record':str(saved_home/'record.json'), 'source_saved_request_id':original_id,
                'source_saved_outcome':original_receipt['outcome'],
                'source_unknown_record':str(unknown_home/'record.json'), 'source_unknown_outcome':unknown_receipt['outcome'],
                'recorded_stop':json.loads(registered.stdout), 'records':refused_records,
                'transport':json.loads(log.read_bytes()), 'unrelated_bad_journal_ignored':True,
                'read_result':'dataset_read', 'unauthorized_stop_rejected':True}
            (Path(directory)/'a07-launcher.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))

    def test_managed_input_freeze_failure_does_not_start_native_cli(self):
        import contextlib
        import errno
        import io
        import os

        ref=self.save('probe-write.json',{'content':candidate()})
        home=self.home/'runs/probe/delivery/operation-fixed'
        run={'id':'probe','request_id':'request'}
        with patch.dict(os.environ,{'HTAM_DELIVERY_ROOT':str(self.root),'HTAM_DELIVERY_RUN_ID':'probe'}), \
                patch.object(delivery,'_capture_home',return_value=(run,home)), \
                patch.object(delivery.tempfile,'TemporaryFile',side_effect=PermissionError(errno.EACCES,'fixture')), \
                patch.object(delivery.subprocess,'Popen') as launched, contextlib.redirect_stderr(io.StringIO()):
            code=delivery.execute_native(self.root,['not-executed'],
                ['dataset','process','save','--input',ref['path']],env={},cwd=self.root)
        self.assertEqual(code,1)
        launched.assert_not_called()
        record=json.loads((home/'record.json').read_bytes())
        self.assertEqual(record['dispatch'],'not_started')
        self.assertEqual(record['local_failure']['stage'],'input_freeze')
        self.assertEqual(record['local_failure']['system_code'],'EACCES')

    def capture(self,*,stdin=False,context=True,broken=False,old_output=False,index_failure=False,sandbox=False):
        launch=self.save('launch.json',{'run_id':'capture-run','request_id':'request',
            'native_cwd':str(self.root),'work_home':str(self.home)})
        tasks.mutate(self.root,'run-intent',self.actor,{'id':'capture-run','request_id':'request',
            'config_ref':self.ref(self.config_path),'input_refs':[launch,self.input_ref],
            'target_host':'codex','operation':'start'})
        native=self.home/'native.py'
        native.write_text('''import json,sys
from pathlib import Path
a=sys.argv; value=a[a.index('--input')+1]
body=json.loads(sys.stdin.buffer.read() if value=='-' else Path(value).read_bytes())
path=Path(a[a.index('--output')+1]); old=path.exists()
if not old:path.write_text(json.dumps(body))
doc={'schemaVersion':1,'operation':'dataset process validate','status':'partial' if old else 'completed',
 'outcome':'validation_passed','data':{'validation':{'status':'passed','local':{'ok':True}}},
 'warnings':[{'code':'OUTPUT_UNAVAILABLE'}] if old else [],'message':'x'*95000,'error':None}
print(json.dumps(doc));print('y'*45000,file=sys.stderr)
with (path.parent/'operation-count').open('a') as f:f.write('once\\n')
''')
        output=self.home/'canonical-output.json'
        if old_output:output.write_text(json.dumps(candidate(amount='999')))
        body=self.save('cli-input.json',candidate())
        argv=['dataset','process','validate','--input','-' if stdin else body['path'],'--output',str(output)]
        script='import delivery_evidence as d,os,sys;sys.exit(d.execute_native(sys.argv[1],sys.argv[2:4],sys.argv[4:],env=os.environ.copy(),cwd=sys.argv[1]))'
        environment={**os.environ,'PYTHONPATH':str(REPO/'bin')}
        if context:environment.update(HTAM_DELIVERY_ROOT=str(self.root),HTAM_DELIVERY_RUN_ID='absent-run' if broken else 'capture-run')
        command=[sys.executable,'-c',script,str(self.root),sys.executable,str(native),*argv]
        if sandbox:
            environment.update(HTAM_DELIVERY_REQUEST_ID='request',HTAM_DELIVERY_RUN_HOME=str(self.home/'runs/capture-run'))
            policy='(version 1) (allow default) (deny file-read* (subpath '+json.dumps(str(self.root/'.control'))+')) (deny file-write* (subpath '+json.dumps(str(self.root/'.control'))+'))'
            command=['/usr/bin/sandbox-exec','-p',policy,*command]
        result=subprocess.run(command,env=environment,
            input=json.dumps(candidate()).encode() if stdin else None,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr[-1000:])
        self.assertEqual((self.home/'operation-count').read_text(),'once\n')
        self.assertGreater(len(result.stdout),95000);self.assertGreater(len(result.stderr),45000)
        # The trusted host indexes this original run; the CLI sandbox itself
        # has no need for management database writes.
        if index_failure:
            with patch.object(delivery.tasks,'transaction',side_effect=sqlite3.OperationalError('database locked')):
                value=delivery.ingest_run(self.root,tasks.read_run(self.root,'capture-run'),self.actor)
                self.assertEqual(value['status'],'association_pending')
        else:delivery.ingest_run(self.root,tasks.read_run(self.root,'capture-run'),self.actor)
        run=tasks.read_run(self.root,'capture-run')
        records=[e['payload']['record_ref'] for e in run['events'] if e['kind']=='delivery-evidence']
        return result,records,output

    def test_collector_tees_complete_stdio_and_stable_input_without_hash_in_native_validation(self):
        result,records,output=self.capture(stdin=True)
        self.assertEqual(len(records),1)
        record=json.loads(Path(records[0]['path']).read_bytes())
        self.assertTrue(record['capture_complete']);self.assertTrue(record['input_unchanged'])
        self.assertEqual(Path(record['stdout_ref']['path']).read_bytes(),result.stdout)
        self.assertEqual(Path(record['stderr_ref']['path']).read_bytes(),result.stderr)
        self.assertEqual(json.loads(Path(record['output_ref']['path']).read_bytes()),candidate())
        returned=self.returned('native-only',[])
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),returned,scope.Evidence(self.root))
            self.assertTrue(pool.native_facts(pool.artifacts[0])['structure'])

    def test_collector_partial_output_keeps_old_disk_separate(self):
        result,records,output=self.capture(old_output=True)
        record=json.loads(Path(records[0]['path']).read_bytes())
        self.assertFalse(record['output_fresh']);self.assertNotIn('output_ref',record)
        self.assertEqual(json.loads(output.read_bytes()),candidate(amount='999'))
        self.assertEqual(json.loads(result.stdout)['status'],'partial')

    def test_collector_failure_preserves_one_native_success_without_context_requirement(self):
        result,records,_=self.capture(broken=True)
        self.assertEqual(records,[]);self.assertIn(b'needs_association',result.stderr)

    def test_standalone_native_operation_has_no_collector_requirement(self):
        _,records,_=self.capture(context=False)
        self.assertEqual(records,[])

    @unittest.skipUnless(sys.platform=='darwin','Actual macOS native boundary')
    def test_cli_archive_needs_no_management_database_read_or_write(self):
        _,records,_=self.capture(sandbox=True)
        self.assertEqual(len(records),1)
        record=json.loads(Path(records[0]['path']).read_bytes())
        self.assertTrue(record['capture_complete'])

    def test_host_index_failure_keeps_native_success_and_explicit_record_recovery(self):
        result,records,_=self.capture(index_failure=True)
        self.assertEqual(records,[]);self.assertEqual(result.returncode,0)
        original=next((self.home/'runs/capture-run/delivery').glob('operation-*/record.json'))
        report=self.save('manual-result.txt','Native success retained; associate its original operation archive.')
        argv=[sys.executable,str(REPO/'bin/harness.py'),'--root',str(self.root),'work-return',
            '--request','request','--result',report['path'],'--artifact',str(original)]
        cp=subprocess.run(argv,capture_output=True,text=True,timeout=10)
        self.assertEqual(cp.returncode,0,cp.stderr)
        with contextlib.closing(tasks._connect(self.root,True)) as c:
            returned=scope._returns(c,'task')[-1]
            pool=delivery.EvidenceSet(c,self.root,tasks.read_request(self.root,'request'),returned,scope.Evidence(self.root))
            self.assertTrue(pool.native_facts(pool.artifacts[0])['structure'])
        self.assertEqual((self.home/'operation-count').read_text(),'once\n')


if __name__=='__main__':
    unittest.main()
