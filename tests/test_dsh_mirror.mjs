import assert from 'node:assert/strict'
import {resolve, join} from 'node:path'
import {pathToFileURL} from 'node:url'

assert.ok(process.argv[2], 'Pass the installed CLI mirror directory to qualify it.')
const {mkdtempSync,writeFileSync,chmodSync,existsSync,rmSync}=await import('node:fs')
const {tmpdir}=await import('node:os')
const observedDir=mkdtempSync(join(tmpdir(),'htam-mirror-read-'))
process.on('exit',()=>rmSync(observedDir,{recursive:true,force:true}))
const sessionFile=join(observedDir,'session.jsonl')
writeFileSync(sessionFile,'')
const mirror = resolve(process.argv[2])
const {summarize,mergeSummary,apply,runHeadlessTurn} = await import(pathToFileURL(join(mirror, 'index.js')))
const start=turn=>({type:'turn/start',data:{turn}})
const end=(turn,kind)=>({type:'turn/end',data:{turn,reason:{kind}}})
const recovered=summarize([start(1),end(1,'error'),start(2),end(2,'completed')])
assert.equal(recovered.turnFailed,false);assert.equal(recovered.failedTurns,1)
assert.equal(recovered.lastTurnOutcome,'completed');assert.equal(recovered.openTurns,0)
const running=summarize([start(1),end(1,'error'),start(2)])
assert.equal(running.turnFailed,false);assert.equal(running.openTurns,1)
assert.equal(summarize([end(1,'aborted')]).openTurns,0)
assert.equal(summarize([start(1),end(1,'aborted')]).lastTurnOutcome,'aborted')
assert.equal(summarize([]).lastTurnOutcome,null)
console.log('Mirror distinguishes current turn, historical failure, stopped and unstarted sessions.')

// Every polling partition must preserve the same turn facts as a full read.
const history=[start(1),{type:'step/start'},end(1,'error'),
 {type:'session/title',data:{title:'Failed run'}},start(2),{type:'tool/call',data:{name:'check'}},
 start(3),end(2,'completed'),{type:'step/start'},end(3,'aborted'),
 {type:'session/title',data:{title:'Stopped run'}},start(4),end(4,'completed')]
const facts=s=>Object.fromEntries(['openTurns','openTurnIds','latestTurn','lastTurnOutcome',
 'turnFailed','failedTurns','turns','steps','tools'].map(k=>[k,s[k]]))
for(let endAt=1;endAt<=history.length;endAt++) {
 for(let split=1;split<=endAt;split++) {
  const whole=summarize(history.slice(0,endAt))
  const incremental=mergeSummary(summarize(history.slice(0,split)),history.slice(split,endAt))
  assert.deepEqual(facts(incremental),facts(whole),`poll split ${split}/${endAt}`)
 }
}

// Exercise the actual cached /list handler, including quiet and resumed polls.
let handler,events=[],revision=0,duringRead=[]
apply({sessions:{list:()=>[]},workspaceRegistry:{list:()=>[]},
 sessionPersistence:{list:async()=>[{header:{id:'observed',cwd:'/fixture'},location:{path:sessionFile},revision,eventCount:events.length}],
  open:async(_id,access)=>{assert.equal(access,'read');return {read:async from=>{
   if(duringRead.length){events.push(...duringRead);duringRead=[];revision++}
   return {events:events.slice(from)}
  },close:async()=>{}}}},
 effect:fn=>fn(),webServer:{register:route=>{handler=route.handler;return ()=>{}}},
},{projectRoot:'',cacheDir:observedDir})
async function poll(batch) {
 events.push(...batch);if(batch.length)revision++
 let payload,status
 await handler({url:'/cli-mirror-api/list'},{writeHead:code=>{status=code},end:body=>{payload=JSON.parse(body)}})
 assert.equal(status,200,payload?.error)
 return payload.groups[0].sessions[0]
}
assert.equal((await poll([start(1)])).running,true)
assert.equal((await poll([{type:'step/start'}])).running,true)
assert.equal((await poll([])).running,true)
assert.equal((await poll([end(1,'error')])).turnFailed,true)
assert.equal((await poll([{type:'session/title',data:{title:'Error remains'}}])).turnFailed,true)
assert.equal((await poll([start(2)])).running,true)
assert.equal((await poll([end(2,'completed')])).lastTurnOutcome,'completed')
assert.equal((await poll([{type:'session/title',data:{title:'Complete remains'}}])).running,false)
// Native writes can land between listing the snapshot and reading its tail.
duringRead=[start(3)]
assert.equal((await poll([{type:'session/title',data:{title:'New native turn'}}])).turns,3)
assert.equal((await poll([])).turns,3,'An event already folded must not be counted again')
console.log('Incremental polling preserves live turns, failure, resume and actual completion.')

// Evaluate the actual panel components with a tiny element renderer; no browser/session writes.
const {readFileSync}=await import('node:fs')
const {runInNewContext}=await import('node:vm')
let components
const source=readFileSync(join(mirror, 'client.js'),'utf8')
const marker="    return {\n      inject: ['slots'],"
assert.ok(source.includes(marker))
const React={createElement:(type,props,...children)=>({type,props,children})}
runInNewContext(source.replace(marker,"    capture({PkgRow,PackageFacts});\n"+marker),{
 window:{__ModuleLoader__:{load:({factory})=>factory(()=>React)}},capture:value=>components=value,
})
const pkg={id:'ready',package:'Ready package',sessions:[],programs:[],stage:{declared:{value:'scientific_production'}},nativeStatus:'not_started'}
const options={pkg,active:false,now:Date.now(),onPick(){}}
assert.ok(components.PkgRow(options),'Registered package without a Session must remain visible')
assert.ok(components.PkgRow({...options,pkg:{...pkg,sessions:[{id:'hidden'}]}}),'Hidden sessions retain their package row')
const issue={meaning:'Original evidence changed',error:'ValueError',ref:{path:'/exact/original.pdf'}}
const rendered=JSON.stringify(components.PackageFacts({pkg:{...pkg,evidenceIssues:[issue]}}))
assert.ok(rendered.includes('Original evidence changed'));assert.ok(rendered.includes('/exact/original.pdf'))
assert.ok(!rendered.includes('[object Object]'))
console.log('Actual panel retains unstarted packages and renders evidence diagnostics.')

const goalEvent={type:'goal/change',data:{operation:'resume',goal:{phase:'active',objective:'Finish'}}}
assert.equal(summarize([goalEvent]).goalPhase,'active')
assert.equal(mergeSummary(summarize([goalEvent]),[start(2),end(2,'completed')]).goalPhase,'active')
assert.equal(mergeSummary(summarize([goalEvent]),[{type:'goal/change',data:{operation:'complete',goal:{phase:'complete'}}}]).goalPhase,'complete')
const full=JSON.stringify(components.PackageFacts({pkg:{...pkg,currentPlan:{path:'/current/plan.md'},planRevision:4,
 continuationRef:{path:'/original/continuation.md'},goalObservation:{source:'dsh_live_agent',goal:{phase:'active',activation:'disarmed'}}}}))
for(const value of ['/current/plan.md','/original/continuation.md','disarmed','非当前实时状态','实际阶段未标注']) assert.ok(full.includes(value))

let reply
runInNewContext(source.replace(marker,"    capture(ReplyBox);\n"+marker),{
 window:{__ModuleLoader__:{load:({factory})=>factory(()=>({...React,useState:v=>[v,()=>{}],useRef:()=>({}),useEffect:()=>{}}))}},
 capture:value=>reply=value,
})
const idleReply=reply({row:{running:false,goalOperation:'resume',goalPhase:'active'}})
assert.equal(idleReply.children[0].props.disabled,false,'Persisted unfinished Goal is not a live process lock')

// The real continuation launcher returns acceptance; the child remains alive
// until its own work ends. This executable only writes local fixture evidence.

const home=mkdtempSync(join(tmpdir(),'htam-mirror-send-'))
try {
 const launcher=join(home,'fake-dsh'),done=join(home,'finished')
 writeFileSync(launcher,`#!/usr/bin/env python3\nimport time\nfrom pathlib import Path\ntime.sleep(0.15)\nPath('finished').write_text('completed')\n`);chmodSync(launcher,0o755)
 const accepted=await runHeadlessTurn({dshBin:launcher,sessionId:'fixture',cwd:home,message:'local fixture'})
 assert.equal(accepted.code,'started');assert.equal(existsSync(done),false)
 for(let i=0;i<100&&!existsSync(done);i++)await new Promise(resolve=>setTimeout(resolve,20))
 assert.equal(readFileSync(done,'utf8'),'completed')
} finally {rmSync(home,{recursive:true,force:true})}
console.log('Original plan, Goal facts, idle continuation and child lifetime verified.')
