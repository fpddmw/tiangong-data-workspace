// Test-only model route. The installed Agent, tools, Goal driver and CLI are real.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'
import {randomUUID} from 'node:crypto'
import {readFileSync, writeFileSync} from 'node:fs'

export async function apply(ctx, config) {
  const require = createRequire(config.moduleEntry)
  const {LlmAdapter} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')))
  if(config.flush_open_turn_marker)ctx.on('session/event',(session,event)=>{
    if(event.type==='turn/start'&&event.data.turn===2)
      queueMicrotask(()=>ctx.sessions.flush(session).then(()=>writeFileSync(config.flush_open_turn_marker,'native turn start flushed')))
  })
  let step = 0, goal, review
  if (config.slow_flush_ms) {
    // Fault injection only: real persistence still writes all original bytes.
    const flush=ctx.sessions.flush.bind(ctx.sessions)
    ctx.sessions.flush=async (...args)=>{await new Promise(resolve=>setTimeout(resolve,config.slow_flush_ms));return flush(...args)}
  }
  const text = value => ({type:'text',text:value})
  const call = (name,args={}) => ({type:'tool-call',id:randomUUID(),name,arguments:JSON.stringify(args)})
  class Scripted extends LlmAdapter {
    async *stream(options) {
      assert.equal(options.provider,'htam-offline')
      const tool = [...options.messages].reverse().find(m => m.role === 'tool')
      if (tool?.isError && config.phase !== 'shell') throw new Error('Native tool failed: '+JSON.stringify(tool.content))
      if (tool) {
        try {
          const value = JSON.parse(tool.content.filter(b=>b.type==='text').map(b=>b.text).join(''))
          if (value.goal) goal = value.goal
          if (value.diagnostic?.review_id) review = value.diagnostic.review_id
          if (value.accepted === false) throw new Error('Control action rejected: '+JSON.stringify(value))
        } catch (error) {if (!(error instanceof SyntaxError)) throw error}
      }
      const author = options.tools.some(t=>t.name==='create_goal')
      let block
      if (!author) {
        const prompt=options.messages.flatMap(m=>m.content ?? []).filter(b=>b.type==='text').map(b=>b.text).join('\n')
        const delta=config.focused && /^完整字段和交换差异 (.+)$/m.exec(prompt)?.[1]
        if (config.a02_verifier) {
          const facts = /^审阅材料索引 (.+)$/m.exec(prompt)?.[1]
          assert.ok(facts, 'A02 reviewer did not receive the frozen facts')
          if (step++ === 0) block=call('local_check',{argv:[config.python,config.a02_verifier,facts]})
          else {
            assert.match(JSON.stringify(tool.content),/A02-facts-verified/)
            block=text('A02-facts-verified: retain the flux identity error, steelworks/rolling boundary and annual basis, and Furnes mass and electricity qualifications.')
          }
        } else if (delta && step++===0) block=call('local_check',{argv:[config.python,config.delta_verifier,delta]})
        else if (delta) {
          assert.equal(tool?.isError,false)
          assert.match(JSON.stringify(tool.content),/delta-local-check-passed/)
          block=text('Focused offline opinion: version-paired Process and Flow changes read; language and exchange differences retained in the frozen packet.')
        } else {
          await new Promise(resolve=>setTimeout(resolve,300))
          block=text(config.original_candidate
            ? 'Scoped replay opinion: keep 0.021 kg per kg dry black mass distinct from 0.014883 kg per kg product in both languages.'
            : 'Independent opinion: use the dry-mass basis and verify the denominator.')
        }
      } else if (config.phase === 'a02_review') {
        const actions = [
          ()=>call('request_review',{artifacts:config.a02_artifacts,question:config.a02_question}),
          ()=>call('review_result',{review_id:review,wait_seconds:30}),
          ()=>{assert.match(JSON.stringify(tool.content),/A02-facts-verified/);return call('get_goal')},
          ()=>call('update_goal',{goal_id:goal.id,revision:goal.revision,action:'complete'}),
          ()=>text('The original A02 frozen evidence and independent check were delivered.'),
        ]
        assert.ok(step<actions.length,'Unexpected extra A02 model action')
        block=actions[step++]()
      } else if (config.focused) {
        const actions=[
          ()=>call('create_goal',{objective:'Exercise independent review of the original and revision',max_goal_rounds:8}),
          ()=>call('write',{file_path:config.candidate,content:readFileSync(config.original_candidate,'utf8')}),
          ()=>call('request_review',{artifacts:[config.candidate],question:'First exact independent review.'}),
          ()=>call('review_result',{review_id:review,wait_seconds:30}),
          ()=>call('write',{file_path:config.candidate,content:readFileSync(config.revised_candidate,'utf8')}),
          ()=>call('request_review',{artifacts:[config.candidate],question:'Check the revised exact fields.',prior_review_id:review}),
          ()=>call('review_result',{review_id:review,wait_seconds:30}),
          ()=>{assert.match(JSON.stringify(tool.content),/Focused offline opinion/);return call('get_goal')},
          ()=>call('update_goal',{goal_id:goal.id,revision:goal.revision,action:'complete'}),
          ()=>text('The author received the independent focused opinion and delivered the engineering evidence.'),
        ]
        assert.ok(step<actions.length,'Unexpected additional model call')
        block=actions[step++]()
      } else if (config.phase === 'hold') {
        if(step++===0) block=call('create_goal',{objective:'Isolated interruption check',max_goal_rounds:8})
        else if(step===2 && config.effect_file) block=call('bash',{
          description:'Append the one original isolated effect',
          command:"printf '%s\\n' original-effect >> '"+config.effect_file.replaceAll("'","'\\''")+"'"})
        else if(step===(config.effect_file?3:2)) block=text('Original completed turn before interruption.')
        else {await new Promise((resolve,reject)=>{
          if(options.signal.aborted) return reject(new Error('Cancelled'))
          options.signal.addEventListener('abort',()=>reject(new Error('Cancelled')),{once:true})
        });return}
      } else if (config.phase === 'pause') {
        if(step++===0) block=call('create_goal',{objective:'Isolated explicit pause check',max_goal_rounds:8})
        else if(step===2) block=call('update_goal',{goal_id:goal.id,revision:goal.revision,action:'pause'})
        else block=text('Paused as requested; retained original work.')
      } else if (config.phase === 'shell') {
        block=step++===0?call('bash',{description:'Observe an isolated nonzero exit',command:'exit 7'}):text('The command failed; original result remains available.')
      } else if (config.phase === 'seed') {
        if (step++ === 0) block=call('create_goal',{objective:'Complete the original isolated candidate with independent review.',max_goal_rounds:8})
        else {yield {type:'finish',reason:{kind:'error',failure:{code:'OFFLINE_INTERRUPTION',message:'Deliberate isolated interruption'}}};return}
      } else {
        const actions=[
          ()=>call('get_goal'),
          ()=>{assert.equal(goal.phase,'active');return text('Original Goal restored; continue the complete local work.')},
          ()=>call('write',{file_path:config.candidate,content:config.original_candidate
            ? readFileSync(config.original_candidate,'utf8') : '{"basis":"wet","value":8}'}),
          ()=>call('request_review',{artifacts:[config.candidate,...(config.calculation ? [config.calculation] : [])],
            question:'Check the reference denominator for the source-limited use.'}),
          ()=>text('Independent review requested; continue in this Goal.'),
          ()=>call('review_result',{review_id:review,wait_seconds:10}),
          ()=>call('write',{file_path:config.candidate,content:config.revised_candidate
            ? readFileSync(config.revised_candidate,'utf8') : '{"basis":"dry","value":4}'}),
          ()=>call('get_goal'),
          ()=>call('update_goal',{goal_id:goal.id,revision:goal.revision,action:'complete'}),
          ()=>text('Revised using independent dry-mass feedback; lead owns adoption and submission.'),
        ]
        assert.ok(step<actions.length,'Unexpected additional model call')
        block=actions[step++]()
      }
      yield {type:'block-start',index:0,blockType:block.type}
      if(block.type==='text') yield {type:'text-delta',index:0,text:block.text}
      else yield {type:'tool-call-delta',index:0,id:block.id,name:block.name,argumentsDelta:block.arguments}
      yield {type:'block-end',index:0,block}
      yield {type:'usage',usage:{inputTokens:1,outputTokens:1}}
      yield {type:'finish',reason:{kind:block.type==='tool-call'?'tool-calls':'stop'}}
    }
  }
  ctx.llm.registerAdapter(['htam-offline'],new Scripted())
}
