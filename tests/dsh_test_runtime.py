"""Explicit installation binding for optional native DSH qualification."""
import json
import os
from pathlib import Path
import shutil
import unittest


requires_dsh = unittest.skipUnless(os.environ.get('HTAM_DSH_TEST_RUNTIME'),
    'Set HTAM_DSH_TEST_RUNTIME to run installed DSH integration tests')


def installed_dsh():
    config = Path(os.environ['HTAM_DSH_TEST_RUNTIME']).resolve(strict=True)
    document = json.loads(config.read_bytes())
    host, = [row for row in document['hosts'].values() if row['adapter'] == 'dsh-headless']
    binary = shutil.which(host['node'])
    if not binary or not host.get('module_entry'):
        raise ValueError('The selected DSH installation requires an executable node and module_entry.')
    return binary, str(config.parent / host['module_entry'])
