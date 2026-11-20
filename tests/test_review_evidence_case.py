"""Frozen exact evidence must not turn source counts into review qualification."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

CASE = Path(__file__).resolve().parents[1] / 'materials/packages/four-agent-business-flow-20261029/adapters/qualification'
SPEC = importlib.util.spec_from_file_location('qualification_case', CASE / 'verify_evidence.py')
case = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(case)


class EvidenceCaseTests(unittest.TestCase):
    def test_actual_facts_keep_native_and_scientific_unknowns(self):
        facts = case.verify()
        self.assertTrue(facts['process']['body_and_metadata_stable'])
        self.assertTrue(facts['process']['diagnostic_hash_matches_current_json'])
        self.assertEqual(facts['process']['state_code'], 20)
        self.assertFalse(facts['process']['stored_submitted_checksum_observed'])
        self.assertFalse(facts['process']['json_ordered_column_presence_or_parity_observed'])
        self.assertEqual(facts['scope']['unique_direct_targets'], 20)
        self.assertEqual(len(facts['scope']['fresh_flow_reads']), 6)
        self.assertEqual(facts['scope']['native_reference_graph_rows'], [])
        self.assertFalse(facts['scope']['server_complete_affected_scope_verified'])
        self.assertTrue(facts['experts']['logical_page_coverage_verified'])
        self.assertTrue(facts['experts']['all_three_current_members'])
        self.assertFalse(facts['experts']['professional_eligibility_verified'])
        self.assertFalse(facts['experts']['conflict_clearance_verified'])

    def test_bound_input_change_is_not_silently_rebaselined(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            bindings = json.loads((CASE / 'input-bindings.json').read_text())
            (root / 'input-bindings.json').write_text(json.dumps(bindings))
            (root / bindings['files'][0]['path']).write_text('{"altered": true}')
            with self.assertRaisesRegex(ValueError, 'Changed evidence'):
                case.verify(root)

    def test_versionless_uri_is_not_invented_as_conflict(self):
        result = case.references({'@refObjectId': 'object', '@type': 'source data set',
                                  '@version': '01.01.001', '@uri': '../sources/object.xml'})
        self.assertFalse(result[0]['explicit_uri_version_conflict'])
        result = case.references({'@refObjectId': 'object', '@type': 'source data set',
                                  '@version': '01.01.001', '@uri': '../sources/object_01.01.000.xml'})
        self.assertTrue(result[0]['explicit_uri_version_conflict'])
        self.assertEqual(result[0]['uri_version'], '01.01.000')


if __name__ == '__main__':
    unittest.main()
