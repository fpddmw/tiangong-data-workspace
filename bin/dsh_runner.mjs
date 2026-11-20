// One invocation of one native Agent. DSH owns Goal rounds and persistence.
import {createRequire} from 'node:module'
import {realpathSync} from 'node:fs'
import {pathToFileURL} from 'node:url'
import {createHash, randomUUID} from 'node:crypto'
import {goalSettlement, rearmActiveGoal} from './dsh_goal.mjs'
import {installRequestTiming} from './dsh_sse_diagnostics.mjs'

const emit = value => process.stdout.write(JSON.stringify(value) + '\n')
const textOf = message => (message?.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('')

// Observation only: the Session remains the full original, including binary
// attachments. Neither a projection nor its absence decides business completion.
export function invocationObservation(ctx, agent, output = emit, streamUsage = () => undefined) {
  let turn = 0, step = 0, text = '', reason, usage, complete = true
  const calls = new Set()
  const off = ctx.on('session/event', (session, event) => {
    if (session !== agent.session) return
    const d = event.data
    switch (event.type) {
      case 'turn/start': turn = d.turn; step = 0; reason = undefined; text = ''; break
      case 'turn/end': reason = d.reason; break
      case 'step/start': step = d.step; break
      case 'request/header': output({type:'htam_request_header',turn}); return
      case 'llm/retry':
      case 'llm/retry-started':
        output({type:'htam_request_retry', phase:event.type === 'llm/retry' ? 'scheduled' : 'started',
          turn:d.turn, step:d.step, retry:d.retry, at_ms:event.time, delay_ms:d.delayMs})
        return
      case 'assistant/message': {
        const value = textOf(d.message)
        if (value) { text = value; output({type:'text', text:value}) }
        // Usage comes from the native attempt, including native retries.
      }
      // fall through
      case 'assistant/attempt': {
        output({type:'htam_response_attempt',turn})
        let value
        try { value = d.usage ?? streamUsage(d.stream) } catch { /* optional metering */ }
        if (!value) complete = false
        else if (!usage) usage = {...value}
        else for (const k of Object.keys(usage)) {
          if (typeof value[k] === 'number') usage[k] += value[k]
          else delete usage[k]
        }
        return
      }
      case 'tool/call': calls.add(d.callId); output({type:'tool_call', callId:d.callId, tool:d.name}); return
      case 'tool/result':
        if (event.surfaceOp === 'append') output({type:'tool_result', callId:d.message.toolCallId,
          status:d.message.isError ? 'error' : 'completed', ...(d.error ? {error:d.error} : {})})
        return
    }
    if (['turn/start','turn/end','step/start','step/end'].includes(event.type)) {
      output({type:'status', phase:event.type.replace('/', '_'), ...d,
        ...(event.type === 'step/end' && complete && usage ? {usage} : {})})
      if (event.type === 'step/end') { usage = undefined; complete = true }
    }
  })
  const tools = ctx.on('tools/result', ({name,callId}, result) => {
    if (!calls.has(callId)) return
    const value = result?.value
    if (name === 'bash' && value?.kind === 'foreground' && Number.isInteger(value.exitCode)) {
      output({type:'htam_shell_result', callId, exitCode:value.exitCode})
    }
    if (['write','edit'].includes(name) && typeof value?.path === 'string' && typeof value.after === 'string'
        && (value.before === null || typeof value.before === 'string') && value.before !== value.after) {
      output({type:'htam_file_effect', callId, path:value.path,
        before_sha256:value.before === null ? null : createHash('sha256').update(value.before).digest('hex'),
        sha256:createHash('sha256').update(value.after).digest('hex')})
    }
  })
  output({type:'session',sessionId:agent.session.id,cwd:agent.session.header.cwd})
  return {get:() => ({turn,text,reason}), position:() => ({turn,step}), dispose() {off();tools()} }
}

async function run(ctx, config, state) {
  await ctx.get('loader')?.await()
  // Observe settled startup even if a dependency failed to activate. Requiring
  // injection here would prevent this runner from reporting that very failure.
  const names = ['headlessStartup', 'agentDefaultModel', 'agents', 'sessions', 'goals', 'permissionPresets', 'sessionQuery', 'sandboxPolicy', 'tools']
  const services = Object.fromEntries(names.map(name => [name, ctx.get(name)]))
  const missing = names.filter(name => !services[name])
  if (missing.length) throw Object.assign(new Error('Native startup services unavailable: ' + missing.join(', ')), {code:'DSH_STARTUP_UNAVAILABLE'})
  const {headlessStartup:startup, agentDefaultModel, agents, sessions, goals, permissionPresets, sessionQuery} = services
  const require = createRequire(realpathSync(config.moduleEntry))
  const get = name => import(pathToFileURL(require.resolve('@deepseek-ai/' + name)))
  const [{createUserMessage,lastAssistantStreamChunk},{installModelSelection},{setApprovalPolicy}] = await Promise.all([
    get('dsh-llm'),get('dsh-agent'),get('dsh-user-approval')])
  let task = startup.task
  if (task === undefined || task === '-') { const chunks=[];for await (const c of process.stdin) chunks.push(c);task=Buffer.concat(chunks).toString('utf8') }
  if (!task?.trim()) throw new Error('A task is required.')
  const selection = agentDefaultModel.currentSelection()
  const setup = (agentCtx, agent) => {agent.model.select(selection);installModelSelection(agentCtx)}
  let handle
  if (startup.sessionId) {
    // Inspect only: never turn a missing identity into a replacement Session.
    const original = await sessionQuery.observeSession(startup.sessionId)
    try {
      if (original.header.cwd !== process.cwd()) throw new Error('Original Session working directory differs.')
      if (original.header.origin === 'subagent' || original.header.agentPreset ||
          original.events.some(e => e.type === 'agent-preset/selected'))
        throw new Error('The original Session requires its own composition.')
      if (agents.get(startup.sessionId)) throw new Error('Original Session is already live.')
      state.creating = true
      handle = await agents.resume({resumeSessionId:startup.sessionId,agentOptions:selection,setup})
    } finally {original[Symbol.dispose]()}
  } else {
    // Agent creation may acquire durable native identity. Beyond this point a
    // failed setup is not reported as an unobserved, safely repeatable launch.
    state.creating = true
    handle = await agents.create({sessionId:'session-'+randomUUID(),meta:{cwd:process.cwd()},agentOptions:selection,setup})
  }
  const {agent} = handle
  const mode = startup.permissionMode ?? config.mode
  if (mode) permissionPresets.set(agent.session, mode)
  if (startup.approvalPolicy) setApprovalPolicy(agent.session, startup.approvalPolicy)
  const observation = invocationObservation(ctx, agent, emit, stream => lastAssistantStreamChunk(stream, 'usage')?.usage)
  const stopTiming = await installRequestTiming(ctx, config.moduleEntry,
    {provider:selection.provider, sessionId:agent.session.id, position:observation.position, output:emit})
  const settled = goalSettlement(ctx, agent)
  let failure
  const stopError = ctx.on('agent/error', ({agent:subject,error}) => {if (subject === agent) failure=error})
  const stopTerminal = ctx.on('session/event', (session,event) => {
    if (session === agent.session && event.type === 'turn/end') failure=undefined
  })
  try {
    state.submitted = true
    if (startup.sessionId) {
      rearmActiveGoal(goals, agent, config.technicalContinue === true)
    }
    agent.followup(createUserMessage({content:[{type:'text',text:task}],source:{kind:'user'}}))
    await settled.wait()
    await sessions.flush(agent.session)
    const result = observation.get()
    // Native errors with no persisted terminal remain failures, never success.
    if (failure) throw failure
    try { emit({type:'htam_goal_state',sessionId:agent.session.id,turn:result.turn,
      source:'dsh_live_agent',goal:goals.get(agent) ?? null}) }
    catch { emit({type:'observation_issue',capability:'native_goal'}) }
    emit({type:'final',sessionId:agent.session.id,reason:result.reason ?? null,text:result.text})
    ctx.appExit(result.reason?.kind === 'completed' ? 0 : 1)
  } finally {stopTiming();stopError();stopTerminal();settled.dispose();observation.dispose()}
}

export function apply(ctx, config) {
  const state = {creating:false,submitted:false}
  run(ctx,config,state).catch(error => {
    emit({type:'error',message:error.message,code:error.code,
      ...(!state.creating && !state.submitted ? {native_status:'not_started'} : {})})
    ctx.appExit(1)
  })
}
