// One formal dynamic context contribution. Native DSH owns projection/dedup.
import {execFile} from 'node:child_process'
import {promisify} from 'node:util'
import {join} from 'node:path'
import {goalRecoveryContext,GOAL_CONTEXT_BUDGET_BYTES} from '../dsh_goal.mjs'
const execute=promisify(execFile)

export function apply(ctx,config) {
  const cache=new WeakMap(),errors=new WeakMap()
  const entry=agent=>{
    let row=cache.get(agent)
    if(!row){row={text:'',snapshotHash:null,refresh:null,dirty:true};cache.set(agent,row)}
    return row
  }
  const refresh=agent=>{
    const row=entry(agent)
    if(row.refresh)return row.refresh
    row.refresh=(async()=>{
      try {
        const {stdout}=await execute(config.python,[config.snapshotEntry??join(config.workspaceRoot,'bin/harness_context_hook.py'),'--root',config.workspaceRoot,
          '--dsh','--request',config.requestId,'--run-id',config.runId,'--session-id',agent.session.id,
          '--inline-budget',String(8192-GOAL_CONTEXT_BUDGET_BYTES-2)],
          {timeout:1000,maxBuffer:64*1024,windowsHide:true})
        const result=JSON.parse(stdout)
        if(typeof result.text!=='string')throw Object.assign(new Error('Local snapshot unavailable'),{code:'CONTEXT_READ_INCOMPLETE'})
        if(Buffer.byteLength(result.text)>8192)throw Object.assign(new Error('Local context budget exceeded'),{code:'CONTEXT_BUDGET_EXCEEDED'})
        row.text=result.text;row.snapshotHash=result.snapshot_hash;row.dirty=false
      } catch(error) {
        row.text='';row.dirty=true
        const code=error.code??error.name??'ContextReadFailed'
        if(errors.get(agent)!==code){errors.set(agent,code);ctx.logger?.warn?.('Task context unavailable: '+String(code))}
      }
    })().finally(()=>{row.refresh=null})
    return row.refresh
  }
  const contextText=agent=>{
    const original=entry(agent).text,note=goalRecoveryContext(ctx.get?.('goals')?.get(agent))
    const combined=original+'\n\n'+note
    return Buffer.byteLength(combined)<=8192?combined:
      'Task context not expanded; read the complete original request '+config.requestId+' with python3 bin/context.py --request '+config.requestId+'.\n'+note
  }
  ctx.systemPrompt.context({name:'htam:task',order:220,interpolate:false,
    text:context=>context.agent?contextText(context.agent):''})
  ctx.on('agent/created',({agent})=>{void refresh(agent)})
  ctx.on('session/event',(session,event)=>{
    if(event.type==='compaction/end'){
      const agent=ctx.agents.get(session.id)
      if(agent)entry(agent).dirty=true
    }
  })
  ctx.on('system-prompt/assemble',async(_assembly,context,next)=>{
    if(!context.agent)return next()
    await refresh(context.agent)
    const assembly=await next()
    const text=contextText(context.agent)
    // Only our own complete context is replaced. Never append a user turn,
    // call followup, or rewrite another contributor's context.
    const own=assembly.contexts.find(c=>c.name==='htam:task')
    if(own){own.text=text;own.interpolate=false}
    else assembly.contexts.push({name:'htam:task',text,interpolate:false})
    return assembly
  })
}
