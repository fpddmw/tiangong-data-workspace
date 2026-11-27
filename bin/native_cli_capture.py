#!/usr/bin/env python3
"""Installed CLI delegate for managed skill subprocesses, preserving its argv."""
import os
from pathlib import Path
import sys

from delivery_evidence import execute_native
from env_run import load_runtime_config

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    spec, _ = load_runtime_config(ROOT)
    command = [str(ROOT / spec['node']), str(ROOT / spec['cli'])]
    raise SystemExit(execute_native(ROOT, command, sys.argv[1:], env=os.environ.copy(), cwd=Path.cwd()))
