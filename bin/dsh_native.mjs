// Native Cordis tool policy and exact-session query for DSH 0.2.2-rc.1.
import { createRequire } from 'node:module'
import { realpathSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

import { bindOriginalGoalControl, controlChannel } from './dsh_goal.mjs'
import { safeError, safeText } from './safe_diagnostics.mjs'
import { installSseDiagnostics } from './dsh_sse_diagnostics.mjs'
import { installIdentityRefresh, installReviewRequest, installReviewResult, installLocalCheck, installFindingReport, installTiangongCli } from './dsh/delivery.mjs'

export function limitTimeoutRetries(ctx, {provider, recentTimeoutUntilMs = 0, automaticProbe = false}, now = Date.now) {
  const attempts = new WeakMap()
  return ctx.on('agent/request-error', (request, next) => {
    const {agent, turn, step, provider: selected, failure} = request
    if (selected !== provider || !agent?.session) return next()
    if (!['TIMEOUT', 'LLM_STREAM_IDLE_TIMEOUT'].includes(failure?.code)) {
      attempts.delete(agent.session)
      return next()
    }
    let current = attempts.get(agent.session)
    if (current?.turn !== turn || current?.step !== step) {
      // Keep the original recovery path available after an isolated stall.
      // A recent terminal timeout of this same continuation is stronger evidence.
      current = {turn, step, failures:0, recent:now() < recentTimeoutUntilMs}
      current.limit = automaticProbe || current.recent ? 2 : 5
      attempts.set(agent.session, current)
    }
    if (++current.failures < current.limit) return next()
    process.stdout.write(JSON.stringify({type:'htam_timeout_limit', turn, step, failures:current.failures,
      previous_terminal_timeout:current.recent}) + '\n')
    // Returning without a recovery decision leaves the original native error intact.
  }, {prepend:true})
}

function installModelAdmission(ctx, channel, activityIntervalMs, disconnect) {
  let turn, sessionId
  ctx.on('session/event', (session, event) => {
    if (event.type === 'turn/start') {
      sessionId = session.id
      turn = event.data.turn
    }
  })
  channel.disconnected.addEventListener('abort', () => {
    // The installed runtime freezes GenerateOptions. Close this exact native
    // transport to end any request already in progress when its owner dies.
    if (disconnect && sessionId && Number.isInteger(turn)) {
      const {path, ...binding} = disconnect
      try {writeFileSync(path, JSON.stringify({...binding, sessionId, turn, pid:process.pid})+'\n', {flag:'wx',mode:0o600})}
      catch { /* Missing proof leaves the original native abort unresolved for recovery. */ }
    }
    setTimeout(() => process.exit(2), 2000).unref()
    ctx.appExit(2)
  }, {once:true})
  return ctx.on('llm/stream', async function* (options, next) {
    const signal = options.signal ? AbortSignal.any([options.signal, channel.signal]) : channel.signal
    signal.throwIfAborted()
    const admitted = await channel.request('model_enter', undefined, signal)
    if (!admitted.accepted) throw new Error('Original host withheld the model request')
    try {
      signal.throwIfAborted()
      let lastActivity = -Infinity
      for await (const chunk of next()) {
        const now = performance.now()
        if (now - lastActivity >= activityIntervalMs && (chunk.type === 'text-delta' || chunk.type === 'reasoning-delta' ||
            chunk.type === 'tool-call-delta' || chunk.type === 'block-end')) {
          lastActivity = now
          process.stdout.write(JSON.stringify({type:'htam_model_activity', permit:admitted.permit}) + '\n')
        }
        yield chunk
      }
    } finally {
      process.stdout.write(JSON.stringify({type:'htam_model_closed', permit:admitted.permit}) + '\n')
    }
  }, {prepend:true})
}

export function apply(ctx, config) {
  const { allow } = config
  const channel = controlChannel(config.controlFd)
  bindOriginalGoalControl(channel)
  if (channel) ctx.effect(() => () => channel.close())
  if (channel && config.modelCapacity) installModelAdmission(ctx, channel, config.modelActivityIntervalMs, config.controlDisconnect)
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.tools.restrict({ allow })
  })
  if (config.timeoutRetries) limitTimeoutRetries(ctx, config.timeoutRetries)
  if (channel && config.moduleEntry) {
    return Promise.all([
      ...(config.sseDiagnostics ? [installSseDiagnostics(ctx, config.moduleEntry, config.sseDiagnostics)] : []),
      ...(allow.includes('tiangong_cli') ? [installTiangongCli(ctx, config.moduleEntry, channel, config.tiangongCliTool)] : []),
      ...(allow.includes('local_check') ? [installLocalCheck(ctx, config.moduleEntry, channel)] : []),
      ...(allow.includes('request_review') ? [installReviewRequest(ctx, config.moduleEntry, channel, config.reviewRequestTool)] : []),
      ...(allow.includes('review_result') ? [installReviewResult(ctx, config.moduleEntry, channel)] : []),
      ...(allow.includes('report_finding') ? [installFindingReport(ctx, config.moduleEntry, channel)] : []),
      ...(allow.includes('refresh_editor_identity') ? [installIdentityRefresh(ctx, config.moduleEntry, channel)] : []),
    ])
  }
}

export function turnObservation(events, turn) {
  let header, currentTurn, latestTurn, latestTerminal, terminal, nativeError, text = ''
  const routes = [], requests = [], responses = [], toolCalls = []
  const selectedCalls = new Map()
  const pendingTools = new Map()
  let routeMismatch = false
  let failedResponseAttempts = 0
  for (const {type, data} of events) {
    if(type==='tool/call')pendingTools.set(data.callId,{call_id:data.callId,name:data.name,turn:data.turn})
    if(type==='tool/result')pendingTools.delete(data.message?.toolCallId)
    if (type === 'request/header') header = data.header.config
    if (type === 'turn/start') { currentTurn = data.turn; latestTurn = data.turn; latestTerminal = undefined }
    if (type === 'turn/end' && data.turn === latestTurn) latestTerminal = data.reason.kind
    if (currentTurn !== turn) continue
    if (type === 'request/header') {
      requests.push({provider:header?.provider, model:header?.model,
        tools:(data.header.tools ?? []).map(tool => typeof tool === 'string' ? tool : tool.name).filter(Boolean)})
    }
    if (type === 'tool/call') {
      const call = {call_id:data.callId, name:data.name}
      toolCalls.push(call)
      selectedCalls.set(data.callId, call)
    }
    if (type === 'tool/result') {
      const call = selectedCalls.get(data.message?.toolCallId)
      if (call) call.result = data.message.isError ? 'error' : 'returned'
    }
    if (type === 'assistant/attempt' && data.stream?.at(-1)?.chunk?.reason?.kind === 'error') {
      failedResponseAttempts++
    }
    if (type === 'assistant/message') {
      const response = data.message.source?.replayState?.response
      responses.push({blocks:(data.message.content ?? []).map(block => block.type),
        stop_reason:response?.stopReason ?? data.message.stopReason,
        ...(typeof response?.responseId === 'string' ? {response_id:safeText(response.responseId)} : {})})
      // Same lossless text projection as the installed headless summarize().
      const joined = (data.message.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('')
      if (joined !== '') text = joined
      const {provider, model} = data.message.source ?? {}
      if (provider !== header?.provider || model !== header?.model) routeMismatch = true
      if (provider && model) routes.push({provider, model, reasoningEffort:header?.reasoningEffort})
    }
    if (type === 'turn/end') {
      terminal = data.reason.kind
      if (terminal === 'error') {
        const error = data.reason.error
        nativeError = safeError(error)
      } else nativeError = undefined
    }
  }
  const route = routes[0]
  return {
    source:'dsh_persisted_session_log', persisted_event_count:events.length,
    terminal, latest_turn:latestTurn, latest_terminal:latestTerminal,
    unreturned_tool_calls:[...pendingTools.values()], requests, responses, tool_calls:toolCalls,
    ...(failedResponseAttempts ? {failed_response_attempts:failedResponseAttempts} : {}),
    ...(nativeError ? {native_error:nativeError} : {}),
    ...(terminal === 'completed' ? {final_text:text} : {}),
    ...(routeMismatch ? {route_observation_issue:'Native route differs from its header.'} : {}),
    ...(!routeMismatch && route && routes.every(r => JSON.stringify(r) === JSON.stringify(route)) ? {route} : {}),
  }
}

export async function readPersistedTurn(persistence, session, turn, loadGoal) {
  // SessionQuery cold reads synthesize interrupted closers for an open turn.
  // Only events actually persisted by the original writer establish a terminal.
  const handle = await persistence.open(session, 'read')
  try {
    if (handle.header.id !== session) return {}
    const { events } = await handle.read(0)
    const observed = { ...turnObservation(events, Number(turn)), session_id: handle.header.id, cwd: handle.header.cwd }
    if (loadGoal) {
      try {
        const foldGoal = await loadGoal()
        const state = foldGoal(events)
        observed.native_goal = state.goal ? {...state.goal, roundsStarted:state.roundsStarted} : null
        // Activation belongs to the live instance; persistence cannot attest it.
      }
      catch { observed.goal_observation_issue = 'Original native Goal projection unavailable; do not infer its phase.' }
    } else observed.goal_observation_issue = 'Native Goal projection was not requested.'
    return observed
  } finally {
    await handle.close()
  }
}

export async function query(binary, root, session, turn) {
  const require = createRequire(realpathSync(binary))
  const get = name => import(pathToFileURL(require.resolve('@deepseek-ai/' + name)))
  const { Context } = await get('cordis')
  const { default: SessionStore } = await get('dsh-session')
  const persistence = await get('dsh-session-persistence-jsonl')
  const ctx = new Context()
  const sessions = ctx.plugin(SessionStore)
  const storage = ctx.plugin(persistence, { root })
  try {
    await Promise.all([sessions.await(), storage.await()])
    const observed = await readPersistedTurn(ctx.sessionPersistence, session, turn, async () => (await get('dsh-goal')).foldGoal)
    // Resolve through the installed persistence provider, including its encoding
    // and generation rules. This is a read; never open a write handle to probe.
    return {...observed, artifact_path: await ctx.sessionPersistence.resolveCurrentLog(session)}
  } finally {
    await storage.dispose()
    await sessions.dispose()
  }
}

if (process.argv[2] === '--query' && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    console.log(JSON.stringify(await query(...process.argv.slice(3))))
  } catch (error) {
    console.log(JSON.stringify({read_observation_issue: error.code ?? error.name ?? 'NativeReadFailed'}))
    process.exitCode = 1
  }
}
