// Exercise installed DSH policy and macOS sandbox, without a model or data write.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'
import {spawnSync} from 'node:child_process'
import {mkdtempSync,mkdirSync,rmSync,existsSync} from 'node:fs'
import {resolve,join} from 'node:path'
import {scopedPolicy} from '../bin/dsh_policy.mjs'
const require=createRequire(process.argv[2])
const native=name=>import(pathToFileURL(require.resolve('@deepseek-ai/'+name)))
const [{Context},{SandboxPolicyService},{LocalSandboxProvider},{workspaceWritableRoots}]=await Promise.all([
 native('cordis'),native('dsh-sandbox-policy'),native('dsh-sandbox-local'),native('dsh-sandbox')])
const cwd=resolve('.'),home=join(cwd,'.harness-local');mkdirSync(home,{recursive:true})
const probe=mkdtempSync(join(home,'filesystem-probe-'))
const pkg=join(probe,'original-package'),ledger=join(probe,'cli-state','tiangong-lca-cli')
mkdirSync(pkg,{recursive:true});mkdirSync(ledger,{recursive:true})
const ctx=new Context()
// Use the installed constructor/init; only persisted session state is isolated.
ctx.provide('sessionProjections',{async register(){return {stateOf:()=>null}}})
const policyService=ctx.plugin(scopedPolicy(SandboxPolicyService,[pkg,ledger],'workspace-write'),{workspaceRoot:cwd,mode:'workspace-write'})
await policyService.await()
const policy=ctx.sandboxPolicy
const session={id:'offline-policy-check',header:{cwd}}
assert.equal(policy.resolve({session}).workspaceRoot,pkg)
assert.equal(Object.hasOwn(policy,'resolve'),false,'Policy method is class-owned, not patched')
const service=ctx.plugin(LocalSandboxProvider,{runnerCommand:[],runnerFailureSignatures:[],probeTimeoutMs:5000})
try {
 await service.await()
 const selected=policy.resolve({session})
 assert.deepEqual(workspaceWritableRoots(selected),[pkg,ledger])
 assert.equal(session.header.cwd,cwd)
 for(const [path,allowed] of [[join(pkg,'candidate.txt'),true],[join(ledger,'probe.tmp'),true],[join(probe,'outside.txt'),false]]) {
  const command=[process.execPath,'--input-type=module','-e',"import{writeFileSync}from'node:fs';try{writeFileSync(process.argv[1],'offline probe')}catch{process.exit(17)}",path]
  const confined=await ctx.sandbox.confine(command,selected)
  const result=spawnSync(confined.argv[0],confined.argv.slice(1),{cwd,env:{...process.env,ELECTRON_RUN_AS_NODE:'1'},encoding:'utf8',timeout:15000})
  assert.equal(result.status,allowed?0:17,result.stderr)
  assert.equal(existsSync(path),allowed)
 }
 assert.equal(policy.resolve({session,mode:'read-only'}).mode,'read-only')
 assert.throws(()=>policy.resolve({session,mode:'danger-full-access'}),/exceeds/)
 console.log(JSON.stringify({status:'passed',cwd,package_write:true,cli_ledger_write:true,outside_write_denied:true,session_cwd_unchanged:true,model_calls:0,data_writes:0}))
} finally {
 await service.dispose();await policyService.dispose();rmSync(probe,{recursive:true,force:true})
}
