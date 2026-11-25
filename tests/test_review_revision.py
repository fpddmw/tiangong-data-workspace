"""Exercise original review ownership, queue withdrawal, and focused frozen inputs."""
import contextlib
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_base import identity
from scientific_review.author_tools import ReviewService
import dsh_capacity as capacity
from execution_lease import atomic_json
import harness_tasks as tasks
from scientific_review import delta as review_delta
import review_fixtures as data
from scientific_review import queue as review_queue
import run_worker
import test_agent_runtime as fixture


class ReviewRevisionTests(unittest.TestCase):
    setUp=fixture.RuntimeTests.setUp
    create_request=fixture.RuntimeTests.create_request
    ref=fixture.RuntimeTests.ref

    def author(self):
        native=identity('deepseek_harness','session','fixture-author',self.root,'1')
        launch = data.save(self.home/'author-launch.json', {'run_id':'author-run', 'request_id':'request',
            'native_cwd':str(self.root), 'work_home':str(self.home)})
        self.run_intent('author-run','request', launch)
        with tasks.transaction(self.root) as c:
            c.execute('UPDATE work_runs SET native_identity_json=?,outcome=\'running\' WHERE id=\'author-run\'',(tasks._json(native),))
        return native

    def run_intent(self, run_id, request_id, *extra_refs):
        return tasks.mutate(self.root,'run-intent',self.actor,{'id':run_id,'request_id':request_id,
            'config_ref':self.ref(self.config_path),'input_refs':[self.input_ref,*extra_refs],
            'target_host':'deepseek_harness','operation':'start'})

    def candidate(self, name, **fields):
        path = self.home / (name + '.json')
        data.save(path, data.process(**fields))
        return path

    def packet(self, native, path, **fields):
        return {'author_run_id':'author-run','authors':[native],
                'review_scope':'Review original frozen fixture','artifact_refs':[{'id':str(path),**self.ref(path)}],
                'evidence_refs':[self.input_ref,self.ref(path)],**fields}

    def register(self, parent, packet):
        return run_worker._program_review_request(self.root,SimpleNamespace(actor=self.actor),parent,packet)

    def test_replacement_retires_only_the_named_queued_review(self):
        native=self.author();parent=tasks.read_request(self.root,'request')
        previous=self.candidate('previous')
        revised=self.candidate('revised', amount='0.250')
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
        with self.assertRaisesRegex(ValueError,'no complete original opinion'):
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
        old=self.register(parent,self.packet(native,self.candidate('previous')))
        new=self.register(parent,self.packet(native,self.candidate('revised', amount='0.250'),supersedes_review_id=old['id']))
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
        previous=self.candidate('before')
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

    def test_frozen_versions_keep_calculation_language_and_consumers_distinct(self):
        from dataset_checks.projection import tiangong_self_uri
        old_content, new_content = data.revision_documents()
        before, after = self.home/'before.json', self.home/'after.json'
        data.save(before, old_content)
        data.save(after, new_content)
        report = review_delta.compare(self.root, self.packet(None, before), self.packet(None, after))
        self.assertFalse(report['alignment_gaps'])
        self.assertEqual(len(report['changes']), 2)
        process = next(item for item in report['changes'] if item['new_identity'][0] == 'process')
        flow = next(item for item in report['changes'] if item['new_identity'][0] == 'flow')
        self.assertEqual(flow['old_identity'][2], data.VERSION)
        self.assertEqual(flow['new_identity'][2], '00.00.002')
        self.assertEqual(process['self_uri']['before']['status'], 'differs')
        self.assertEqual(process['self_uri']['after']['status'], 'matches')
        self.assertTrue(any(item.get('changed_languages') == ['zh'] for item in process['fields']))
        qty = next(row for row in process['exchange_diff']['changes'] if row['exchange_id'] == '2')
        self.assertEqual(Decimal(qty['before']['resulting_amount']), Decimal(125)/1000)
        self.assertEqual(Decimal(qty['after']['resulting_amount']), Decimal(250)/1000)
        self.assertEqual(process['reference_basis']['after']['reference_exchanges'][0]['resulting_amount'], '1')
        self.assertTrue(any(item['target'] == flow['new_identity'] for item in report['known_consumers']['new']))
        uri = 'https://example.invalid/dataset/' + data.PROCESS_ID
        self.assertEqual(tiangong_self_uri(uri, *process['new_identity'])['status'], 'not_an_exact_tiangong_locator')

    def test_frozen_review_readbacks_follow_the_request_cutoff(self):
        from scientific_review.facts import collect
        native = self.author()
        parent = tasks.read_request(self.root, 'request')
        started = datetime.fromisoformat(tasks.read_run(self.root, 'author-run')['created_at'])
        archive = self.home/'runs/author-run/delivery'
        sent = (started + timedelta(seconds=2)).isoformat()
        early = data.readback(archive, 'before', run_id='author-run', request_id='request',
                              finished_at=(started + timedelta(seconds=1)).isoformat(), body=data.process())
        late_time = (started + timedelta(seconds=3)).isoformat()
        late = data.readback(archive, 'after', run_id='author-run', request_id='request',
                             finished_at=late_time, body=data.process(amount='0.250'))
        with patch('run_worker.runtime.now', return_value=sent):
            review = self.register(parent, self.packet(native, self.candidate('original')))
        raw = Path(review['input_ref']['path']).read_bytes()
        packet = json.loads(raw)
        facts_raw = Path(packet['facts_ref']['path']).read_bytes()
        facts = json.loads(facts_raw)
        self.assertEqual(facts['as_of'], sent)
        opening, = facts['processes']
        self.assertEqual(opening['readback']['status'], 'complete_match')
        self.assertEqual(opening['readback']['body_ref'], early)
        later, = collect(self.root, parent, packet, late_time)['processes']
        self.assertEqual(later['readback']['status'], 'complete_differs')
        self.assertEqual(later['readback']['body_ref'], late)
        self.assertEqual(later['readback']['changed_pointers'], [
            '/processDataSet/exchanges/exchange/1/meanAmount',
            '/processDataSet/exchanges/exchange/1/resultingAmount',
        ])
        self.assertEqual(later['frozen_ref'], opening['frozen_ref'])
        self.assertEqual(later['exchanges'], opening['exchanges'])
        self.assertEqual(Path(packet['facts_ref']['path']).read_bytes(), facts_raw)
        self.assertEqual(Path(review['input_ref']['path']).read_bytes(), raw)

