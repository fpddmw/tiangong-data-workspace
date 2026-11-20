// Installed native Goal service and round driver, isolated local provider only.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'
import {randomUUID} from 'node:crypto'
import {goalSettlement,rearmActiveGoal} from '../bin/dsh_goal.mjs'

async function run(ctx,config){
 await ctx.get('loader')?.await()
 const require=createRequire(config.moduleEntry)
 const native=n=>import(pathToFileURL(require.resolve('@deepseek-ai/'+n)))
 const {LlmAdapter,createUserMessage}=await native('dsh-llm')
 const {installModelSelection}=await native('dsh-agent')
 let requests=0
 class Local extends LlmAdapter{
  async *stream(){
   requests++;const text='Original isolated native lifecycle response.'
   yield{type:'block-start',index:0,blockType:'text'};yield{type:'text-delta',index:0,text}
   yield{type:'block-end',index:0,block:{type:'text',text}};yield{type:'usage',usage:{inputTokens:1,outputTokens:1}}
   yield{type:'finish',reason:{kind:'stop'}}
  }
 }
 ctx.llm.registerAdapter(['htam-local-goal-service'],new Local())
 const {agent}=await ctx.agents.create({sessionId:'session-'+randomUUID(),meta:{cwd:process.cwd()},
  agentOptions:{provider:'htam-local-goal-service',model:'local-only'},setup:c=>{installModelSelection(c)}})
 const phases=[];const ref=g=>({id:g.id,revision:g.revision})
 const created=ctx.goals.create(agent,{objective:'Original local lifecycle service qualification',maxGoalRounds:1})
 const settled=goalSettlement(ctx,agent)
 try{
  agent.followup(createUserMessage({content:[{type:'text',text:'Run the original local qualification and leave the Goal active.'}],source:{kind:'user'}}))
  await settled.wait()
  const exhausted=ctx.goals.get(agent);phases.push(exhausted)
  assert.equal(exhausted.phase,'blocked');assert.equal(exhausted.roundsStarted,1)
  assert.throws(()=>ctx.goals.resume(agent,ref(exhausted)),/exhausted/)
  const edited=ctx.goals.edit(agent,ref(exhausted),{maxGoalRounds:4})
  assert.equal(edited.phase,'blocked');phases.push(edited)
  const resumed=ctx.goals.resume(agent,ref(edited));assert.equal(resumed.activation,'armed');phases.push(resumed)
  const paused=ctx.goals.pause(agent,ref(resumed));phases.push(paused)
  assert.equal(paused.phase,'paused');assert.equal(rearmActiveGoal(ctx.goals,agent,true).phase,'paused')
  const recovered=ctx.goals.resume(agent,ref(paused));phases.push(recovered)
  const complete=ctx.goals.complete(agent,ref(recovered));phases.push(complete)
  assert.equal(complete.phase,'complete');assert.throws(()=>ctx.goals.resume(agent,ref(complete)))
  const next=ctx.goals.create(agent,{objective:'Explicit new local qualification objective',maxGoalRounds:2})
  assert.notEqual(next.id,created.id);phases.push(next)
  const finished=ctx.goals.complete(agent,ref(next));phases.push(finished)
  await agent.whenIdle();await ctx.sessions.flush(agent.session)
  assert.ok(requests>=2,'Installed driver must execute an automatic continuation')
  console.log(JSON.stringify({type:'qualification',result:'passed',session_id:agent.session.id,local_native_requests:requests,phases,
    native_events:agent.session.snapshotEvents().filter(e=>e.type.startsWith('goal/')).map(e=>({type:e.type,data:e.data}))}))
  ctx.appExit(0)
 }finally{settled.dispose()}
}
export function apply(ctx,config){run(ctx,config).catch(e=>{console.log(JSON.stringify({type:'qualification',result:'failed',error:e.stack}));ctx.appExit(1)})}
