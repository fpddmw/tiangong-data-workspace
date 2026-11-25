// Native goal lifecycle integration. Business scope remains in the original brief.
import { Socket } from 'node:net'
import { createInterface } from 'node:readline'
import { safeValue } from './safe_diagnostics.mjs'
export const GOAL_CONTEXT_BUDGET_BYTES=2048
let originalControl

export function bindOriginalGoalControl(channel) {
  originalControl = channel
}

function budgetError(code, message) {
  return Object.assign(new Error(message), {code})
}

export async function authorizeGoalBudget(budget) {
  if (!originalControl || !await originalControl.check('goal_budget', JSON.stringify(budget), originalControl.signal)) {
    throw budgetError('DSH_GOAL_BUDGET_WITHHELD', 'The original host withheld the explicit Goal budget.')
  }
}

export function editAndResumeGoal(goals, agent, budget, emit) {
  const before = goals.get(agent)
  if (agent.session.id !== budget.session_id || before?.id !== budget.id || before.revision !== budget.revision ||
      before.phase !== 'blocked' || before.activation !== 'disarmed' || before.blockedReason?.code !== 'round-limit' ||
      before.maxGoalRounds !== budget.previous_max || before.roundsStarted !== budget.previous_max ||
      !Number.isSafeInteger(budget.max_rounds) || budget.max_rounds <= budget.previous_max) {
    throw budgetError('DSH_GOAL_BUDGET_STALE', 'The live Goal differs from the exact exhausted Goal in the original decision.')
  }
  const observe = (stage, current) => emit({type:'htam_goal_budget',stage,sessionId:agent.session.id,
    goal:{id:current.id,revision:current.revision,phase:current.phase,activation:current.activation,
      roundsStarted:current.roundsStarted,maxGoalRounds:current.maxGoalRounds}})
  const edited = goals.edit(agent,{id:before.id,revision:before.revision},{maxGoalRounds:budget.max_rounds})
  const current = goals.get(agent)
  if (current?.id !== before.id || current.revision !== edited.revision || current.phase !== 'blocked' ||
      current.maxGoalRounds !== budget.max_rounds || current.roundsStarted !== before.roundsStarted ||
      current.objective !== before.objective) {
    throw budgetError('DSH_GOAL_BUDGET_READBACK', 'The native Goal edit did not read back as authorized.')
  }
  observe('edited', current)
  const resumed = goals.resume(agent, {id:current.id,revision:current.revision})
  const active = goals.get(agent)
  if (active?.id !== current.id || active.revision !== resumed.revision || active.phase !== 'active' ||
      active.activation !== 'armed' || active.maxGoalRounds !== budget.max_rounds) {
    throw budgetError('DSH_GOAL_RESUME_READBACK', 'The edited native Goal did not read back as active and armed.')
  }
  observe('resumed', active)
}

// Native CAS activation on an already-authorized technical continuation. Never
// change objective, budget, stopped/blocked/completed phase or native identity.
export function rearmActiveGoal(goals, agent, authorized) {
  if (!authorized) return null
  const goal = goals.get(agent)
  if (goal?.phase !== 'active' || goal.activation !== 'disarmed') return goal
  return goals.resume(agent, {id:goal.id, revision:goal.revision})
}

export function controlChannel(fd, emit = event => console.log(JSON.stringify(event))) {
  if (!Number.isInteger(fd)) return null
  const socket = new Socket({fd, readable:true, writable:true})
  const lines = createInterface({input:socket})
  const connection = new AbortController()
  const peer = new AbortController()
  const pending = new Map(); let serial = 0, closed = false
  const fail = () => {
    closed = true
    connection.abort()
    for (const p of pending.values()) p.reject(new Error('Original dispatcher control channel closed'))
    pending.clear()
  }
  const disconnected = () => {
    if (closed) return
    fail()
    peer.abort()
  }
  socket.on('error', disconnected); socket.on('close', disconnected); socket.on('end', disconnected)
  lines.on('line', line => {
    let value; try { value = JSON.parse(line) } catch { return }
    const p = pending.get(value.id); if (!p) return
    pending.delete(value.id); p.resolve(value)
  })
  return {
    signal: connection.signal,
    disconnected: peer.signal,
    async request(kind, text, signal) {
      if (closed) throw new Error('Original dispatcher control channel closed')
      if (signal?.aborted) throw new Error('Original native step cancelled')
      const id = ++serial
      let abort
      const answer = new Promise((resolve,reject) => {
        pending.set(id,{resolve,reject})
        abort = () => {
          if (pending.delete(id) && !closed) socket.write(JSON.stringify({id,cancelled:true})+'\n')
          reject(new Error('Original native step cancelled'))
        }
        signal?.addEventListener('abort',abort,{once:true})
        emit({type:'htam_control',id,kind,...(text === undefined ? {} : {text})})
      })
      try { return await answer } finally { signal?.removeEventListener('abort',abort) }
    },
    async check(kind, text, signal) { return (await this.request(kind,text,signal)).accepted === true },
    close() { fail(); lines.close(); socket.destroy() },
  }
}

// The common dynamic context reads the live installed service on the first
// resumed turn and after compaction. It never changes phase, scope or budget.
export function goalRecoveryContext(goal) {
  if (!goal) return 'Native Goal: none. For an explicitly assigned continuing objective, create_goal establishes its native rounds.'
  const values={id:goal.id,revision:goal.revision,phase:goal.phase,activation:goal.activation,
    roundsStarted:goal.roundsStarted,maxGoalRounds:goal.maxGoalRounds,blockedReason:goal.blockedReason}
  const steps=
    'On this continuation, first get_goal and read the complete owner instruction at the original request/continuation entry. '+
    'Use its goal_id and revision. For an exhausted cap with explicit extra investment, update_goal action edit max_goal_rounds '+
    '(used rounds + explicitly added rounds, or the explicit total cap), then action resume with the returned revision; edit preserves phase. '+
    'If the owner requests a correction to this same candidate after Goal completion, create_goal for that correction. Separate deliverables use their own author requests and Sessions. '+
    'An active disarmed Goal can resume for authorized continuation. A paused Goal requires explicit owner recovery. '+
    'Read actual tool results, then continue making, checking, requesting independent review and correcting the work. '+
    'Native Goal rounds perform automatic continuation; do not replace them with an external prompt loop or invent extra investment.'
  const render=v=>'Native Goal (live service): '+JSON.stringify(safeValue(v))+'\n'+steps
  if(Buffer.byteLength(render(values))<=GOAL_CONTEXT_BUDGET_BYTES)return render(values)
  const {blockedReason,...state}=values
  const navigation=render({...state,blockedReason:{expanded:false,read:'get_goal returns the complete original blocked reason.'}})
  if(Buffer.byteLength(navigation)<=GOAL_CONTEXT_BUDGET_BYTES)return navigation
  return 'Native Goal facts not expanded; get_goal returns its complete identity, phase, activation, used/cap rounds and blocked reason.\n'+steps
}

// Observe the native driver, without replacing Agent methods or scheduling work.
// Subscribe before testing state so a fast transition cannot lose a wakeup.
export function goalSettlement(ctx, agent) {
  const goals = ctx.get('goals')
  let next = Promise.withResolvers(), disposed = false
  const wake = () => { const previous = next; next = Promise.withResolvers(); previous.resolve() }
  const observe = ({agent: subject}) => { if (subject === agent) wake() }
  const off = ['agent/status', 'goal/changed', 'goal/activation-changed', 'agent/disposed']
    .map(name => ctx.on(name, observe))
  return {
    async wait() {
      while (!disposed) {
        const changed = next.promise
        await agent.whenIdle()
        const goal = goals.get(agent)
        if (!goal || goal.phase !== 'active' || goal.activation !== 'armed') return
        await changed
      }
    },
    dispose() { disposed = true; for (const stop of off) stop(); wake() },
  }
}
