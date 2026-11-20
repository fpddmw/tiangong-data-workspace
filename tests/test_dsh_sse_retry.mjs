// Offline integration with the installed DSH retry hook and optional original B02 log.
import assert from 'node:assert/strict'
import {createHash} from 'node:crypto'
import {readFileSync} from 'node:fs'
import {createRequire} from 'node:module'
import {join} from 'node:path'
import {pathToFileURL} from 'node:url'
import {apply as profile, limitTimeoutRetries} from '../bin/dsh_native.mjs'

const require = createRequire(process.argv[2])
const installed = name => import(pathToFileURL(require.resolve('@deepseek-ai/' + name)))
const retry = await installed('dsh-llm-retry')

async function fixture() {
  const handlers = [], events = []
  let projection, state = {}
  const ctx = {
    fiber: {uid: 'offline'},
    sessionProjections: {async register(p) {projection = p; return {stateOf() {return state}}}},
    on(name, fn, options) {
      if (name === 'agent/request-error') options?.prepend ? handlers.unshift(fn) : handlers.push(fn)
      return () => {}
    },
    effect() {},
  }
  await retry.apply(ctx)
  const session = {append(type, data) {
    const event = {type, data}
    events.push(event)
    state = projection.apply(state, event)
  }}
  function dispatch(payload) {
    const request = {agent:{session}, signal:new AbortController().signal, ...payload}
    const call = i => i < handlers.length ? handlers[i](request, () => call(i+1)) : undefined
    return call(0)
  }
  return {ctx, session, dispatch, events}
}

const baseline = await fixture()
profile(baseline.ctx, {allow: []})
const originalPolicy = Object.freeze({mode:'normal', maxRetries:5, retryableCodes:['TRANSPORT'],
  initialDelayMs:0, maxDelayMs:0, jitterRatio:0})
assert.equal(await baseline.dispatch({turn:1, step:2, provider:'fixture', retryPolicy:originalPolicy,
  failure:{code:'PI_AI_ERROR', message:'Error reading response: malformed server-sent event JSON.'}}), undefined)
assert.equal(baseline.events.filter(e => e.type === 'llm/retry').length, 0)
assert.deepEqual(originalPolicy.retryableCodes, ['TRANSPORT'])
console.log('Native malformed-SSE retry classification is unchanged.')

function failureCode(event) {
  return event.data.stream.findLast(row => row.chunk?.reason?.failure?.code)?.chunk.reason.failure.code
}

function lastContent(event, start) {
  return Math.max(start, ...event.data.stream.filter(row => row.dt?.length ||
    /^(reasoning|text|tool-call)-chunks$/u.test(row.type)).map(row =>
    row.time0 + row.dt.reduce((elapsed, delta) => elapsed + delta, 0)))
}

async function replay(events, receipt, preceding) {
  const provider = receipt.result.native_session_observation.requests[0].provider
  const old = preceding?.result
  const ended = Date.parse(old?.turn_activity.native_terminal_at)
  const start = events.find(e => e.type === 'turn/start').time
  const recent = preceding?.request_id === receipt.request_id
    && preceding.native_identity.id === receipt.native_identity.id
    && old.native_session_observation.native_error.code === 'TIMEOUT'
    && old.native_session_observation.requests.every(request => request.provider === provider)
    && start >= ended && start - ended < 600_000
  const runtime = await fixture()
  let clock = start
  limitTimeoutRetries(runtime.ctx, {provider, recentTimeoutUntilMs:recent ? ended+600_000 : 0}, () => clock)
  const retries = events.filter(event => event.type === 'llm/retry')
  const [mode, maxRetries, retryableCodes] = JSON.parse(retries[0].data.policyKey)
  assert.ok(retries.every(event => event.data.policyKey === retries[0].data.policyKey))
  const policy = {mode, maxRetries, retryableCodes, initialDelayMs:0, maxDelayMs:0, jitterRatio:0}
  let stoppedAt, attemptStart, retryStart, failedBefore, failedAfter, preserved, recovered
  failedBefore = failedAfter = preserved = recovered = 0
  let attemptsMs = 0, idleMs = 0, backoffMs = 0, sawFailure = false
  for (const event of events) {
    clock = event.time
    const {type, data: {turn, step}} = event
    if (type === 'step/start') {
      assert.equal(stoppedAt, undefined, 'A terminal step unexpectedly succeeded under the original run')
      runtime.session.append(type, event.data)
      attemptStart = event.time
      sawFailure = false
    } else if (type === 'llm/retry') {
      retryStart = event.time
    } else if (type === 'llm/retry-started') {
      attemptStart = event.time
      if (stoppedAt !== undefined) backoffMs += event.time - retryStart
    } else if (type === 'assistant/message') {
      assert.equal(stoppedAt, undefined, 'A recovered step was cut off')
      preserved++
      if (sawFailure) recovered++
    } else if (type === 'assistant/attempt' && failureCode(event) === 'TIMEOUT') {
      sawFailure = true
      failedBefore++
      if (stoppedAt === undefined) {
        failedAfter++
        const action = await runtime.dispatch({turn, step, provider, retryPolicy:policy, failure:{code:'TIMEOUT'}})
        if (!action) stoppedAt = step
      } else {
        attemptsMs += event.time - attemptStart
        idleMs += event.time - lastContent(event, attemptStart)
      }
    }
  }
  assert.notEqual(stoppedAt, undefined)
  return {turn:Number(receipt.native_identity.turn_id), recent_timeout:Boolean(recent),
    failed_before:failedBefore, failed_after:failedAfter, native_retries_before:retries.length,
    native_retries_after:runtime.events.filter(event => event.type === 'llm/retry').length,
    success_preserved:preserved, success_after_retry_preserved:recovered, stopped_at_step:stoppedAt,
    avoided_attempt_ms:attemptsMs, avoided_idle_ms:idleMs, avoided_backoff_ms:backoffMs,
    elapsed_ms:events.findLast(e => e.type === 'turn/end').time - start,
    tool_names:events.filter(e => e.type === 'tool/call').map(e => e.data.name)}
}

if (process.argv.includes('--original-b02')) {
  const root = 'materials/packages/tiangong-package-gap-global-main-20261110/apple-scm-reuse-20261110/apple/material-dispatch-20261115'
  const refs = ['b02-resume-20261119-0411pt.json','b02-original-followthrough-0536pt.json',
    'b02-original-continuation-0632pt.json'].map(name => JSON.parse(readFileSync(join(root,name))).receipt)
  const receipts = refs.map(ref => {
    const raw = readFileSync(ref.path)
    assert.equal(createHash('sha256').update(raw).digest('hex'),ref.sha256)
    return JSON.parse(raw)
  })
  const [{Context}, {default:Store}, persistence] = await Promise.all(
    ['cordis','dsh-session','dsh-session-persistence-jsonl'].map(installed))
  const host = JSON.parse(readFileSync('installation/agent-autonomy-20261111/programmatic-worker.json')).hosts.dsh
  const ctx = new Context(), store = ctx.plugin(Store)
  const disk = ctx.plugin(persistence, {root:join(host.dsh_home,'sessions')})
  await Promise.all([store.await(), disk.await()])
  let handle
  try {
    handle = await ctx.sessionPersistence.open(receipts[0].native_identity.id,'read')
    const {events} = await handle.read(0)
    const turns = []
    for (const [index, receipt] of receipts.entries()) {
      let turn
      assert.equal(receipt.native_identity.id,receipts[0].native_identity.id)
      turns.push(await replay(events.filter(e => {
        if (e.type === 'turn/start') turn = e.data.turn
        return turn === Number(receipt.native_identity.turn_id)
      }), receipt, receipts[index-1]))
    }
    assert.deepEqual(turns.map(t => [t.failed_before,t.failed_after,t.success_preserved,t.recent_timeout]),
      [[23,22,17,false], [23,22,15,false], [6,2,1,true]])
    assert.deepEqual(turns.at(-1).tool_names,['get_goal','bash'])
    console.log(JSON.stringify({case:'B02', receipt_refs:refs, turns}))
  } finally {
    await handle?.close()
    await disk.dispose()
    await store.dispose()
  }
}
