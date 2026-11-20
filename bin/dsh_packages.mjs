// Read-only UI adapter over the canonical package projection. No path heuristics.
import {execFile} from 'node:child_process'
import {promisify} from 'node:util'
import {join} from 'node:path'
import {pathToFileURL} from 'node:url'
import {randomUUID} from 'node:crypto'
const execute=promisify(execFile)
export async function packageCoordinates(root,{maxBuffer=16*1024*1024,includeCapacity=false}={}) {
  const argv=[join(root,'bin/harness.py'),'--root',root,'work-packages']
  let stdout
  try {({stdout}=await execute(join(root,'.venv/bin/python'),argv,{maxBuffer,timeout:30000}))}
  catch(error) {
    if(error.code==='ERR_CHILD_PROCESS_STDIO_MAXBUFFER') {
      const output=join(root,'work','package-view-'+randomUUID()+'.json')
      let full
      try {
        const saved=await execute(join(root,'.venv/bin/python'),[...argv,'--output',output],{maxBuffer:256*1024,timeout:30000})
        full=JSON.parse(saved.stdout)
        if(typeof full.output!=='string' || !/^[a-f0-9]{64}$/.test(full.sha256??''))throw new Error('Invalid complete export acknowledgment')
      } catch(failure) {
        throw Object.assign(new Error('读取未完成：complete package export could not be verified. Use work-packages --output FILE.'),
          {code:'READ_INCOMPLETE',causeCode:failure.code??'INVALID_EXPORT_ACK',intendedOutput:output,
           detailCommand:'python3 bin/harness.py work-packages --output '+output})
      }
      throw Object.assign(new Error('读取未完成：package output exceeded the process buffer. Complete query: '+full.output),
        {code:'READ_INCOMPLETE',output:full,detailCommand:'python3 bin/harness.py work-packages --output '+output})
    }
    throw Object.assign(new Error('读取未完成：original package query unavailable; use work-packages --output FILE.'),{code:'READ_INCOMPLETE',causeCode:error.code})
  }
  let result
  try {result=JSON.parse(stdout)} catch {
    throw Object.assign(new Error('读取未完成：invalid or partial JSON; use work-packages --output FILE.'),{code:'READ_INCOMPLETE'})
  }
  if(result.source!=='original_work_task_store' || !Array.isArray(result.packages)) throw Object.assign(new Error('读取未完成：invalid original package projection'),{code:'READ_INCOMPLETE'})
  if(includeCapacity===true) {
    result.capacity=null
    try {
      const {stdout: capacityOutput}=await execute(join(root,'.venv/bin/python'),
        [join(root,'bin/harness.py'),'--root',root,'work-capacity'],{maxBuffer,timeout:30000})
      const {observed_at_unix,counts,occupancy_counts}=JSON.parse(capacityOutput)
      if(Number.isFinite(observed_at_unix) && counts && occupancy_counts) {
        result.capacity={observed_at_unix,counts,occupancy_counts}
      }
    } catch {
      // Keep the original package projection available if capacity cannot be read.
    }
  }
  return result
}
export function groupPackages(sessions, snapshot) {
  const matched=new Set()
  const groups=snapshot.packages.map(pkg=>{
    const ids=new Set(pkg.programs.map(r=>r.sessionId).filter(Boolean))
    const rows=sessions.filter(s=>ids.has(s.id));rows.forEach(s=>matched.add(s.id))
    const programs=pkg.programs.map(p=>({...p,operation:p.operation??null}))
    const current=pkg.stages.filter(s=>!s.independent).at(-1)
    const authorPrograms=programs.filter(p=>!p.independent && (!current?.requestId || p.requestId===current.requestId))
    const latest=[...authorPrograms].sort((a,b)=>(a.updatedAt??'').localeCompare(b.updatedAt??'')).at(-1)
    const returned=[...pkg.returns].filter(r=>!current?.requestId || r.requestId===current.requestId)
      .sort((a,b)=>(a.createdAt??'').localeCompare(b.createdAt??'')).at(-1)
    return {...pkg,sessions:rows,programs,steps:rows.reduce((n,s)=>n+(s.steps??0),0),
      currentRequestId:current?.requestId??null,authorSessionId:latest?.sessionId??null,
      continuationRef:latest?.continuationRef??null,goalObservation:latest?.goalObservation??null,
      stage:{declared:{value:current?.declared??'unknown',source:current?.brief},actual:returned?.stage??null,endpoint:current?.endpoint??null},
      summaryNote: returned && !returned.structuredSummaryAvailable ? 'Read the original conclusion; stage and gaps were not separately declared.' : null,
      conclusion:returned??null,nativeStatus:latest?.nativeStatus??'not_started',acceptance:pkg.decisions}
  })
  const unknown=sessions.filter(s=>!matched.has(s.id))
  if(unknown.length) groups.push({id:'unassigned',package:'未绑定业务包 / 工具资格',packageRoot:null,
    sessions:unknown,programs:[],steps:unknown.reduce((n,s)=>n+(s.steps??0),0),
    stage:{declared:{value:'unknown'},actual:null,endpoint:null},nativeStatus:'unassigned',conclusion:null,acceptance:[]})
  return groups
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) console.log(JSON.stringify(await packageCoordinates(process.argv[2]),null,2))
