"""Exercise original review ownership, queue withdrawal, and focused frozen inputs."""
import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_base import identity
from agent_review import ReviewService
import dsh_capacity as capacity
from execution_lease import atomic_json
import harness_tasks as tasks
import review_delta
import review_queue
import run_worker
import test_agent_runtime as fixture

ROOT=Path(__file__).resolve().parents[1]
APPLE=ROOT/'materials/packages/tiangong-package-gap-global-main-20261110/apple-scm-reuse-20261110/apple'
NG=APPLE/'dsh-batch/ng/program-b1b6aec1a0d39046/program-1877c491d8f01547/reviews'


class ReviewRevisionTests(unittest.TestCase):
    setUp=fixture.RuntimeTests.setUp
    create_request=fixture.RuntimeTests.create_request
    ref=fixture.RuntimeTests.ref

    def author(self):
        native=identity('deepseek_harness','session','fixture-author',self.root,'1')
        self.run_intent('author-run','request')
        with tasks.transaction(self.root) as c:
            c.execute('UPDATE work_runs SET native_identity_json=?,outcome=\'running\' WHERE id=\'author-run\'',(tasks._json(native),))
        return native

    def run_intent(self, run_id, request_id):
        return tasks.mutate(self.root,'run-intent',self.actor,{'id':run_id,'request_id':request_id,
            'config_ref':self.ref(self.config_path),'input_refs':[self.input_ref], 'target_host':'deepseek_harness','operation':'start'})

    def original_ng(self, ident):
        source=NG/('program-review:'+ident)/'packet.json'
        if not source.is_file():self.skipTest('Pinned original NG packets are unavailable')
        packet=json.loads(source.read_bytes())
        selected=next(ref for ref in packet['artifact_refs'] if Path(ref['id']).name=='candidate.body.json')
        raw=Path(selected['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),selected['sha256'])
        path=self.home/('ng-'+ident+'.json');path.write_bytes(raw)
        return path

    def packet(self, native, path, **fields):
        return {'authors':[native],'review_scope':'Review original frozen fixture','artifact_refs':[{'id':str(path),**self.ref(path)}],
                'evidence_refs':[self.input_ref,self.ref(path)],**fields}

    def register(self, parent, packet):
        return run_worker._program_review_request(self.root,SimpleNamespace(actor=self.actor),parent,packet)

    def test_ng_replacement_retires_only_the_named_queued_review(self):
        native=self.author();parent=tasks.read_request(self.root,'request')
        previous=self.original_ng('309ffad95f979f50564e124eb52d0bb6')
        revised=self.original_ng('e0d5c430da4129a240ed1d61ae90f79e')
        old=self.register(parent,self.packet(native,previous))
        self.run_intent('old-queued',old['id'])
        queue=capacity._queue(self.root)
        queue['entries']=[{'run_id':'old-queued','request_id':old['id'],'phase':'queued','kind':'review','sequence':1}]
        atomic_json(capacity._paths(self.root)[0],queue)
        distinct=json.loads(previous.read_bytes());distinct['processDataSet']['processInformation']['dataSetInformation']['common:UUID']='fa8bd20c-c65a-45ea-9716-ec1d96ad8576'
        other=self.home/'other.json';other.write_text(json.dumps(distinct))
        independent=self.register(parent,self.packet(native,other))
        new=self.register(parent,self.packet(native,revised,supersedes_review_id=old['id']))
        with self.assertRaisesRegex(tasks.WorkConflict,'not been accepted'):
            self.run_intent('too-soon',new['id'])
        before=review_queue.author_review(self.root,parent,native,old['id'])[1]
        with self.assertRaisesRegex(ValueError,'original author'):
            review_queue.author_review(self.root,parent,{**native,'id':'somebody','instance':'somebody'},old['id'])
        with self.assertRaisesRegex(ValueError,'no returned independent native opinion'):
            review_delta.baseline(self.root,parent,native,self.packet(native,revised),old['id'])
        first=review_queue.replace(self.root,parent,native,new,old,self.actor)
        self.assertEqual(first['state'],'replaced_before_start')
        self.assertEqual(review_queue.replace(self.root,parent,native,new,old,self.actor),first)
        self.assertTrue(tasks.read_run(self.root,'old-queued')['stop_requested'])
        self.assertEqual(self.run_intent('only-new',new['id'])['may_launch'],True)
        self.assertTrue(self.run_intent('unrelated-still-eligible',independent['id'])['may_launch'])
        with self.assertRaisesRegex(tasks.WorkConflict,'replaced'):
            self.run_intent('late-old',old['id'])
        self.assertEqual(review_queue.author_review(self.root,parent,native,old['id'])[1],before)
        service=ReviewService(SimpleNamespace(root=self.root,actor=self.actor),parent,{},'author-run',self.home,lambda:None)
        self.assertEqual(service.result({'review_id':old['id']})['state'],'superseded_before_start')

    def test_replacement_losing_capacity_claim_never_stops_or_starts(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        native=self.author();parent=tasks.read_request(self.root,'request')
        old=self.register(parent,self.packet(native,self.original_ng('309ffad95f979f50564e124eb52d0bb6')))
        new=self.register(parent,self.packet(native,self.original_ng('e0d5c430da4129a240ed1d61ae90f79e'),supersedes_review_id=old['id']))
        self.run_intent('old-racing',old['id'])
        taken=Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with capacity._lock(self.root):
                def attempt():
                    taken.set()
                    return review_queue.replace(self.root,parent,native,new,old,self.actor)
                future=pool.submit(attempt)
                self.assertTrue(taken.wait(5))
                queue=capacity._queue(self.root)
                queue['entries']=[{'run_id':'old-racing','request_id':old['id'],'phase':'admitted','kind':'review','sequence':1}]
                atomic_json(capacity._paths(self.root)[0],queue)
            result=future.result(timeout=10)
        self.assertEqual(result['state'],'claimed')
        self.assertFalse(tasks.read_run(self.root,'old-racing')['stop_requested'])
        with self.assertRaisesRegex(tasks.WorkConflict,'not been accepted'):
            self.run_intent('new-withheld',new['id'])

    def test_compares_frozen_fields_and_language_without_guessing_science(self):
        previous=self.original_ng('309ffad95f979f50564e124eb52d0bb6')
        before=json.loads(previous.read_bytes())
        previous_exchanges=before['processDataSet']['exchanges']['exchange']
        ref=before['processDataSet']['processInformation']['dataSetInformation']
        ref['common:generalComment']=[{'@xml:lang':'en','#text':'Mass per dry reference output.'},
                                      {'@xml:lang':'zh','#text':'干产品基准。'}]
        previous.write_text(json.dumps(before))
        after=json.loads(previous.read_bytes())
        after['processDataSet']['processInformation']['dataSetInformation']['common:generalComment'][1]['#text']='按湿产品作分母。'
        changed=after['processDataSet']['exchanges']['exchange'][0];changed.update(meanAmount='55',exchangeDirection='Input')
        revised=self.home/'revised.json';revised.write_text(json.dumps(after))
        other=self.home/'unreadable.json';other.write_text('{')
        prior=self.packet(None,previous);current=self.packet(None,revised)
        current['artifact_refs'].append({'id':str(other),**self.ref(other)})
        report=review_delta.compare(self.root,prior,current)
        subject,=report['changes']
        self.assertTrue(report['unreadable_json_artifacts']['new'])
        text,=[row for row in subject['fields'] if row['pointer'].endswith('generalComment')]
        self.assertEqual(text['changed_languages'],['zh'])
        self.assertIn('干产品基准',json.dumps(text,ensure_ascii=False))
        self.assertIn('按湿产品作分母',json.dumps(text,ensure_ascii=False))
        exchange,=subject['exchange_diff']['changes']
        self.assertEqual(exchange['after']['mean_amount'],'55')
        self.assertEqual(exchange['after']['direction'],'Input')
        self.assertEqual(exchange['before']['mean_amount'],previous_exchanges[0]['meanAmount'])

        # A one-language spelling correction remains a field fact without a
        # quantity change or a programmatic judgment about adoption.
        wording=before['processDataSet']['processInformation']['dataSetInformation']['common:generalComment'][0]
        wording['#text']='Castng scenario.';previous.write_text(json.dumps(before))
        prior=self.packet(None,previous)
        wording['#text']='Casting scenario.';revised.write_text(json.dumps(before))
        spelling,=review_delta.compare(self.root,prior,self.packet(None,revised))['changes']
        self.assertEqual(len(spelling['fields']),1)
        self.assertEqual(spelling['fields'][0]['changed_languages'],['en'])
        self.assertEqual(spelling['exchange_diff']['changes'],[])

    def test_named_frozen_scientific_cases_keep_calculations_and_wording_distinct(self):
        from decimal import Decimal
        from lca_gate_projection import declared_process_basis, tiangong_self_uri
        material=APPLE/'material-dispatch-20261115'
        salts=material/'program-38e55011e83b6769/reviews'
        rounds=['5f241439b6f783dbf8db0306058fb7b0','3a314bf5d32fb447a04bc298199c5f25','15d1dbb4fc771c38902bcd427a8dd0dc']
        if not (salts/('program-review:'+rounds[0])).exists():self.skipTest('Pinned SALTS originals are unavailable')
        packets=[json.loads((salts/('program-review:'+ident)/'packet.json').read_bytes()) for ident in rounds]
        for before,after in ((packets[0],packets[1]),(packets[0],packets[2]),(packets[1],packets[2])):
            report=review_delta.compare(ROOT,before,after)
            self.assertFalse(report['alignment_gaps'])
            changed=next(x for x in report['changes'] if x['new_identity'][0]=='process')
            self.assertEqual(changed['self_uri']['after']['status'],'matches')
            self.assertTrue(any(row.get('changed_languages') for row in changed['fields']))
            self.assertTrue(report['known_consumers']['new'])
        retained=review_delta.packet_bodies(ROOT,packets[-1])[0]
        record=next(item['body']['processDataSet'] for key,item in retained.items() if key[0]=='process')
        basis=record['processInformation']['quantitativeReference']['functionalUnitOrOther']
        self.assertIn('收货态',json.dumps(basis,ensure_ascii=False))
        self.assertIn('**干**黑粉',json.dumps(record['exchanges']['exchange'][0]['generalComment'],ensure_ascii=False))
        al=APPLE/'program-674929badbf1354a/work/r42-snapshot-20261118'
        path=next(al.glob('process-3f869*.json'))
        key,item=next(iter(review_delta.packet_bodies(ROOT,{'artifact_refs':[self.ref(path)]})[0].items()))
        process=item['body']['processDataSet']
        self.assertIn('Zn 5.5 kg',json.dumps(declared_process_basis(process,key)['title']))
        zinc=next(x for x in process['exchanges']['exchange'] if str(x['@dataSetInternalID'])=='2')
        self.assertEqual(Decimal('5.5')/100*Decimal('1000'),Decimal(zinc['meanAmount']))
        self.assertEqual(tiangong_self_uri('https://example.invalid/dataset/'+key[1],*key)['status'],'not_an_exact_tiangong_locator')
        index=json.loads((material/'ls-review-f0f1423e70c2ad2fc546473f4ce69258-1422.json').read_bytes())
        note=Path(index['management_return']['result_ref']['path']).read_text()
        self.assertIn('1648.816+铁红27385.629+FeCl₂442.149',note)
        self.assertEqual(sum(map(Decimal,('1648.816','27385.629','442.149'))),Decimal('29476.594'))

    def test_a02_original_readbacks_follow_the_frozen_request_cutoff(self):
        from a02_review_verifier import A02_PACKET, read_exact, verify
        from review_facts import collect
        if not (ROOT/A02_PACKET).is_file() or not (ROOT/'.control/harness.sqlite').is_file():
            self.skipTest('Pinned A02 originals and work ledger are required')
        review = tasks.read_request(ROOT, Path(A02_PACKET).parent.name)
        raw = read_exact(review['input_ref'])
        parent = tasks.read_request(ROOT, review['parent_request_id'])
        packet = json.loads(raw)
        at_request = collect(ROOT, parent, packet, review['created_at'])
        self.assertEqual(verify(at_request), 'A02-facts-verified')
        later = collect(ROOT, parent, packet, '2026-11-19T16:38:24.000000+00:00')
        self.assertEqual([p['readback']['status'] for p in later['processes']], ['complete_differs']*2)
        self.assertEqual([len(p['readback']['changed_pointers']) for p in later['processes']], [3, 2])
        for initial, current in zip(at_request['processes'], later['processes']):
            self.assertEqual(initial['frozen_ref'], current['frozen_ref'])
            self.assertEqual(initial['exchanges'], current['exchanges'])
            self.assertNotEqual(initial['readback']['body_ref']['sha256'], current['readback']['body_ref']['sha256'])
        self.assertEqual(read_exact(review['input_ref']), raw)

