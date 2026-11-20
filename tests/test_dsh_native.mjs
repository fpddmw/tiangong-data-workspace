import assert from 'node:assert/strict'
import { readPersistedTurn } from '../bin/dsh_native.mjs'

const events = [
  { type: 'request/header', data: { header: { config: { provider: 'p', model: 'deepseek-test' } } } },
  { type: 'turn/start', data: { turn: 1 } },
  { type: 'assistant/message', data: { turn: 1, message: { source: { provider: 'p', model: 'deepseek-test' } } } },
]
let closed = 0
const persistence = { async open(id, mode) {
  assert.equal(mode, 'read')
  return { header: { id }, async read() { return { events } }, async close() { closed++ } }
} }
assert.equal((await readPersistedTurn(persistence, 's', 1)).terminal, undefined)
events.push({ type: 'turn/end', data: { turn: 1, reason: { kind: 'aborted' } } })
assert.equal((await readPersistedTurn(persistence, 's', 1)).terminal, 'aborted')
assert.equal(closed, 2)
console.log('Persisted open-turn/aborted-turn tests passed.')

const {goalSettlement} = await import('../bin/dsh_goal.mjs')
const handlers = new Map()
let goal={phase:'active',activation:'armed'}, idleCalls=0
const agent={whenIdle:async()=>{idleCalls++}}
const originalIdle=agent.whenIdle
const ctx={get:()=>({get:()=>goal}),on(name,fn){handlers.set(name,fn);return()=>handlers.delete(name)}}
const observer=goalSettlement(ctx,agent)
let returned=false
const waiting=observer.wait().then(()=>{returned=true})
await new Promise(resolve=>setTimeout(resolve,60))
assert.equal(returned,false);assert.equal(idleCalls,1,'No timer-driven poll')
assert.equal(agent.whenIdle,originalIdle)
goal={phase:'complete',activation:'disarmed'}
handlers.get('goal/changed')({agent})
await waiting;observer.dispose();assert.equal(handlers.size,0)
for (const phase of ['paused','blocked','active']) {
 goal={phase,activation:'disarmed'}
 const observed=goalSettlement(ctx,agent);await observed.wait();observed.dispose()
}
console.log('Native event settlement preserves methods and waits across slow checkpoints.')

const {goalRecoveryContext}=await import('../bin/dsh_goal.mjs')
assert.match(goalRecoveryContext(null),/none/)
for(const phase of ['complete','paused','blocked','active'])assert.match(goalRecoveryContext({phase}),new RegExp(phase))
const note=goalRecoveryContext({id:'goal-1',phase:'active',activation:'disarmed',revision:3})
assert.match(note,/"goal-1"/)
assert.match(note,/"revision":3/)
assert.match(note,/disarmed/)
assert.match(note,/update_goal/)
assert.match(note,/max_goal_rounds/);assert.match(note,/edit preserves phase/)
const longGoal=goalRecoveryContext({id:'goal-original',phase:'blocked',blockedReason:{message:'Complete original reason '.repeat(2000)}})
assert.ok(Buffer.byteLength(longGoal)<=2048);assert.match(longGoal,/complete original blocked reason/)
console.log('Live Goal context gives native recovery actions without changing state or investment.')

const {turnObservation}=await import('../bin/dsh_native.mjs')
const reportEvents=[
 {type:'turn/start',data:{turn:3}},
 {type:'assistant/message',data:{turn:3,message:{content:[{type:'text',text:'Original report.\n'}]}}},
 {type:'turn/end',data:{turn:3,reason:{kind:'completed'}}}]
assert.equal(turnObservation(reportEvents,3).final_text,'Original report.\n')
assert.equal(turnObservation(reportEvents,3).latest_turn,3)
reportEvents.push({type:'turn/start',data:{turn:4}})
assert.equal(turnObservation(reportEvents,3).latest_turn,4)
assert.equal(turnObservation(reportEvents,3).latest_terminal,undefined)
assert.equal(turnObservation(reportEvents,4).terminal,undefined)
assert.equal(turnObservation(reportEvents,4).final_text,undefined)
console.log('Original final recovered without management parsing; later/open turns remain visible.')

const responseEvents = [
 {type:'turn/start',data:{turn:5}},
 {type:'assistant/attempt',data:{stream:[{chunk:{reason:{kind:'error'}}}]}},
 {type:'assistant/message',data:{message:{content:[{type:'text',text:'Original.'}],
   source:{replayState:{response:{responseId:'resp_original',stopReason:'stop'}}}}}},
 {type:'turn/end',data:{turn:5,reason:{kind:'completed'}}},
 {type:'turn/start',data:{turn:6}},
 {type:'assistant/attempt',data:{stream:[]}},
]
const recovered = turnObservation(responseEvents,5)
assert.deepEqual(recovered.responses,[{blocks:['text'],stop_reason:'stop',response_id:'resp_original'}])
assert.equal(recovered.failed_response_attempts,1)
assert.equal(recovered.final_text,'Original.')
assert.equal(turnObservation(responseEvents,6).failed_response_attempts,undefined)

reportEvents.push({type:'turn/end',data:{turn:4,reason:{kind:'error',error:{code:'TIMEOUT'}}}})
assert.equal(turnObservation(reportEvents,3).latest_terminal,'error')
assert.equal(turnObservation(reportEvents,4).terminal,'error')
assert.deepEqual(turnObservation(reportEvents,4).native_error,{code:'TIMEOUT'})
assert.equal(turnObservation(reportEvents,3).native_error,undefined)
assert.equal(turnObservation(reportEvents,4).final_text,undefined)
console.log('Persisted native error belongs to the exact original turn.')

for (const [message, expected] of [
  ['Error reading response: malformed server-sent event JSON.', 'Error reading response: malformed server-sent event JSON.'],
  ['Authorization: Bearer private-value', 'Authorization: [REDACTED] [REDACTED]'],
  ['value ' + 'x'.repeat(100), 'value ' + 'x'.repeat(100)],
  ['short '.repeat(200), 'short '.repeat(200)],
]) {
  const failure = {type:'turn/end',data:{turn:4,reason:{kind:'error',error:{code:'PI_AI_ERROR',message,body:'not retained'}}}}
  assert.deepEqual(turnObservation([...reportEvents,failure],4).native_error,{code:'PI_AI_ERROR',message:expected,omitted_fields:['body']})
  assert.equal(turnObservation([...reportEvents,failure],3).native_error,undefined)
}
console.log('Original native error chain survives complete credential-safe observation.')

const {readFileSync}=await import('node:fs')
const {safeText}=await import('../bin/safe_diagnostics.mjs')
for(const row of JSON.parse(readFileSync(new URL('./fixtures/safe-diagnostics.json',import.meta.url))))assert.equal(safeText(row.input),row.expected)
const {rearmActiveGoal}=await import('../bin/dsh_goal.mjs')
let resumed=[]
for(const phase of ['active','paused','complete','blocked']) {
 const goal={id:'original',revision:8,phase,activation:'disarmed',maxGoalRounds:40,objective:'Original scope'}
 const goals={get:()=>goal,resume:(agent,ref)=>{resumed.push(ref);return {...goal,activation:'armed'}}}
 rearmActiveGoal(goals,{},true)
}
assert.deepEqual(resumed,[{id:'original',revision:8}])
console.log('Only authorized active Goal activation is rearmed in the original Session.')

const rawEvents=reportEvents.slice(0,3)
const stored={async open(){return {header:{id:'original',cwd:'/workspace'},async read(){return {events:rawEvents}},async close(){}}}}
for(const load of [async()=>{throw new Error('Missing optional module')},async()=>()=>{throw new Error('Unknown Goal projection')}]) {
 const read=await readPersistedTurn(stored,'original',3,load)
 assert.equal(read.final_text,'Original report.\n');assert.equal(read.terminal,'completed')
 assert.ok(read.goal_observation_issue)
}
console.log('Original text survives optional Goal module and projection failures.')

const {invocationObservation}=await import('../bin/dsh_runner.mjs')
const handlers2=new Map(), output=[]
const session2={id:'metering-unavailable',header:{cwd:'/workspace'}}
const observation=invocationObservation({on(name,fn){handlers2.set(name,fn);return()=>handlers2.delete(name)}},
 {session:session2},value=>output.push(value),()=>{throw new Error('Optional native metering unavailable')})
const record=(type,data)=>handlers2.get('session/event')(session2,{type,data})
record('turn/start',{turn:1});record('step/start',{turn:1,step:1})
record('assistant/message',{message:{content:[{type:'text',text:'Original complete report'}]}})
record('step/end',{turn:1,step:1});record('turn/end',{turn:1,reason:{kind:'completed'}})
assert.equal(observation.get().text,'Original complete report')
assert.equal(observation.get().reason.kind,'completed')
assert.equal('usage' in output.find(e=>e.phase==='step_end'),false)
observation.dispose()
console.log('Optional metering failure preserves the original report and terminal.')
