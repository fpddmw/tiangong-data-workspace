"""Full counters and conditions survive list previews and exact detail reads."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import harness_discovery as discovery
import harness_hook as hooks
import test_harness_tasks as fixtures


class AssetListIntegrityTests(unittest.TestCase):
    def test_nine_uses_keep_full_counts_whole_conditions_and_complete_pagination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'.harness-local/manager/state.sqlite'
            path.parent.mkdir(parents=True)
            tail='允许保存草稿，但尚未授权正式送审；以干基计量。'
            entries=[{'id':'U'+str(index),'state':'supported_with_limits',
                'statement':'完整科学说明 '*700+tail} for index in range(9)]
            body={'access':'local_workspace','title':'Original exact asset','description':'Original use and conditions',
                'limitations':[tail],'provenance':[{'id':'R'+str(i),'condition':tail} for i in range(9)],'uses':entries}
            with sqlite3.connect(path) as c:
                c.executescript('CREATE TABLE assets(id TEXT,current_version TEXT,updated_at TEXT);'
                    'CREATE TABLE asset_versions(asset_id TEXT,version TEXT,body TEXT,sha256 TEXT);'
                    'CREATE TABLE asset_uses(id INTEGER PRIMARY KEY,asset_id TEXT,work_id TEXT,requirement TEXT,version TEXT,body TEXT,at TEXT);')
                raw=json.dumps(body)
                c.execute('INSERT INTO assets VALUES (?,?,?)',('asset','v1','original-time'))
                c.execute('INSERT INTO asset_versions VALUES (?,?,?,?)',('asset','v1',raw,hashlib.sha256(raw.encode()).hexdigest()))
                c.executemany('INSERT INTO asset_uses VALUES (?,?,?,?,?,?,?)',
                    [(i,'asset','task','A'+str(i),'v1',json.dumps(entries[i]),'original-time') for i in range(9)])
            before=path.read_bytes()
            short=discovery.assets(root,task_id='task',limit=1)['items'][0]
            self.assertEqual(short['limitations'],[tail])
            self.assertEqual(short['use_total'],9)
            self.assertEqual(short['use_observations_pagination']['total'],9)
            for field in ('declared_uses','use_observations'):
                for entry in short[field]:
                    self.assertTrue(entry['not_expanded'])
                    self.assertNotIn('state',entry);self.assertNotIn('statement',entry)
                    self.assertIn('--asset-version v1',entry['detail_command'])
            seen=[];offset=0
            while True:
                page=discovery.assets(root,task_id='task',asset_id='asset',version='v1',limit=3,offset=offset)['items'][0]
                self.assertEqual(page['use_total'],9)
                seen.extend(item['requirement'] for item in page['use_observations'])
                self.assertTrue(all(item['statement'].endswith(tail) for item in page['use_observations']))
                self.assertTrue(all(item['statement'].endswith(tail) for item in page['declared_uses']))
                if page['use_next_offset'] is None:break
                offset=page['use_next_offset']
            self.assertEqual(len(seen),9);self.assertEqual(len(set(seen)),9)
            self.assertEqual(path.read_bytes(),before)


class MaintenanceListIntegrityTests(unittest.TestCase):
    def test_more_than_eight_affected_requests_and_findings_have_full_detail_counters(self):
        f=fixtures.TaskCoreTests('runTest');f.setUp();self.addCleanup(f.tearDown)
        f.call(f.main,'grant-role',{'id':'maintainer','actor':f.other,'org_role':'maintainer','evidence_ref':f.evidence})
        (f.root/'.control/runtime.json').write_text('{"binding":"same fixture runtime"}')
        actors=[]
        for index in range(9):
            worker=f.actor('worker-'+str(index));actors.append(worker)
            f.call(f.main,'grant-role',{'id':'role-'+str(index),'actor':worker,'org_role':'executor','evidence_ref':f.evidence})
            task='task-'+str(index)
            f.call(f.main,'create',{'id':task,'owner_actor':f.lead,'original_ref':f.brief,'plan_ref':f.plan,
                'summary':'Original affected task','required_scope':['a']})
            f.call(f.lead,'request',{'id':'request-'+str(index),'task_id':task,'kind':'plan','recipient':worker,
                'recipient_role':'executor','input_ref':f.brief,'summary':'Original affected input','plan_revision':1,'required_scope':['a']})
            event={'hook_event_name':'PostToolUse','session_id':worker['id'],'cwd':str(f.root),
                'turn_id':'turn','tool_use_id':'tool-'+str(index),'tool_name':'Bash',
                'tool_input':{'command':'python3 -B bin/lca_gate.py --rows ORIGINAL-'+str(index)},
                'tool_response':{'exit_code':3,'output':'Original fixture failure'}}
            with patch.dict('os.environ',{'CODEX_THREAD_ID':worker['id']}):hooks.handle(f.root,event)
        with patch.dict('os.environ',{'CODEX_THREAD_ID':f.other['id']}):
            group,=hooks.pending_observations(f.root,f.other,all_actors=True)
            self.assertEqual(group['affected_total'],9);self.assertEqual(group['affected_shown'],8)
            self.assertEqual(len(group['affected']),8)
            self.assertIn('--problem',group['problem_detail_command'])
            details=hooks.pending_observations(f.root,f.other,all_actors=True,problem_id=group['problem_id'])
            self.assertEqual(len({entry['task_id'] for row in details for entry in row['affected']}),9)
            for row in details:hooks.link_observation(f.root,f.other,row['source_ref']['path'],task_id=row['candidates'][0])
        event={**event,'session_id':actors[0]['id'],'tool_use_id':'new-recurrence',
            'tool_input':{'command':'python3 -B bin/lca_gate.py --rows ORIGINAL-AGAIN'}}
        with patch.dict('os.environ',{'CODEX_THREAD_ID':actors[0]['id']}):hooks.handle(f.root,event)
        with patch.dict('os.environ',{'CODEX_THREAD_ID':f.other['id']}):
            current,=hooks.pending_observations(f.root,f.other,all_actors=True)
            self.assertEqual(current['existing_findings_total'],9)
            self.assertEqual(current['existing_findings_shown'],8)
            detail=hooks.pending_observations(f.root,f.other,all_actors=True,problem_id=current['problem_id'])
        self.assertTrue(all(row['existing_findings_total']==9 and len(row['existing_findings'])==9 for row in detail))


if __name__=='__main__':unittest.main()
