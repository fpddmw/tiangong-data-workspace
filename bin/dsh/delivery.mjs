// Author tools: local checks, original login refresh, and independent review.
import {createRequire} from 'node:module'
import {realpathSync} from 'node:fs'
import {pathToFileURL} from 'node:url'

function nativeParameters(declaration) {
  const schema = declaration.inputSchema
  return Object.fromEntries(Object.entries(schema.properties).map(([name, value]) => {
    // The installed DSH value DSL lacks minItems. The common dispatcher still
    // validates the complete authoritative schema, including nonempty argv.
    const {minItems, ...nativeValue} = value
    return [name, {...nativeValue, ...(schema.required.includes(name) ? {required:true} : {})}]
  }))
}

export async function installTiangongCli(ctx, moduleEntry, channel, declaration) {
  const require = createRequire(realpathSync(moduleEntry))
  const {defineTool} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')))
  ctx.tools.register(defineTool({
    name:'tiangong_cli', description:declaration.description, parameters:nativeParameters(declaration),
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(args,exec) { return JSON.stringify(await channel.request('tiangong_cli',JSON.stringify(args),exec.signal)) },
    presentCall:()=>({card:'generic',title:'TianGong CLI',kind:'read'}),
  }))
}

export async function installLocalCheck(ctx, moduleEntry, channel) {
  const require = createRequire(realpathSync(moduleEntry))
  const {defineTool} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')))
  ctx.tools.register(defineTool({
    name:'local_check',
    description:'Run a local calculation or offline CLI check with network access denied by the OS. Reviewer candidates remain read-only. Save temporary reports under the HTAM_CHECK_OUTPUT environment directory; the result returns that path. This grants no database or credential access.',
    parameters:{argv:{type:'array',items:{type:'string'},required:true,description:'Executable followed by arguments; no implicit shell.'}},
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(args,exec) { return JSON.stringify(await channel.request('local_check',JSON.stringify(args),exec.signal)) },
    presentCall:()=>({card:'generic',title:'Offline calculation',kind:'read'}),
  }))
}

export async function installIdentityRefresh(ctx, moduleEntry, channel) {
  const require = createRequire(realpathSync(moduleEntry))
  const {defineTool} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')))
  ctx.tools.register(defineTool({
    name:'refresh_editor_identity',
    description:'Ask the original trusted launcher to check and refresh its existing TianGong editor login through the official CLI. No account switching, credentials access or data writes. Use after expiry or a sandbox refresh error; continue local work if unavailable.',
    parameters:{},
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(_args,exec) {
      const {accepted,diagnostic} = await channel.request('refresh_editor_identity',undefined,exec.signal)
      return JSON.stringify({accepted,diagnostic})
    },
    presentCall:()=>({card:'generic',title:'Check original editor login',kind:'read'}),
  }))
}

// The author requests an actual review; scope, identities and hashes are owned
// by the dispatcher. Neither agent has to author a management return envelope.
export async function installReviewRequest(ctx, moduleEntry, channel, declaration) {
  const require = createRequire(realpathSync(moduleEntry))
  const {defineTool} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')))
  ctx.tools.register(defineTool({
    name:'request_review',
    description:declaration.description, parameters:nativeParameters(declaration),
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(args,exec) {
      if (!exec.agent || ctx.agents.get(exec.agent.id)!==exec.agent) throw new Error('Review belongs to the original live author')
      const result=await channel.request('request_review',JSON.stringify(args),exec.signal)
      return JSON.stringify(result)
    },
    presentCall:()=>({card:'generic',title:'Request independent review',kind:'read'}),
  }))
}

export async function installReviewResult(ctx, moduleEntry, channel) {
  const require = createRequire(realpathSync(moduleEntry))
  const {defineTool} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')))
  ctx.tools.register(defineTool({
    name:'review_result',
    description:'Read the original independent opinion and native state. Default wait_seconds is 0; explicit waits are bounded to 600 seconds and cancellable. Continue independent work and keep the original review_id. Author completion does not cancel the review; judge the opinion and revise.',
    parameters:{review_id:{type:'string',required:true},wait_seconds:{type:'number',description:'Observation timeout 0–600 seconds; default 0. The original service validates the bound.'}},
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(args,exec) {return JSON.stringify(await channel.request('review_result',JSON.stringify(args),exec.signal))},
    presentCall:()=>({card:'generic',title:'Read independent opinion',kind:'read'}),
  }))
}

export async function installFindingReport(ctx, moduleEntry, channel) {
  const require = createRequire(realpathSync(moduleEntry))
  const {defineTool} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')))
  ctx.tools.register(defineTool({
    name:'report_finding',
    description:'Report a shared tool or runtime problem on the original task board. Save the observed error, affected action and original evidence in a file, then give its path. The host records your actual Session and task; continue independent work while maintenance handles the finding.',
    parameters:{report:{type:'string',required:true,description:'Path to the saved original problem report.'},
                finding_id:{type:'string',description:'Existing finding to append this observation to; omit for a new problem.'}},
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(args,exec) { return JSON.stringify(await channel.request('report_finding',JSON.stringify(args),exec.signal)) },
    presentCall:()=>({card:'generic',title:'Report shared tool problem',kind:'read'}),
  }))
}
