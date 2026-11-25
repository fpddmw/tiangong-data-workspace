import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class RepositoryLinks(unittest.TestCase):
    def test_full_inventory_commit_and_working_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
            git('init', '-q')
            (root/'archive').mkdir()
            (root/'archive'/'incoming.md').write_text('[old][one]\n\n[one]: ../guide.md#first-1\n')
            (root/'guide.md').write_text('# First\n# First\n\n[old](missing.md)\n\n```\n[code](ignored)\n```\n')
            git('add', '.')
            git('-c','user.name=Test','-c','user.email=local@example.invalid','commit','-qm','original')
            def audit(name, *args):
                path = root/(name+'.json')
                cmd = [sys.executable, str(Path(__file__).with_name('check_links.py')), '--root', str(root),
                       '--report', str(path), *map(str, args)]
                result = subprocess.run(cmd, capture_output=True, text=True)
                self.assertIn(result.returncode, [0, 1], result.stderr)
                return result.returncode, json.loads(path.read_text())
            status, first = audit('base','--revision','HEAD')
            self.assertEqual((status, first['documents'], first['unresolved']), (1, 2, 1))
            (root/'guide.md').write_text('# First\n')
            (root/'new.md').write_text(f'[absolute]({root.as_uri()}/gone.md)\n')
            status, working = audit('changed', '--baseline-report', root/'base.json')
            self.assertEqual((status, working['new_findings'], working['resolved']), (1, 2, 1))
            self.assertEqual({r[0] for r in working['findings']}, {'archive/incoming.md', 'new.md'})
            self.assertEqual(audit('frozen','--revision','HEAD','--baseline-report',root/'base.json')[0],0)
