"""Verify the installed dataset CLI through the managed editor launcher, offline."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bin'))
from lca_gate_references import reference_body
from tiangong_cli_tool import execute
import harness_tasks as tasks
import test_agent_runtime as fixture

PACKAGE=ROOT/'materials/packages/tiangong-package-gap-global-main-20261110/apple-scm-reuse-20261110/apple/material-dispatch-20261115'
OWNER=PACKAGE/'program-de153e04488e9a60'
ORIGINAL=OWNER/'runs/program:1cc122e39427c3bd24eba9569ec6aa6b/delivery/operation-190c1ae58d754174a04ee9761b67e495/input.json'
ORIGINAL_HASH='2060d358f951f0557700e1effd59587a42a888a4986306e9626d94161f55410c'
ORIGINAL_DELIVERY=ORIGINAL.parents[1]
FLOW_DELIVERY=OWNER/'runs/program:91ade1b19b072c245bba2494e918c7b7/delivery'
ZINC_READ='129dd76526654839827e234e6eb53849'
REVISED_ZINC_READ='4b3e3288ab4e430ab8641b0ea6164c87'
TABLES={'source':'sources','flow':'flows','process':'processes','contact':'contacts',
        'flowproperty':'flowproperties','unitgroup':'unitgroups','lifecyclemodel':'lifecyclemodels'}
MODIFIED='2026-01-01T00:00:00.000Z'

RECEIVER=r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {syncBuiltinESMExports} from 'node:module';
const cfg=JSON.parse(fs.readFileSync(process.env.QUALIFICATION_CONFIG));
const log=(event)=>fs.appendFileSync(cfg.events,JSON.stringify(event)+'\n');
const originalRead=fs.readFileSync;
fs.readFileSync=function(file,...options) {
  try {
    const value=originalRead.call(this,file,...options);
    if (file===0 || file===cfg.input) {
      const raw=Buffer.isBuffer(value) ? value : Buffer.from(value);
      log({input:file===0?'fd0':'path',bytes:raw.length,sha256:createHash('sha256').update(raw).digest('hex')});
    }
    return value;
  } catch(e) {
    if (file===0) log({input:'fd0',systemCode:e.code});
    throw e;
  }
};
syncBuiltinESMExports();
if (cfg.replace && process.argv.includes('-')) fs.writeFileSync(cfg.input,'{"replaced":true}');
globalThis.fetch=async (resource, options={})=>{
  const url=new URL(resource?.url ?? resource);
  assert.equal(url.origin,cfg.origin);
  if (url.pathname==='/auth/v1/user') {
    return Response.json({id:cfg.actor,email:'offline@example.invalid',role:'authenticated'});
  }
  const table=url.pathname.split('/').at(-1);
  if (table==='app_dataset_save_draft') {
    const body=JSON.parse(options.body);
    assert.equal(body.id,cfg.target.id);
    assert.equal(body.version,cfg.target.version);
    assert.deepEqual(body.expectedJsonOrdered,cfg.target.expectedContent);
    assert.deepEqual(body.jsonOrdered,cfg.target.content);
    assert.equal(fs.existsSync(cfg.committed),false,'unexpected second write');
    log({send:'save'});
    if (cfg.reject) return Response.json({code:'forbidden',message:'Fixture exact save refused'}, {status:403});
    fs.writeFileSync(cfg.committed,JSON.stringify(body.jsonOrdered));
    if (cfg.lose) throw Error('Fixture response lost after write');
    return Response.json({ok:true});
  }
  assert(url.pathname.startsWith('/rest/v1/'),url.pathname);
  let rows=cfg.rows[table] ?? [];
  const id=url.searchParams.get('id');
  const version=url.searchParams.get('version');
  if (id?.startsWith('eq.')) rows=rows.filter(r=>r.id===id.slice(3));
  if (id?.startsWith('in.')) rows=rows.filter(r=>id.includes(r.id));
  if (version?.startsWith('eq.')) rows=rows.filter(r=>r.version===version.slice(3));
  rows=rows.map(row=>{
    if(row.id!==cfg.target.id || row.version!==cfg.target.version || !fs.existsSync(cfg.committed)) return row;
    const body=JSON.parse(originalRead(cfg.committed));
    return {...row,json:body,json_ordered:body};
  });
  if (!rows.length) log({unmatched:table,id,version});
  const count=rows.length;
  rows=rows.slice(0, Number(url.searchParams.get('limit') ?? '100'));
  return Response.json(rows,{headers:{'content-range':count?'0-'+(rows.length-1)+'/'+count:'*/0'}});
};
"""


def fixture_rows(target, zinc_read=ZINC_READ):
    # A later read of the same draft version can have a different body.
    sources=[
        *(FLOW_DELIVERY/f'operation-{operation}/output.json' for operation in (
            '448fb07988e1494f91be93da0a4f2868', 'b4c125290bc249b9b5b018ec18528421',
            '65d24569f5bf4acd8b09019f79da42ee', '9af18743ce844fb9a3646005c8524fc0',
            'ecf3973c7d184121b43d562716279c48', '4d3031cc5b634736a437f46a0f59e47d',
        )),
        *(ORIGINAL_DELIVERY/f'operation-{operation}/output.json' for operation in (
            zinc_read, '333924dbacd94a378c9a4631712eba06', 'c67f14c2852a4d30b2160046c21a1884',
        )),
        PACKAGE/'al-two-sheet-routes-ref-contact-f4b4c314-8c4c-4c83-968f-5b3c7724f6a8-01.00.001-20261118-2305.json',
        *(PACKAGE/f'salts-three-li-ref-source-{reference}-20261118-2113.json' for reference in (
            '06ae18cc-f26f-5fa2-b9da-c574a21d5e8a-00.00.001',
            'a97a0155-0234-4b87-b4ce-a45da52f2a40-03.00.003',
            'd92a1a12-2545-49e2-a585-55c259997756-20.20.002',
        )),
    ]
    rows={}
    for path in sources:
        doc=json.loads(path.read_bytes())
        key,body=reference_body(doc.get('data',{}).get('content') or doc)
        assert body is not None, path
        table_key=(TABLES[key[0]],*key[1:])
        assert table_key not in rows, (path,table_key)
        rows[table_key]=body
    rows[('processes',target['id'],target['version'])]=target['expectedContent']
    result={}
    for (table,identifier,version),body in rows.items():
        result.setdefault(table,[]).append({'id':identifier,'version':version,'state_code':0 if
            (identifier,version)==(target['id'],target['version']) else 100,'user_id':target['expectedActor'],
            'json':body,'json_ordered':body,'modified_at':MODIFIED,'model_id':None,'model_version':None})
    return result


class InputQualification(unittest.TestCase):
    setUp=fixture.RuntimeTests.setUp
    create_request=fixture.RuntimeTests.create_request
    ref=fixture.RuntimeTests.ref

    def test_installed_manageable_input(self):
        raw=ORIGINAL.read_bytes()
        self.assertEqual((len(raw),hashlib.sha256(raw).hexdigest()),(68349,ORIGINAL_HASH))
        target=json.loads(raw)
        runtime=json.loads((ROOT/'.control/runtime.json').read_bytes())
        original_runtime_path=os.environ.get('HTAM_1014_RECOVERY_FROM_RUNTIME')
        recovery_from=json.loads(Path(original_runtime_path).read_bytes()) if original_runtime_path else runtime
        shutil.copytree(ROOT/'bin',self.root/'bin',ignore=shutil.ignore_patterns('__pycache__'))
        (self.root/'.venv/bin').mkdir(parents=True)
        (self.root/'.venv/bin/python').symlink_to(sys.executable)
        account=self.root/'offline-home'
        journals=account/'.local/state/tiangong-lca/dataset-operations'
        journals.mkdir(parents=True)
        (self.root/'.control/accounts.json').write_text(json.dumps({'project_ref':'isolated-a08',
            'editor':{'user_id':target['expectedActor']}}))
        (self.root/'.env').write_text('DATA_EDITOR_SESSION_FILE=offline-not-a-credential\n'
                                      'TIANGONG_LCA_API_BASE_URL='+target['expectedEnvironment']+'\n')
        (self.root/'.env').chmod(0o600)
        receiver=self.home/'receiver.mjs'
        receiver.write_text(RECEIVER)
        shim=self.home/'installed-node'

        def bind(binding):
            node,cli=(ROOT/binding[key] for key in ('node','cli'))
            self.assertTrue(node.is_file() and cli.is_file())
            shim.write_text(f'''#!{sys.executable}
import os,sys
os.environ.update(TIANGONG_LCA_AUTH_MODE='access-token',TIANGONG_LCA_ACCESS_TOKEN='offline-test',
    TIANGONG_LCA_SUPABASE_PUBLISHABLE_KEY='offline-test',QUALIFICATION_CONFIG={str(self.home/'fixture.json')!r})
os.execv({str(node)!r}, [{str(node)!r}, '--import', {str(receiver)!r}, *sys.argv[1:]])
''')
            shim.chmod(0o700)
            (self.root/'.control/runtime.json').write_text(json.dumps({'node':str(shim),'cli':str(cli)}))

        bind(runtime)
        run='qualify-a08'
        launch=self.home/'launch.json'
        launch.write_text(json.dumps({'run_id':run,'request_id':'request','native_cwd':str(self.root),'work_home':str(self.home)}))
        tasks.mutate(self.root,'run-intent',self.actor,{'id':run,'request_id':'request',
            'config_ref':self.ref(self.config_path),'input_refs':[self.ref(launch), self.input_ref],
            'target_host':'codex','operation':'start'})
        usage=self.home/'runs'/run
        usage.mkdir(parents=True)
        # Keep the original owner's WAL/SHM files present for sandboxed read-only admission.
        controller=tasks._connect(self.root)
        self.addCleanup(controller.close)
        controller.execute('SELECT COUNT(*) FROM work_runs').fetchone()
        lease=SimpleNamespace(path=usage/'execution.json',value={'run_id':run,'request_id':'request'},
            tool_intent=lambda key:None,tool_not_started=lambda key:None,tool_spawned=lambda key,process:None)
        docs=fixture_rows(target)

        def call(path,*args):
            with patch.dict(os.environ,{'HOME':str(account)}):
                result=execute(self.root,{'role':'editor','surface':'cli',
                    'argv':['dataset','process','save','--input',str(path),'--json',*args]},
                    lease=lease,writable_roots=(self.home,journals),guard=lambda:None,cancelled=lambda:False,
                    network_access=False)
            stdout=Path(result['stdout_ref']['path']).read_bytes()
            return json.loads(stdout) if stdout else None, result['stderr']

        def prepare(name,raw_input,*,rows=docs,**flags):
            path=self.home/'case'/name/'input.json'
            path.parent.mkdir(parents=True)
            path.write_bytes(raw_input)
            fixture={**flags,'input':str(path),'origin':target['expectedEnvironment'],'actor':target['expectedActor'],
                     'target':target,'rows':rows,'events':str(path.with_name('events.jsonl')),
                     'committed':str(path.with_name('committed.json'))}
            (self.home/'fixture.json').write_text(json.dumps(fixture))
            return path

        def events(path):
            log=path.with_name('events.jsonl')
            return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

        compact=json.dumps(target,ensure_ascii=False,separators=(',',':')).encode()
        padded=raw+b' '*(200_000-len(raw))
        proof=[]
        for name,raw_input,flags in [('A08',raw,{}),('compact',compact,{}),('multibyte-200KB',padded,{}),
                ('replaced-source',raw,{'replace':True}),('lost-reply',raw,{'lose':True})]:
            path=prepare(name,raw_input,**flags)
            preview,err=call(path,'--dry-run')
            self.assertEqual((preview or {}).get('outcome'),'preview_completed',
                             ((preview or {}).get('error'),err,events(path)[-25:]))
            self.assertFalse(any('send' in item for item in events(path)))
            if flags.get('lose'):
                bind(recovery_from)
            try:
                saved,err=call(path)
            finally:
                if flags.get('lose'):
                    bind(runtime)
            expected='write_outcome_unknown' if flags.get('lose') else 'dataset_written'
            self.assertEqual((saved or {}).get('outcome'),expected,((saved or {}).get('error'),err,events(path)[-25:]))
            self.assertEqual(json.loads(path.with_name('committed.json').read_bytes()),target['content'])
            if flags.get('lose'):
                follow,_=call(path)
                self.assertEqual(follow['outcome'],'write_outcome_unknown',follow)
                self.assertTrue(follow['data']['readback']['contentMatchesIntent'])
            observed=events(path)
            sha=hashlib.sha256(raw_input).hexdigest()
            reads=[i for i in observed if i.get('input')]
            self.assertTrue(reads and all(i.get('bytes')==len(raw_input) and i.get('sha256')==sha for i in reads),reads)
            self.assertEqual(sum('send' in i for i in observed),1)
            proof.append({'case':name,'bytes':len(raw_input),'sha256':sha,'native_reads':reads,
                'preview':preview['outcome'],'outcome':saved['outcome'],'saves':1,'complete_body_equal':True})
            if flags.get('lose'):
                proof[-1].update(initial_cli=recovery_from['cli'],recovery_cli=runtime['cli'],
                                 recovery_outcome=follow['outcome'], readback_matches_intent=True)
            for journal in journals.glob('*.json'):
                journal.unlink()

        path=prepare('revised-reference',raw,rows=fixture_rows(target,REVISED_ZINC_READ))
        checked_bindings=[runtime] if recovery_from==runtime else [recovery_from,runtime]
        for candidate in checked_bindings:
            bind(candidate)
            response,err=call(path,'--dry-run')
            self.assertEqual((response or {}).get('error',{}).get('code'),'PROCESS_FLOW_NAME_ADMISSION_BLOCKED',
                             (response,err))
        self.assertFalse(any('send' in item for item in events(path)))
        proof.append({'case':'revised-reference','outcome':'PROCESS_FLOW_NAME_ADMISSION_BLOCKED','saves':0,
                      'checked_cli':[candidate['cli'] for candidate in checked_bindings]})

        path=prepare('malformed',b'{"incomplete":')
        preview,_=call(path,'--dry-run')
        self.assertEqual(preview['error']['code'],'INPUT_INVALID_JSON')
        saved,err=call(path)
        self.assertIsNone(saved)
        self.assertEqual(json.loads(err)['stage'],'input_json')
        self.assertFalse(any('send' in i for i in events(path)))
        path=prepare('re-location',raw)
        external=self.root.parent/('missing-'+self.root.name+'.json')
        saved,err=call(external)
        self.assertIsNone(saved)
        self.assertEqual(json.loads(err)['stage'],'input_path')
        saved,err=call(path.with_name('missing.json'))
        self.assertIsNone(saved)
        self.assertEqual((json.loads(err)['stage'],json.loads(err)['system_code']),('input_open','ENOENT'))
        proof.extend([{'case':'malformed','stage':'input_json','saves':0},
                      {'case':'out-of-workspace','stage':'input_path','saves':0},
                      {'case':'missing-file','stage':'input_open','saves':0}])
        denied=prepare('re-unreadable',raw)
        denied.chmod(0)
        try:
            saved,err=call(denied)
            self.assertIsNone(saved)
            result=json.loads(err)
            self.assertEqual(result['stage'],'input_open')
            self.assertIn(result['system_code'],('EACCES','EPERM'))
            self.assertEqual(events(denied),[])
            proof.append({'case':'unreadable-file','stage':result['stage'],'system_code':result['system_code'],'saves':0})
        finally:
            denied.chmod(0o600)
        rejected=prepare('service-rejection',raw,reject=True)
        response,_=call(rejected)
        self.assertIsNotNone(response)
        self.assertIsNotNone(response.get('error'),response)
        self.assertFalse(rejected.with_name('committed.json').exists())
        self.assertEqual(sum(i.get('send')=='save' for i in events(rejected)),1)
        proof.append({'case':'service-rejection','sends':1,'committed':False,'cli_error':response['error']['code']})
        for journal in journals.glob('*.json'):journal.unlink()

        path=prepare('public-stdin',raw)
        from delivery_evidence import context_environment
        from agent_base import child_environment
        proc=subprocess.run(['/usr/bin/sandbox-exec','-p','(version 1) (allow default) (deny network*)',
            str(self.root/'.venv/bin/python'),str(self.root/'bin/launch.py'),'--role','editor','cli','--',
            'dataset','process','save','--input','-','--json'],input=raw,cwd=self.root,
            env={**child_environment(),**context_environment(self.root,lease),'HOME':str(account)},
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
        self.assertEqual(proc.returncode,0,proc.stderr.decode())
        observed=events(path)
        self.assertIn({'input':'fd0','bytes':len(raw),'sha256':ORIGINAL_HASH},observed)
        self.assertEqual(sum(row.get('send')=='save' for row in observed),1)
        self.assertEqual(json.loads(path.with_name('committed.json').read_bytes()),target['content'])
        proof.append({'case':'public-stdin','bytes':len(raw),'sha256':ORIGINAL_HASH,'saves':1,'complete_body_equal':True})
        if output:=os.environ.get('HTAM_1014_INPUT_EVIDENCE'):
            Path(output).write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    unittest.main()
