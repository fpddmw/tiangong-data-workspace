"""Exercise the installed CLI in the real editor sandbox against an offline service."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
from tiangong_cli_tool import execute


SERVICE = """
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
const files = JSON.parse(process.env.FIXTURE_FILES);
const initial = JSON.parse(fs.readFileSync(files.baseline));
const request = JSON.parse(fs.readFileSync(process.env.FIXTURE_REQUEST));
const binding = {
  expectedActor: initial.expectedActor,
  expectedEnvironment: initial.expectedEnvironment,
};
const target = {modelId: initial.modelId, version: initial.version};
const record = (action, details) => fs.appendFileSync(files.events, JSON.stringify({action, ...details})+'\\n');
const read = file => JSON.parse(fs.readFileSync(file));
const response = result => Response.json({protocol:'model-bundle.v1', ok:true, ...result});
globalThis.fetch = async (url, options={}) => {
  const route = new URL(url);
  assert.equal(route.origin, binding.expectedEnvironment);
  if (route.pathname==='/auth/v1/user') {
    return Response.json({id:binding.expectedActor, email:'fixture@example.invalid', role:'authenticated'});
  }
  assert(route.pathname.endsWith('/save_lifecycle_model_bundle'));
  const {action, operationId, expectedRevision, plan, modelId, version} = JSON.parse(options.body);
  assert.deepEqual({modelId, version}, target);
  if (action==='inspect') {
    const current = fs.existsSync(files.commit) ? read(files.commit).snapshot : initial.snapshot;
    record(action, {revision:current.revision});
    return response({status:'observed', snapshot:current});
  }
  if (action==='recover') {
    const stored = read(files.commit);
    assert.equal(operationId,stored.receipt.operationId);
    record(action,{operationId});
    if (request.action==='save' && files.interrupt) throw Error('fixture recovery temporarily unavailable');
    return response(stored.receipt);
  }
  assert.equal(action,'save');
  assert.equal(expectedRevision,initial.snapshot.revision);
  assert(!fs.existsSync(files.commit), 'a second save was issued');
  const journal = path.join(os.homedir(),'.local/state/tiangong-lca/model-result-groups',operationId+'.json');
  assert.deepEqual(read(journal),{action:'recover',...target,...binding,operationId});
  const members = plan.parent.jsonTg.submodels;
  const processes = plan.processMutations.filter(p=>p.op!=='delete').map(p=>({
    id:p.id, version:p.version, model_id:modelId, model_version:version,
    state_code:0, json_ordered:p.jsonOrdered,
  }));
  assert.deepEqual(members.map(p=>p.id).sort(), processes.map(p=>p.id).sort());
  const revision=createHash('sha256').update(JSON.stringify(plan)).digest('hex');
  const model={id:modelId, version, state_code:0, json_ordered:plan.parent.jsonOrdered, json_tg:plan.parent.jsonTg};
  const snapshot={revision,model,members,processes};
  const receipt={status:'committed',...target,operationId,revision,members,savedAt:'2026-01-01T00:00:00Z'};
  fs.writeFileSync(files.commit,JSON.stringify({snapshot,receipt}));
  record(action,{operationId, journalBeforeSend:true, members:members.length});
  if (files.interrupt) throw Error('fixture connection lost after commit');
  return response(receipt);
};
"""


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def qualify(directory, runtime, recovery_runtime, original, baseline, interrupted):
    root = directory / 'workspace'
    root.mkdir(parents=True)
    (root / 'bin').mkdir()
    (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/python').symlink_to(sys.executable)
    home = directory / 'account'
    (home / '.local/state/tiangong-lca').mkdir(parents=True)
    target = {key: original[key] for key in ('modelId', 'version')}
    binding = {
        'expectedEnvironment': 'https://fixture.example.invalid',
        'expectedActor': '00000000-0000-4000-8000-000000000001',
    }
    files = {'commit': str(root / 'commit.json'), 'events': str(root / 'events.jsonl'),
             'baseline': str(write_json(root / 'baseline.json', {**baseline, **binding})),
             'interrupt': interrupted}
    hook = root / 'receiver.mjs'
    hook.write_text(SERVICE, encoding='utf-8')
    binding_path = write_json(root / 'runtime.json', runtime)
    launcher = root / 'bin/launch.py'
    launcher.write_text(f"""import json, os, sys
args = sys.argv[5:]
assert sys.argv[1:5] == ['--role', 'editor', 'cli', '--']
request = args[args.index('--input')+1]
with open({str(binding_path)!r}) as source:
    runtime = json.load(source)
env = {{k: os.environ[k] for k in ('HOME', 'TMPDIR', 'PATH')}}
env.update(FIXTURE_FILES={json.dumps(files)!r}, FIXTURE_REQUEST=request,
           TIANGONG_LCA_API_BASE_URL={binding['expectedEnvironment']!r},
           TIANGONG_LCA_ACCESS_TOKEN='offline-test-token',
           TIANGONG_LCA_SUPABASE_PUBLISHABLE_KEY='offline-test-key')
os.execve(runtime['node'], [runtime['node'], '--import', {str(hook)!r}, runtime['cli'], *args], env)
""", encoding='utf-8')

    def call(request):
        path = write_json(root / (request['action']+'.json'), request)
        argv = ['dataset', 'model', 'result-group', '--input', str(path), '--json']
        result = execute(root, {'role':'editor', 'surface':'cli', 'argv':argv},
                         writable_roots=(root,), guard=lambda:None, cancelled=lambda:False, network_access=False)
        response = json.loads(Path(result['stdout_ref']['path']).read_bytes())
        assert response.get('data'), response
        return response['data']

    with patch.dict(os.environ, {'HOME':str(home)}):
        saved = call({**original, **binding, 'baseline':files['baseline']})
        if interrupted:
            assert saved['status']=='unknown', saved['status']
            operation = saved['operationId']
            journal = Path(saved['recoveryFile'])
            assert journal.parent == home / '.local/state/tiangong-lca/model-result-groups'
            write_json(binding_path, recovery_runtime)
            saved = call(json.loads(journal.read_bytes()))
            assert saved['receipt']['operationId']==operation
        assert saved['status']=='verified', saved['status']
        inspected = call({'action':'inspect', **target})
    stored=json.loads(Path(files['commit']).read_bytes())
    assert saved['snapshot']==inspected['snapshot']==stored['snapshot']
    events=[json.loads(line) for line in Path(files['events']).read_text().splitlines()]
    sends=[entry for entry in events if entry['action']=='save']
    assert len(sends)==1 and sends[0]['journalBeforeSend']
    recoveries=[entry for entry in events if entry['action']=='recover']
    assert len(recoveries)==(2 if interrupted else 0)
    assert all(entry['operationId']==sends[0]['operationId'] for entry in recoveries)
    return {'case':'lost_commit_response' if interrupted else 'normal_save',
            'save_requests':len(sends), 'recovery_reads':len(recoveries),
            'journal_before_send':True, 'independent_group_readback_equal':True,
            'members':sends[0]['members']}


def installed_binding(path):
    runtime = json.loads(path.read_bytes())
    return {key: str(ROOT / runtime[key]) for key in ('node', 'cli')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True, help='Explicit CLI runtime binding of the installed tools')
    parser.add_argument('--recovery-runtime', type=Path, help='Switch to this installation after losing the original reply')
    parser.add_argument('--input', type=Path, required=True, help='Original qualified result-group save request')
    args=parser.parse_args()
    runtime = installed_binding(args.runtime)
    recovery_runtime = installed_binding(args.recovery_runtime) if args.recovery_runtime else runtime
    original=json.loads(args.input.read_bytes())
    baseline_path=args.input.parent / original['baseline']
    baseline=json.loads(baseline_path.read_bytes())
    cli = Path(runtime['cli'])
    with tempfile.TemporaryDirectory(prefix='htam-group-qualification-') as tmp:
        print(json.dumps({'cli':str(cli.relative_to(ROOT)) if cli.is_relative_to(ROOT) else str(cli),
                          **({'recovery_cli': recovery_runtime['cli']} if recovery_runtime != runtime else {}),
                          'cases':[qualify(Path(tmp).resolve()/str(interrupted),runtime,recovery_runtime,original,baseline,interrupted)
                                   for interrupted in (False,True)]}, indent=2))


if __name__=='__main__':
    main()
