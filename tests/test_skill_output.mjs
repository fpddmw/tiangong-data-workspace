import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

// A real child writes beyond spawnSync's default pipe capacity. The ordinary
// public command entry is isolated; no account or dataset is invoked.
test('shared Skill forwards large child stdout and preserves nonzero exit', () => {
  const dir=mkdtempSync(path.join(os.tmpdir(),'skill-stream-'));
  try {
    const wrapper=new URL('../.agents/skills/scripts/lib/cli-launcher.mjs',import.meta.url).href;
    const runner=path.join(dir,'run.mjs');
    writeFileSync(runner,`import { runTiangongCommand } from ${JSON.stringify(wrapper)};
const result=runTiangongCommand(['--eval',"process.stdout.write('x'.repeat(3*1024*1024),()=>process.exit(7))"],
 {cliCommand:process.execPath});process.exitCode=result;`);
    const result=spawnSync(process.execPath,[runner],{encoding:'utf8',maxBuffer:4*1024*1024});
    assert.ifError(result.error);assert.equal(result.status,7,result.stderr);assert.equal(result.stdout.length,3*1024*1024);assert.equal(result.stderr,'');
  } finally {rmSync(dir,{recursive:true,force:true});}
});
