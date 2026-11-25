// Actual child-process buffer boundaries; fake read-only CLI, no data/model calls.
import assert from 'node:assert/strict'
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join,resolve} from 'node:path'
import {createHash} from 'node:crypto'
import {packageCoordinates,groupPackages} from '../bin/dsh_packages.mjs'

const root=mkdtempSync(join(tmpdir(),"htam package's read-"))
try {
  mkdirSync(join(root,'bin'))
  writeFileSync(join(root,'bin','harness.py'),`import hashlib,json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
assert sys.argv[1]=='--root' and Path(sys.argv[2]).resolve()==root
with (root/'calls.jsonl').open('a') as log:log.write(json.dumps(sys.argv[1:])+'\\n')
mode=(root/'mode').read_text()
if sys.argv[3]=='work-capacity':
 if mode=='capacity-failed':raise SystemExit(1)
 if mode=='capacity-unobserved':print('{"counts":{"occupied":null}}');raise SystemExit(0)
 print(json.dumps({'observed_at_unix':1794940395.25,'counts':{'occupied':{'author':2,'review':6}},
 'occupancy_counts':{'confirmed_execution':{'author':1,'review':6}},'waiting':[{'run_id':'private'}]}))
 raise SystemExit(0)
assert sys.argv[3]=='work-packages'
if mode=='partial':print('{"source":"original_work_task_store","packages":[');raise SystemExit(0)
body={'source':'original_work_task_store','packages':[{'id':'one','package':'Original package',
 'programs':[{'sessionId':'native-one','requestId':'request','operation':'resume','updatedAt':'1','nativeStatus':'running',
 'goalObservation':{'goal':{'phase':'active'}},'continuationRef':{'path':'original-note'}}],
 'stages':[{'requestId':'request','independent':False,'endpoint':'candidate'}],
 'returns':[{'requestId':'request','createdAt':'1','structuredSummaryAvailable':False,
 'contribution':'完整说明 '*20000+'允许保存草稿，但尚未授权正式送审。'}],
 'decisions':[],'scope':{'remaining_scope':['P'+str(i) for i in range(9)]}}]}
if '--output' in sys.argv:
 if mode=='export-failed':print('{"error":{"code":"WRITE_DENIED"}}');raise SystemExit(1)
 if mode=='export-partial':print('{"output":');raise SystemExit(0)
 output=Path(sys.argv[sys.argv.index('--output')+1]);output.parent.mkdir(parents=True,exist_ok=True)
 raw=(json.dumps(body,ensure_ascii=False)+'\\n').encode();output.write_bytes(raw)
 print(json.dumps({'output':str(output),'sha256':hashlib.sha256(raw).hexdigest()}))
else:print(json.dumps(body,ensure_ascii=False))
`)
  writeFileSync(join(root,'mode'),'full')
  let overflow
  await assert.rejects(()=>packageCoordinates(root,{maxBuffer:128}),error=>{
    overflow=error;return error.code==='READ_INCOMPLETE' && /Complete query/.test(error.message)
  })
  const bytes=readFileSync(overflow.output.output)
  assert.equal(createHash('sha256').update(bytes).digest('hex'),overflow.output.sha256)
  const full=JSON.parse(bytes)
  assert.equal(full.packages[0].scope.remaining_scope.length,9)
  assert.ok(full.packages[0].returns[0].contribution.endsWith('允许保存草稿，但尚未授权正式送审。'))
  const calls=readFileSync(join(root,'calls.jsonl'),'utf8').trim().split('\n').map(JSON.parse)
  assert.equal(calls.length,2)
  assert.ok(calls.every(argv=>argv[2]==='work-packages' && !argv.includes('--execute')))
  assert.ok(calls[1].includes('--output'))
  assert.ok(overflow.detailCommand.includes("'\\''"), 'The apostrophe in the checkout path is escaped')
  const accepted=await packageCoordinates(root)
  assert.equal(Object.hasOwn(accepted,'capacity'),false)
  assert.equal(readFileSync(join(root,'calls.jsonl'),'utf8').trim().split('\n').length,3)
  const {capacity,...withCapacity}=await packageCoordinates(root,{includeCapacity:true,python:resolve(process.argv[2])})
  assert.deepEqual(withCapacity,accepted)
  assert.deepEqual(capacity,{
    observed_at_unix:1794940395.25,
    counts:{occupied:{author:2,review:6}},
    occupancy_counts:{confirmed_execution:{author:1,review:6}},
  })
  for(const mode of ['capacity-failed','capacity-unobserved']) {
    writeFileSync(join(root,'mode'),mode)
    assert.deepEqual(await packageCoordinates(root,{includeCapacity:true}),{...accepted,capacity:null})
  }
  const groups=groupPackages([{id:'native-one',steps:2}],accepted)
  assert.equal(groups[0].scope.remaining_scope.length,9)
  assert.equal(groups[0].goalObservation.goal.phase,'active')
  assert.ok(groups[0].conclusion.contribution.endsWith('允许保存草稿，但尚未授权正式送审。'))
  writeFileSync(join(root,'mode'),'partial')
  await assert.rejects(()=>packageCoordinates(root),error=>error.code==='READ_INCOMPLETE' && /partial JSON/.test(error.message))
  for(const mode of ['export-failed','export-partial']) {
    writeFileSync(join(root,'mode'),mode)
    await assert.rejects(()=>packageCoordinates(root,{maxBuffer:128}),error=>
      error.code==='READ_INCOMPLETE' && error.output===undefined && /--output/.test(error.detailCommand))
  }
  console.log('Complete package output, buffer overflow export, partial JSON rejection and optional capacity projection passed.')
} finally {rmSync(root,{recursive:true,force:true})}
