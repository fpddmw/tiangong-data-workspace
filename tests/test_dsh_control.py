"""Actual inherited socket lifecycle; no model, credentials or business runs."""
import json
from pathlib import Path
import socket
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
NODE=ROOT/'.toolchain/node/24.19.0-darwin-arm64/bin/node'

@unittest.skipUnless(NODE.exists(), 'local node unavailable')
class ControlTests(unittest.TestCase):
    def test_ack_close_pending_and_check_after_close(self):
        script='''
import assert from 'node:assert/strict';
import {controlChannel} from './bin/dsh_goal.mjs';
const channel=controlChannel(Number(process.argv[2]));
assert.equal(await channel.check('admission'),true);
const pending=channel.check('admission');
const rejected=assert.rejects(pending,/closed/);
channel.close(); await rejected;
await assert.rejects(channel.check('admission'),/closed/);
console.log('PASS');
'''
        parent,child=socket.socketpair()
        with parent,child:
            process=subprocess.Popen([str(NODE),'--input-type=module','-',str(child.fileno())],cwd=ROOT,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,pass_fds=(child.fileno(),))
            child.close()
            process.stdin.write(script);process.stdin.close();process.stdin=None
            first=json.loads(process.stdout.readline())
            self.assertEqual(first['kind'],'admission')
            parent.sendall((json.dumps({'id':first['id'],'accepted':True})+'\n').encode())
            output,error=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,error);self.assertIn('PASS',output)
