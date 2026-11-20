// Native goal lifecycle integration. Business scope remains in the original brief.
import { Socket } from 'node:net'
import { createInterface } from 'node:readline'
import { safeValue } from './safe_diagnostics.mjs'
export const GOAL_CONTEXT_BUDGET_BYTES=2048

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
  const pending = new Map(); let serial = 0, closed = false
  const fail = () => { closed = true; for (const p of pending.values()) p.reject(new Error('Original dispatcher control channel closed')); pending.clear() }
  socket.on('error', fail); socket.on('close', fail); socket.on('end', fail)
  lines.on('line', line => {
    let value; try { value = JSON.parse(line) } catch { return }
    const p = pending.get(value.id); if (!p) return
    pending.delete(value.id); p.resolve(value)
  })
  return {
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
    'For a complete Goal and explicitly assigned new work, create_goal for that complete new objective in this same Session; complete cannot resume. '+
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
