#!/usr/bin/env python3
"""Select the installed tool and role environment; native tools own their commands."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from env_run import read_env, role_env, tool_environment, tool_cwd

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['editor', 'manager', 'research'], default='editor')
    parser.add_argument('surface', choices=['cli', 'ai', 'foundry', 'utilities'])
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv and args.argv[0] == '--' else args.argv
    if not argv:
        raise ValueError('Explicit public command required')
    if args.surface == 'utilities':
        from utilities_tools import run
        raise SystemExit(run(ROOT, argv))
    spec = json.loads((ROOT / '.control/runtime.json').read_text())
    cwd = tool_cwd(ROOT)
    envfile = ROOT / '.env'
    env = role_env(args.role, read_env(envfile) if envfile.exists() else {}, ROOT)
    env = tool_environment(ROOT, env)
    key = {'cli': 'cli', 'ai': 'ai_cli', 'foundry': 'foundry_runtime_cli'}[args.surface]
    node, cli = ROOT / spec['node'], ROOT / spec[key]
    command = [str(node), str(cli)]
    if args.surface == 'foundry':
        manifest = ROOT / spec['manifest']
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != spec['manifest_sha256']:
            raise ValueError('Original Foundry runtime manifest changed')
        command += ['runtime', 'exec', '--manifest', str(manifest), '--manifest-sha256', spec['manifest_sha256'],
                    '--cache-dir', str(ROOT / spec['cache']), '--entry', 'foundry',
                    '--cwd', str(ROOT / spec['foundry_workspace']), '--']
    if args.surface == 'cli':
        from delivery_evidence import execute_native
        raise SystemExit(execute_native(ROOT,command,argv,env=env,cwd=cwd))
    raise SystemExit(subprocess.call(command + argv, env=env, cwd=cwd))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        sys.exit(1)
