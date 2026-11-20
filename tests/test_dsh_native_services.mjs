// Installed native services, isolated objects; no network/model/session creation.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {pathToFileURL,fileURLToPath} from 'node:url'
import {execFileSync} from 'node:child_process'
import {installIdentityRefresh,installReviewRequest,installReviewResult,installLocalCheck,installFindingReport,installTiangongCli} from '../bin/dsh_delivery.mjs'
const require=createRequire(process.argv[2])
const native=name=>import(pathToFileURL(require.resolve('@deepseek-ai/'+name)))
let tool
await installIdentityRefresh({tools:{register:t=>tool=t}},process.argv[2],{async request(kind) {assert.equal(kind,'refresh_editor_identity');return {accepted:true,diagnostic:{status:'passed'}}}})
assert.equal(tool.name,'refresh_editor_identity')
assert.equal(JSON.parse(await tool.execute({},{signal:new AbortController().signal})).diagnostic.status,'passed')

const declarations=JSON.parse(execFileSync(fileURLToPath(new URL('../.venv/bin/python',import.meta.url)),
  ['-c','import json;from collaboration_tools import CONTROLLED_TOOLS;print(json.dumps(CONTROLLED_TOOLS))'],
  {encoding:'utf8',env:{...process.env,PYTHONPATH:fileURLToPath(new URL('../bin',import.meta.url))}}))
const cliArgs={role:'editor',surface:'cli',argv:['--version','; literal 参数']}
const cliSignal=new AbortController().signal
await installTiangongCli({tools:{register:t=>tool=t}},process.argv[2],{
 async request(kind,text,signal){assert.equal(kind,'tiangong_cli');assert.deepEqual(JSON.parse(text),cliArgs);assert.equal(signal,cliSignal);return {accepted:true,diagnostic:{exit_code:7}}}
},declarations.tiangong_cli)
assert.equal(tool.name,'tiangong_cli')
assert.equal(JSON.parse(await tool.execute(cliArgs,{signal:cliSignal})).diagnostic.exit_code,7)

const author={id:'original-author'}, reviewArgs={artifacts:['work/candidate.json','work/calculation.md'],question:'Check the source and unit balance.'}
let reviewDisarms=0, reviewAccepted=false
await installReviewRequest({tools:{register:t=>tool=t},agents:{get:id=>id===author.id?author:undefined},
 goals:{disarm:a=>{assert.equal(a,author);reviewDisarms++}}},process.argv[2],{
 async request(kind,text){assert.equal(kind,'request_review');assert.deepEqual(JSON.parse(text),reviewArgs);return {accepted:reviewAccepted}}
},declarations.request_review)
assert.equal(tool.name,'request_review')
assert.equal(tool.description,declarations.request_review.description)
assert.equal(JSON.parse(await tool.execute(reviewArgs,{agent:author})).accepted,false)
assert.equal(reviewDisarms,0)
reviewAccepted=true
assert.equal(JSON.parse(await tool.execute(reviewArgs,{agent:author})).accepted,true)
assert.equal(reviewDisarms,0)
await assert.rejects(tool.execute(reviewArgs,{agent:{id:author.id}}),/original live author/)
await installReviewResult({tools:{register:t=>tool=t}},process.argv[2],{
 async request(kind,text){assert.equal(kind,'review_result');return {accepted:true,diagnostic:JSON.parse(text)}}
})
assert.equal(tool.name,'review_result')
assert.equal(JSON.parse(await tool.execute({review_id:'review-original'},{})).diagnostic.review_id,'review-original')
assert.equal(JSON.parse(await tool.execute({review_id:'review-original',wait_seconds:10},{})).diagnostic.wait_seconds,10)
let findingArgs={report:'work/observed-error.md'}
await installFindingReport({tools:{register:t=>tool=t}},process.argv[2],{
 async request(kind,text){assert.equal(kind,'report_finding');assert.deepEqual(JSON.parse(text),findingArgs);return {accepted:true,diagnostic:{finding_id:'finding:original'}}}
})
assert.equal(tool.name,'report_finding')
assert.equal(JSON.parse(await tool.execute(findingArgs,{})).diagnostic.finding_id,'finding:original')
findingArgs={...findingArgs,finding_id:'finding:original'}
assert.equal(JSON.parse(await tool.execute(findingArgs,{})).diagnostic.finding_id,'finding:original')
await installLocalCheck({tools:{register:t=>tool=t}},process.argv[2],{
 async request(kind,text){assert.equal(kind,'local_check');return {accepted:true,diagnostic:JSON.parse(text)}}
})
assert.equal(tool.name,'local_check')
assert.deepEqual(JSON.parse(await tool.execute({argv:['python3','-c','print(8*2)']},{})).diagnostic.argv,['python3','-c','print(8*2)'])

// Native phase, not the wording of a final, controls continuation.
const driver=await native('dsh-goal-round-driver')
for (const phase of ['active','blocked','paused','complete']) {
 const handlers=new Map(),cleanups=[],queue=[];let cleaningUp=false
 const goal={id:'original-goal',revision:1,phase,activation:'armed',roundsStarted:0,maxGoalRounds:10,objective:'Original delivery'}
 const session={id:'original-author',snapshotEvents:()=>[{type:'assistant/message',data:{turn:1,message:{content:[{type:'text',text:'Waiting for an external measurement.'}]}}}]}
 const live={id:session.id,session,status:'running',whenIdle:async()=>{},inbox:{nextTurn:[],nextStep:[],remove(){}},
  followup(message){queue.push(message);this.status='running';this.inbox.nextTurn.push(message)}}
 const emit=(name,...args)=>(handlers.get(name)||[]).map(fn=>fn(...args))
 const ctx={fiber:{state:2},logger:{warn:message=>assert.fail(message)},get:()=>undefined,
  on(name,fn){handlers.set(name,[...(handlers.get(name)||[]),fn])},
  effect(fn){const value=fn();if(value?.next)cleanups.push(value.next().value)},
  sessions:{flush:async()=>{}},agents:{get:id=>id===live.id?live:undefined,list:()=>[],withoutInitiator:fn=>fn()},
  goals:{get:()=>({...goal}),disarm(){assert.equal(cleaningUp,true,'No format-driven disarm')},
   resume(){assert.fail('No automatic resume')},block(){assert.fail('No automatic block')},pause(){assert.fail('No automatic pause')}}}
 driver.apply(ctx)
 emit('agent/created',{agent:live})
 emit('session/event',session,{type:'turn/end',data:{turn:1,reason:{kind:'completed'}}})
 live.status='idle';emit('agent/status',{agent:live,status:'idle'})
 await new Promise(resolve=>setTimeout(resolve,30))
 assert.equal(queue.length,phase==='active'?1:0,`native phase ${phase} controls continuation`)
 assert.equal(goal.phase,phase)
 cleaningUp=true;for(const cleanup of cleanups)if(cleanup){live.status='idle';await cleanup()}
}
console.log('Installed native Goal driver follows explicit phase and does not classify final prose.')

const {requiredImageOffload}=await native('dsh-llm')
const offload=await native('dsh-compaction-image-offload')
const events=[{seq:0,type:'user/message',data:{content:[{type:'image',attachment:{attachmentId:'a'}}]}},
 {seq:1,type:'tool/result',data:{message:{content:['b','c','d','e'].map(attachmentId=>({type:'image',attachment:{attachmentId}}))}}}]
const originals=JSON.stringify(events)
const messages=new Map(events.map(e=>[e.seq,e.type==='user/message'?e.data:e.data.message]))
let projection;const handlers=new Map(),appended=[]
offload.apply({sessions:{registerMessageProjection:p=>projection=p},on:(name,fn)=>handlers.set(name,fn)})
const session={surface:{nodes:[0,1]},eventAt:i=>events[i],deriveEventMessage:e=>messages.get(e.seq),append(type,data){
 appended.push({type,data});const changed=projection.project({type,data},{nodes:[0,1],messages,events,baseSeq:0})
 for(const [key,value] of changed)messages.set(key,value)
}}
const count=()=>requiredImageOffload([...messages.values()],{representation:'bytes',maxImages:4,countQuantum:1},()=>100)
assert.equal(count(),1)
assert.deepEqual(await handlers.get('agent/request-error')({agent:{session},failure:{code:'IMAGE_OFFLOAD_REQUIRED',offloadImages:count()},signal:new AbortController().signal},()=>assert.fail('Expected native offload')),{kind:'retry'})
assert.equal(count(),0)
assert.equal(appended.length,1);assert.equal(appended[0].type,'image/offload')
assert.equal(JSON.stringify(events),originals)
assert.deepEqual(appended[0].data.targets,[{seq:0,imageIndexes:[0]}])
console.log('Installed author tools preserve native collaboration; four-image request budget offloads oldest input without losing original events or replacing Session.')

// Full installed Agent + formal SystemPrompt middleware + real native surface
// compaction. This uses only an isolated DB, local scripted adapter and Sessions.
{
 const {mkdtemp,writeFile,readFile,mkdir}=await import('node:fs/promises')
 const {tmpdir}=await import('node:os')
 const {join,dirname}=await import('node:path')
 const {fileURLToPath}=await import('node:url')
 const {execFile}=await import('node:child_process')
 const {promisify}=await import('node:util')
 const execute=promisify(execFile),repo=dirname(dirname(fileURLToPath(import.meta.url)))
 const root=await mkdtemp(join(tmpdir(),'htam-context-native-'))
 await writeFile(join(root,'ISOLATED-CONTEXT-FIXTURE'),'isolated native service qualification')
 const python=join(repo,'.venv/bin/python'),fixture=join(repo,'tests/context_native_fixture.py')
 const env={...process.env,DSH_HOME:join(root,'dsh-home'),ELECTRON_RUN_AS_NODE:'1'}
 for(const key of ['CODEX_THREAD_ID','CODEX_TURN_ID','DSH_SESSION_ID','DSH_SHELL','CLAUDE_SESSION_ID'])delete env[key]
 await execute(python,[fixture,'init',root],{env,timeout:10000})
 const patch=join(root,'patch.json')
 await writeFile(patch,JSON.stringify([
  {id:'session-title-llm',disabled:true},{id:'headless-runner',disabled:true},
  {insert:[{id:'isolated-context-driver',name:join(repo,'tests/dsh_context_native_driver.mjs'),inject:['llm','agents','sessions','compaction','systemPrompt'],
   config:{moduleEntry:process.argv[2],workspaceRoot:root,python,fixture,snapshotEntry:join(repo,'bin/harness_context_hook.py')}}]}
 ]))
 try {
  const result=await execute('/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh',
   ['--profile','headless','--patch',patch,'--json','--permission-mode','read-only','--approval-policy','never','Isolated native context qualification'],
   {env,cwd:root,timeout:45000,maxBuffer:4*1024*1024})
  const output=result.stdout.split('\n').filter(Boolean).map(line=>{try{return JSON.parse(line)}catch{return null}})
  const proof=output.find(r=>r?.type==='qualification')
  assert.equal(proof?.result,'passed',JSON.stringify({proof,stderr:result.stderr}))
  console.log('Installed native context qualification: '+JSON.stringify(proof))
 } catch(error) {
  console.error('Isolated native context artifacts: '+root)
  console.error(error.stdout??'');console.error(error.stderr??'');throw error
 }
}
