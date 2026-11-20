"""The host receives a complete short entry or an explicit full-file read route."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_adapter import CodexAdapter


class InstructionEntryTests(unittest.TestCase):
    def adapter(self, root, role='executor'):
        adapter = CodexAdapter.__new__(CodexAdapter)
        adapter.root = root
        adapter.cwd = str(root)
        adapter.config = {'programmatic': True, 'role': role, 'model': 'fixture-model',
                          'provider': {'codex_model_provider': 'fixture-provider'}}
        adapter.host = object()
        adapter.native_policy = {'sandbox': 'read-only', 'network_access': False}
        return adapter

    def params(self, adapter):
        with patch('codex_adapter.mcp_inventory', return_value=[]):
            return adapter._params()

    def test_short_entry_and_responsibility_paths_are_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'AGENTS.md').write_text('Read the complete linked role guide.\n')
            for role, guide, duty in [('executor', 'data.md', 'Deliver the complete'),
                                       ('scientific_reviewer', 'reviewer.md', 'Assess the frozen candidate')]:
                params = self.params(self.adapter(root, role))
                self.assertEqual(params['config']['project_doc_max_bytes'], 32768)
                self.assertTrue(params['baseInstructions'].startswith(duty))
                self.assertIn(str(root / 'roles' / guide), params['baseInstructions'])
                self.assertIn(str(root / 'workflows/business-flow/README.md'), params['baseInstructions'])
                self.assertTrue(params['config']['features']['hooks'])

    def test_oversized_utf8_entry_cannot_be_presented_as_complete_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tail = '允许保存草稿，但尚未授权正式送审。'
            entry = root / 'AGENTS.md'
            entry.write_text('原指示。' * 5000 + tail)
            self.assertGreater(entry.stat().st_size, 32768)
            params = self.params(self.adapter(root))
            self.assertEqual(params['config']['project_doc_max_bytes'], 0)
            self.assertIn(str(entry), params['baseInstructions'])
            self.assertIn('Read the complete entry', params['baseInstructions'])
            self.assertTrue(entry.read_text().endswith(tail))

    def test_formal_workspace_entry_fits_without_increasing_budget(self):
        root = Path(__file__).resolve().parents[1]
        self.assertLessEqual((root / 'AGENTS.md').stat().st_size, 32768)
        self.assertEqual(self.params(self.adapter(root))['config']['project_doc_max_bytes'], 32768)


if __name__ == '__main__':
    unittest.main()
