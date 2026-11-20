// Test composition over real installed native services and real compaction.
// No private class patching, fake lifecycle events, remote models or production.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'
import {randomUUID} from 'node:crypto'
import {execFile} from 'node:child_process'
import {promisify} from 'node:util'
import {apply as taskContext} from '../bin/dsh_context.mjs'
const execute=promisify(execFile)
const emit=value=>console.log(JSON.stringify(value))

async function run(ctx,config) {
 await ctx.get('loader')?.await()
 const require=createRequire(config.moduleEntry),native=name=>import(pathToFileURL(require.resolve('@deepseek-ai/'+name)))
 const [{LlmAdapter,createUserMessage},{installModelSelection}]=await Promise.all([native('dsh-llm'),native('dsh-agent')])
 let summarizing=false;const requests=[]
 class Scripted extends LlmAdapter {
  async *stream(options) {
   assert.equal(options.provider,'htam-context-local')
   const texts=options.messages.flatMap(m=>(m.content??[]).filter(b=>b.type==='text').map(b=>b.text)).join('\n')
   if(!summarizing){
    const a=texts.includes('task=native-context-A'),b=texts.includes('task=native-context-B')
    assert.notEqual(a,b,'Exact original per-agent task context must be present without cross-session leakage')
    requests.push(a?'A':'B')
   }
   const text=summarizing?'Original isolated context check and retained decisions.':'Isolated native response. '+('preserved context material '.repeat(400))
   const block={type:'text',text}
   yield {type:'block-start',index:0,blockType:'text'};yield {type:'text-delta',index:0,text};yield {type:'block-end',index:0,block}
   yield {type:'usage',usage:{inputTokens:summarizing?1:1000,outputTokens:summarizing?1:1000}}
   yield {type:'finish',reason:{kind:'stop'}}
  }
 }
 ctx.llm.registerAdapter(['htam-context-local'],new Scripted())
 const handles=[]
 for(const name of ['A','B']) {
  const handle=await ctx.agents.create({sessionId:'session-'+randomUUID(),meta:{cwd:config.workspaceRoot},
   agentOptions:{provider:'htam-context-local',model:'isolated-context-model'},setup(agentCtx,agent){
    installModelSelection(agentCtx)
    agentCtx.plugin({name:'isolated-task-context-'+name,inject:['systemPrompt','agents'],apply:taskContext},{workspaceRoot:config.workspaceRoot,python:config.python,
     snapshotEntry:config.snapshotEntry,requestId:'request-'+name,runId:'run-'+name})
   }})
  handles.push(handle)
  await execute(config.python,[config.fixture,'bind',config.workspaceRoot,name,handle.agent.session.id],{timeout:5000})
 }
 const step=async(agent,text)=>{agent.followup(createUserMessage({content:[{type:'text',text}],source:{kind:'user'}}));await agent.whenIdle();await ctx.sessions.flush(agent.session)}
 const reminders=agent=>agent.session.snapshotEvents().filter(e=>e.type==='user/message'&&e.data.source?.sections?.some(s=>s.name==='htam:task'))
 await Promise.all(handles.map(({agent})=>step(agent,'Read the original isolated responsibility.')))
 await Promise.all(handles.map(({agent})=>step(agent,'Observe unchanged facts once more.')))
 for(const {agent} of handles)assert.equal(reminders(agent).length,1,'No duplicate retained context on unchanged native requests')
 const {agent}=handles[0];const prior=reminders(agent)[0]
 assert.ok(agent.session.surface.nodes.includes(prior.seq))
 summarizing=true
 let compacted
 try {compacted=await ctx.compaction.compactNow(agent,new AbortController().signal)} finally {summarizing=false}
 assert.ok(compacted,'Native compaction must replace a real retained surface span')
 assert.ok(!agent.session.surface.nodes.includes(prior.seq),'Original context must actually leave the native surface')
 assert.ok(agent.session.snapshotEvents().some(e=>e.seq===prior.seq),'Original bytes remain in the native log')
 await step(agent,'Continue after the actual native compaction.')
 assert.equal(reminders(agent).length,2,'Native runtime context projection must restore the complete unchanged card after compaction')
 assert.equal(reminders(handles[1].agent).length,1,'Other native Session context remains unchanged')
 emit({type:'qualification',result:'passed',native_requests:requests,context_messages:handles.map(h=>reminders(h.agent).length),
   actual_compaction:true,original_context_seq:prior.seq,remaining_surface_nodes:agent.session.surface.nodes.length})
 ctx.appExit(0)
}
export function apply(ctx,config){run(ctx,config).catch(error=>{emit({type:'qualification',result:'failed',error:error.stack??error.message});ctx.appExit(1)})}
