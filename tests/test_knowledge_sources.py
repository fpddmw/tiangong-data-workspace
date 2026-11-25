"""Historical lookup keeps readable originals independent of missing catalogs."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from workspace_queries import historical_methods as knowledge


class HistoricalKnowledgeTests(unittest.TestCase):
    def test_document_is_readable_without_old_database_and_after_path_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'arbitrary-original-location'; archive.mkdir()
            original = archive / 'method.md'; original.write_text('A useful quantity method')
            alias = archive / 'alias.md'; alias.symlink_to(original)
            recovery = root / 'recovery.json'
            recovery.write_text(json.dumps({'source_root': str(root),
                'retired_source_root': str(archive), 'source_database': str(root/'missing/catalog.db')}))
            manifest = {'knowledge_sources': {'htam': {
                'recovery_ref': {'path': str(recovery), 'sha256': 'old recorded bytes'},
                'documents': [{'id': 'method', 'path': 'alias.md', 'sha256': 'previous document bytes'}]}}}
            result = knowledge.discover(root, method_id='method', manifest=manifest)
            self.assertEqual(result['items'][0]['method_ref']['path'], str(original.resolve()))
            self.assertFalse(result['items'][0]['matches_recorded_bytes'])
            self.assertIn('Original catalog unavailable', str(result['source_issues']))

    def test_missing_recovery_does_not_hide_an_explicit_absolute_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'method.md'; original.write_text('original')
            manifest = {'knowledge_sources': {'htam': {
                'recovery_ref': {'path': 'absent.json'},
                'documents': [{'id': 'method', 'path': str(original)}]}}}
            result = knowledge.discover(root, method_id='method', manifest=manifest)
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['items'][0]['method_ref']['sha256'], hashlib.sha256(b'original').hexdigest())

    def test_explicit_document_cannot_expose_a_credential_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            folder = root / '.secrets'; folder.mkdir()
            original = folder / 'session.json'; original.write_text('{}')
            manifest = {'knowledge_sources': {'htam': {'documents': [{'id': 'secret', 'path': str(original)}]}}}
            result = knowledge.discover(root, method_id='secret', manifest=manifest)
            self.assertEqual(result['items'], [])
            self.assertIn('credentials', str(result['source_issues']))
