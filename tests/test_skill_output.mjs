import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'

const skills = ['', 'flow-hybrid-search/', 'process-hybrid-search/', 'lifecyclemodel-hybrid-search/', 'lca-study/']
for (const skill of skills) {
  test(`ordinary Skill output survives large stdout and stderr (${skill || 'shared'})`, () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'skill-stream-'))
    try {
      const launcher = new URL(`../.agents/skills/${skill}scripts/lib/cli-launcher.mjs`, import.meta.url)
      const runner = path.join(dir, 'probe.mjs')
      const child = "const s=require('node:fs').readFileSync(0,'utf8');process.stdout.write(s+'x'.repeat(3*1024*1024));process.stderr.write('y'.repeat(2*1024*1024),()=>process.exitCode=7)"
      writeFileSync(runner, `import {runTiangongCommand} from ${JSON.stringify(launcher.href)};
process.exitCode=runTiangongCommand(['--eval',${JSON.stringify(child)}],{cliCommand:process.execPath});`)
      const result = spawnSync(process.execPath, [runner], {encoding:'utf8', input:'original stdin', maxBuffer:8*1024*1024})
      assert.ifError(result.error)
      assert.equal(result.status, 7)
      assert.ok(result.stdout === 'original stdin' + 'x'.repeat(3*1024*1024))
      assert.ok(result.stderr === 'y'.repeat(2*1024*1024))
    } finally {
      rmSync(dir, {recursive:true, force:true})
    }
  })
}

test('explicit capture returns the two strings and child status', async () => {
  const {executeTiangongCommand} = await import('../.agents/skills/scripts/lib/cli-launcher.mjs')
  const result = executeTiangongCommand(['--eval', "process.stdout.write('a');process.stderr.write('b');process.exitCode=7"], {cliCommand:process.execPath})
  assert.deepEqual([result.stdout,result.stderr,result.status], ['a','b',7])
})
