"""Unit tests for machine-local onboarding templates, setup_local.py and accounts localization."""
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parents[1] / 'bin'
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import env_run
import setup_local


class SetupLocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / '.control').mkdir(parents=True)
        (self.root / '.env.example').write_text('TIANGONG_LCA_API_BASE_URL=\nDATA_EDITOR_SESSION_FILE=\n', encoding='utf-8')
        (self.root / '.control/runtime.template.json').write_text(json.dumps({
            'schema': 'data-workspace.launchers.v1',
            'node': '.toolchain/node/bin/node',
            'cli': '.toolchain/cli/bin/tiangong-lca.js',
            'external_materials': {'ef31_source_package': ''},
        }), encoding='utf-8')
        (self.root / '.control/accounts.template.json').write_text(json.dumps({
            'project_ref': '<supabase-project-ref>',
            'editor': {'user_id': '<your-editor-user-uuid>'},
        }), encoding='utf-8')

    def test_init_creates_missing_local_files_with_0600_env_and_preserves_existing(self):
        result = setup_local.init_local(self.root)
        created_paths = {item['path'] for item in result['created']}
        self.assertEqual(created_paths, {'.env', '.control/runtime.local.json', '.control/accounts.local.json'})
        self.assertEqual(stat.S_IMODE((self.root / '.env').stat().st_mode), 0o600)

        # Second init must never overwrite existing files
        (self.root / '.control/accounts.local.json').write_text('{"custom": true}\n', encoding='utf-8')
        second = setup_local.init_local(self.root)
        self.assertEqual(second['created'], [])
        self.assertEqual(json.loads((self.root / '.control/accounts.local.json').read_text()), {'custom': True})

    def test_init_respects_existing_legacy_runtime_and_accounts_files(self):
        (self.root / '.control/runtime.json').write_text('{"legacy": true}\n', encoding='utf-8')
        (self.root / '.control/accounts.json').write_text('{"legacy": true}\n', encoding='utf-8')
        result = setup_local.init_local(self.root)
        self.assertEqual([item['path'] for item in result['created']], ['.env'])
        self.assertFalse((self.root / '.control/runtime.local.json').exists())
        self.assertFalse((self.root / '.control/accounts.local.json').exists())

    def test_accounts_local_overrides_legacy_and_external_material_resolves(self):
        (self.root / '.control/accounts.json').write_text(json.dumps({
            'project_ref': 'legacy-proj', 'editor': {'user_id': 'legacy-user'}
        }), encoding='utf-8')
        (self.root / '.control/accounts.local.json').write_text(json.dumps({
            'project_ref': 'local-proj', 'editor': {'user_id': 'local-user'}
        }), encoding='utf-8')
        env = env_run.role_env('editor', {
            'DATA_EDITOR_SESSION_FILE': '/tmp/session.json',
            'TIANGONG_LCA_API_BASE_URL': 'https://example.invalid',
        }, self.root)
        self.assertEqual(env['TIANGONG_LCA_EXPECTED_PROJECT_REF'], 'local-proj')
        self.assertEqual(env['TIANGONG_LCA_EXPECTED_USER_ID'], 'local-user')

        ext_dir = self.root / 'external-ef31'
        ext_dir.mkdir()
        (self.root / '.control/runtime.local.json').write_text(json.dumps({
            'node': 'bin/node', 'cli': 'bin/cli.js',
            'external_materials': {'ef31_source_package': str(ext_dir)},
        }), encoding='utf-8')
        self.assertEqual(env_run.external_material_path(self.root, 'ef31_source_package'), ext_dir)
        with self.assertRaises(ValueError):
            env_run.external_material_path(self.root, 'missing_pack')

    def test_doctor_reports_ready_and_never_leaks_secrets(self):
        secret = 'super-secret-value-never-print-987654'
        env_path = self.root / '.env'
        env_path.write_text(
            f'TIANGONG_LCA_API_BASE_URL=https://api.example.invalid\n'
            f'DATA_EDITOR_SESSION_FILE=/tmp/editor-session.json\n'
            f'S3_SECRET_ACCESS_KEY={secret}\n',
            encoding='utf-8',
        )
        os.chmod(env_path, 0o600)

        node_bin = self.root / '.toolchain/node/bin/node'
        cli_bin = self.root / '.toolchain/cli/bin/tiangong-lca.js'
        manifest = self.root / 'runtime-manifests/foundry.json'
        for p, body in ((node_bin, b'node'), (cli_bin, b'cli'), (manifest, b'{"ok":true}')):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)

        (self.root / '.control/runtime.local.json').write_text(json.dumps({
            'node': '.toolchain/node/bin/node',
            'cli': '.toolchain/cli/bin/tiangong-lca.js',
            'manifest': 'runtime-manifests/foundry.json',
            'manifest_sha256': hashlib.sha256(b'{"ok":true}').hexdigest(),
        }), encoding='utf-8')
        (self.root / '.control/accounts.local.json').write_text(json.dumps({
            'project_ref': 'proj-123',
            'editor': {'user_id': '11111111-1111-1111-1111-111111111111'},
        }), encoding='utf-8')

        skill_file = self.root / '.agents/skills/scripts/run-cli.mjs'
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_bytes(b'export default 1;\n')
        os.chmod(skill_file, 0o755)
        (self.root / 'skills-lock.json').write_text(json.dumps({
            'schema': 'data-workspace.skills-lock.v1',
            'source': {'repository': 'tiangong-lca/agent-skills', 'commit': 'abc'},
            'skills': [],
            'support_files': [{
                'path': '.agents/skills/scripts/run-cli.mjs',
                'sha256': hashlib.sha256(b'export default 1;\n').hexdigest(),
                'mode': '0o755',
            }],
        }), encoding='utf-8')

        report = setup_local.doctor(self.root)
        serialized = json.dumps(report)
        self.assertTrue(report['ok'], serialized)
        self.assertNotIn(secret, serialized)
        self.assertEqual(report['env']['configured_roles'], ['editor'])
        self.assertEqual(report['runtime']['status'], 'ready')
        self.assertEqual(report['accounts']['status'], 'ready')
        self.assertEqual(report['skills']['status'], 'lock_verified')

        # Insecure permissions on .env must be flagged
        os.chmod(env_path, 0o644)
        bad = setup_local.doctor(self.root)
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['env']['status'], 'insecure_permissions')


if __name__ == '__main__':
    unittest.main()
