"""Launch the selected upstream Utilities executable; it owns command semantics."""
import os
from pathlib import Path
import shutil
import subprocess


def prepare(root, argv):
    selected = os.environ.get('TIANGONG_UTILITIES_BIN', 'lcia-auditor')
    executable = shutil.which(selected)
    if not executable:
        raise ValueError('Utilities is not installed: install the upstream lcia-auditor entry or select it with TIANGONG_UTILITIES_BIN.')
    # Offline diagnostics receive no ambient account or database credentials.
    # An explicitly authorized connected operation uses its upstream credential interface.
    env = {k: v for k, v in os.environ.items() if k in
           {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT'}}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    return [executable, *argv], env, Path(root)


def run(root, argv):
    command, env, cwd = prepare(root, argv)
    return subprocess.call(command, env=env, cwd=cwd)
