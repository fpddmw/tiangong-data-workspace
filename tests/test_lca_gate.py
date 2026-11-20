import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin'))
import lca_gate

PID='f83f7f6c-cd88-5b4a-8263-a3d177b2f663'
FID='c6dddd18-b089-41c7-b881-1fd6d546f0db'

def process(amount='1'):
    return {'processDataSet':{'processInformation':{'dataSetInformation':{'common:UUID':PID}},
        'administrativeInformation':{'publicationAndOwnership':{'common:dataSetVersion':'00.00.001'}},
        'exchanges':{'exchange':[{'@dataSetInternalID':'1','meanAmount':amount,'resultingAmount':amount,
           'exchangeDirection':'Output','referenceToFlowDataSet':{'@type':'flow data set','@refObjectId':FID,'@version':'00.00.001'}}]}}}

class NativeChecksTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.rows=self.root/'process.json';self.rows.write_text(json.dumps(process()))
        self.args=argparse.Namespace(package=self.root,rows=self.rows,out_dir=None,retry=False,check_derived=None,
                                    support_rows=[],reference_rows_file=[],tidas_dir=None)
        self.calls=[]

    def native(self,args,role='research'):
        self.calls.append(args)
        body=json.loads(Path(args[args.index('--input')+1]).read_text())
        self.assertIn('processDataSet',body)
        result={'schemaVersion':1,'status':'completed','outcome':'validation_passed','error':None,
                'data':{'validation':{'ok':True,'notChecked':['remote_references']}},
                'completeness':{'scope':'local_validation','notChecked':['remote_references']}}
        Path(args[args.index('--output')+1]).write_text(json.dumps(result['data']['validation']))
        return subprocess.CompletedProcess(args,0,json.dumps(result),'')

    def run_gate(self,native=None,runtime='fixture'):
        out=io.StringIO()
        with patch.object(lca_gate,'runtime',return_value={'status':'available','identity':runtime}),patch.object(lca_gate,'invoke',side_effect=native or self.native),contextlib.redirect_stdout(out):
            code=lca_gate.gate(self.args)
        self.response=json.loads(out.getvalue());self.report=Path(self.response['report']) if 'report' in self.response else None
        return code,json.loads(self.report.read_text()) if self.report else self.response

    def test_canonical_input_and_native_coverage_are_preserved(self):
        before=self.rows.read_bytes();code,report=self.run_gate()
        self.assertEqual(code,0);self.assertEqual(report['outcome'],'warning')
        self.assertEqual(self.calls[0][:3],['dataset','process','validate'])
        self.assertEqual(report['checks'][0]['not_checked'],['remote_references'])
        self.assertTrue(any('mass balance' in item for item in report['not_checked']))
        self.assertEqual(before,self.rows.read_bytes())
        self.assertIn('resultingAmount',Path(self.response['derived']).read_text())

    def test_unchanged_input_reuses_report_and_changed_runtime_does_not(self):
        self.run_gate();old=self.report
        self.run_gate();self.assertEqual(len(self.calls),1);self.assertTrue(self.response['reused'])
        self.run_gate(runtime='new');self.assertEqual(len(self.calls),2);self.assertNotEqual(old,self.report)

    def test_corrupt_report_is_not_silently_reused_or_overwritten(self):
        self.run_gate();self.report.with_name('candidate-derived.md').write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed original report'):self.run_gate()
        self.args.retry=True;self.run_gate();self.assertEqual(len(self.calls),2)

    def test_successful_invocation_with_invalid_content_is_a_failure(self):
        def invalid(args,role='research'):
            r=self.native(args);d=json.loads(r.stdout);d['data']['validation']['ok']=False
            return subprocess.CompletedProcess(args,0,json.dumps(d),'')
        code,report=self.run_gate(invalid);self.assertEqual(code,1);self.assertEqual(report['checks'][0]['status'],'failed')

    def test_package_validator_failure_is_not_a_successful_local_check(self):
        result=subprocess.CompletedProcess([],0,json.dumps({'ok':False,'summary':{'failed_count':1}}),'')
        with patch.object(lca_gate,'invoke',return_value=result):
            report=lca_gate.run_check('package',['validation','run'],self.root,self.root/'package.json')
        self.assertEqual(report['status'],'failed')

    def test_checks_carry_standing_upstream_boundaries(self):
        _,report=self.run_gate()
        notes=report['checks'][0]['upstream_boundary_notes']
        issues={row['issue'] for row in notes}
        self.assertIn('tiangong-lca/cli#425',issues)
        self.assertIn('tiangong-lca/cli#504',issues)
        self.assertNotIn('tiangong-lca/cli#505',issues)
        self.assertTrue(all(set(row)=={'boundary','issue','summary'} for row in notes))

    def test_package_check_notes_cover_model_and_quantity_boundaries(self):
        result=subprocess.CompletedProcess([],0,json.dumps({'ok':True,'summary':{'failed_count':0}}),'')
        with patch.object(lca_gate,'invoke',return_value=result):
            report=lca_gate.run_check('package',['validation','run'],self.root,self.root/'package.json')
        issues={row['issue'] for row in report['upstream_boundary_notes']}
        self.assertEqual(issues,{'tiangong-lca/cli#504','tiangong-lca/cli#505','tiangong-lca/cli#425'})

    def test_upstream_notes_match_name_kinds_defensively(self):
        self.assertEqual({row['issue'] for row in lca_gate.upstream_notes('001-process')},
                         {'tiangong-lca/cli#504','tiangong-lca/cli#425'})
        self.assertEqual({row['issue'] for row in lca_gate.upstream_notes('002-model')},
                         {'tiangong-lca/cli#504','tiangong-lca/cli#505'})
        self.assertEqual({row['issue'] for row in lca_gate.upstream_notes('003-flow')},
                         {'tiangong-lca/cli#504'})
        self.assertEqual({row['issue'] for row in lca_gate.upstream_notes('package')},
                         {'tiangong-lca/cli#504','tiangong-lca/cli#505','tiangong-lca/cli#425'})

    def test_input_mutation_during_check_is_reported_and_original_copy_remains(self):
        def mutate(args,role='research'):
            result=self.native(args);self.rows.write_text(json.dumps(process('2')));return result
        code,report=self.run_gate(mutate);self.assertEqual(code,1);self.assertEqual(report['outcome'],'input_changed')
        saved=json.loads(self.report.with_name(report['checks'][0]['canonical_input']).read_text())
        self.assertEqual(saved['processDataSet']['exchanges']['exchange'][0]['meanAmount'],'1')

    def test_row_envelope_is_extracted_without_mutating_original(self):
        self.rows.write_text(json.dumps({'rows':[{'json_ordered':process()}]}))
        self.assertEqual(self.run_gate()[0],0)

    def test_response_envelope_is_not_silently_treated_as_empty_success(self):
        self.rows.write_text(json.dumps({'data':{'content':process()}}))
        with self.assertRaisesRegex(ValueError,'canonical body'):self.run_gate()

    def test_changed_amount_is_visible_without_native_reexecution(self):
        self.run_gate();self.args.check_derived=self.report
        self.rows.write_text(json.dumps(process('2')))
        code,result=self.run_gate();self.assertEqual(code,1);self.assertEqual(result['outcome'],'stale')
        self.assertEqual(len(self.calls),1)
        changes=next(iter(result['exchange_changes'].values()))['changed']
        self.assertEqual(changes[0]['after']['resulting_amount'],'2')

    def test_comparison_reads_old_report_without_current_envelope_or_presentation_digest(self):
        self.run_gate()
        report=self.report
        doc=json.loads(report.read_text());doc['schema']='historical-report'
        report.write_text(json.dumps(doc))
        (report.parent/'candidate-derived.md').write_text('Edited presentation; scientific input unchanged.')
        self.args.check_derived=report
        code,result=self.run_gate()
        self.assertEqual(code,0);self.assertEqual(result['outcome'],'current')
        self.assertEqual(len(self.calls),1)

    def test_selected_validation_runs_when_runtime_description_is_unavailable(self):
        out=io.StringIO()
        with patch.object(lca_gate,'runtime',return_value={'status':'unavailable'}),patch.object(lca_gate,'invoke',side_effect=self.native),contextlib.redirect_stdout(out):
            self.assertEqual(lca_gate.gate(self.args),0)
        self.assertEqual(len(self.calls),1)

    def test_text_only_change_invalidates_report_without_inventing_amount_change(self):
        self.run_gate();self.args.check_derived=self.report
        body=process();body['processDataSet']['comment']='revised scientific boundary';self.rows.write_text(json.dumps(body))
        code,result=self.run_gate();self.assertEqual(code,1);self.assertEqual(result['exchange_changes'],{})

    def test_summary_keeps_field_locator_and_full_bilingual_evidence(self):
        baseline = self.root/'frozen.json'
        baseline.write_text(self.rows.read_text())
        self.args.baseline_rows = baseline
        body = process('2')
        body['processDataSet']['comment'] = [
            {'@xml:lang': 'en', '#text': 'Current mass basis ' * 4000},
            {'@xml:lang': 'zh', '#text': '本次采用的质量基准'},
        ]
        self.rows.write_text(json.dumps(body))
        code, report = self.run_gate()
        self.assertEqual(code, 0)
        summary = Path(self.response['summary']).read_text()
        full = Path(self.response['derived']).read_text()
        self.assertIn('/processDataSet/comment', summary)
        self.assertIn('| 1 | Output | 2 | 2 |', summary)
        self.assertIn('candidate-derived.md', summary)
        self.assertNotIn('Current mass basis', summary)
        self.assertIn('本次采用的质量基准', full)
        self.assertIn('Current mass basis ' * 4000, full)
        self.assertIn('candidate-summary.md', report['artifacts'])

    def test_exact_local_references_are_passed_as_public_documents(self):
        ref=self.root/'reference-rows.json';ref.write_text(json.dumps([{'flowDataSet':{
            'flowInformation':{'dataSetInformation':{'common:UUID':FID}},
            'administrativeInformation':{'publicationAndOwnership':{'common:dataSetVersion':'00.00.001'}}}}]))
        self.args.reference_rows_file=[ref];_,report=self.run_gate()
        self.assertIn('--reference-documents',self.calls[0])
        projection=json.loads(self.report.with_name('candidate-derived.json').read_text())
        self.assertIn('flow/'+FID+'/00.00.001',projection['reference_inventory'])

    def test_old_neighbor_manifest_does_not_override_explicit_current_reference(self):
        ref=self.root/'reference-rows.json'
        ref.write_text(json.dumps([{'flowDataSet':{
            'flowInformation':{'dataSetInformation':{'common:UUID':FID}},
            'administrativeInformation':{'publicationAndOwnership':{'common:dataSetVersion':'00.00.001'}}}}]))
        (self.root/'manifest.json').write_text(json.dumps({'files':[{'path':ref.name,'sha256':'0'*64}]}))
        self.args.reference_rows_file=[ref]
        self.assertEqual(self.run_gate()[0],0)
        self.assertIn('--reference-documents',self.calls[0])

if __name__=='__main__':unittest.main()
