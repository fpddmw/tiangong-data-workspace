import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import utilities_tools


class UtilitiesLaunchTests(unittest.TestCase):
    def test_forwards_upstream_arguments_without_old_machine_or_metadata_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'lcia-auditor'
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
            args = ['new-upstream-command', '--new-option', 'input.json']
            with patch.dict(os.environ, {'TIANGONG_UTILITIES_BIN': str(binary), 'CONN': 'private', 'TOKEN': 'private'}):
                command, env, cwd = utilities_tools.prepare(root, args)
            self.assertEqual(command, [str(binary), *args])
            self.assertEqual(cwd, root)
            self.assertNotIn('CONN', env)
            self.assertNotIn('TOKEN', env)

    def test_missing_installation_reports_only_selected_tool(self):
        with patch.dict(os.environ, {'TIANGONG_UTILITIES_BIN': '/absent/lcia-auditor'}):
            with self.assertRaisesRegex(ValueError, 'Utilities is not installed'):
                utilities_tools.prepare(Path('.'), ['--help'])
