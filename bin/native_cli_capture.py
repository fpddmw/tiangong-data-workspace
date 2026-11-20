#!/usr/bin/env python3
"""Installed CLI delegate for managed skill subprocesses, preserving its argv."""
import json,os,sys
from pathlib import Path
from delivery_evidence import execute_native

ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    spec=json.loads((ROOT/'.control/runtime.json').read_bytes())
    raise SystemExit(execute_native(ROOT,[str(ROOT/spec['node']),str(ROOT/spec['cli'])],
        sys.argv[1:],env=os.environ.copy(),cwd=Path.cwd()))
