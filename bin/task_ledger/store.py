"""Original file evidence and the single workspace database; no workflow policy."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3

from workspace_files import is_credential_path, sha256_file

_CONNECTION = ContextVar('evidence_connection', default=None)


def plan_history_path(path, digest):
    return path.with_name(path.stem + '.history-' + digest + path.suffix)


@contextmanager
def evidence_connection(connection):
    token = _CONNECTION.set(connection)
    try:
        yield
    finally:
        _CONNECTION.reset(token)


def initialize_database(root):
    path = Path(root) / '.control/harness.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / 'harness-init.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        c = sqlite3.connect(path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA foreign_keys=ON')
        os.chmod(path, 0o600)
    return c


def _historical_location(root, path, expected):
    """Read a recorded same-byte location; never create a mapping or infer a move."""
    if not path.is_relative_to(root) or not (root / '.control/harness.sqlite').is_file():
        return path
    c = _CONNECTION.get()
    own = c is None
    if own:
        c = sqlite3.connect((root / '.control/harness.sqlite').as_uri() + '?mode=ro', uri=True)
        c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='harness_evidence_locations'").fetchone():
            return path
        seen = set()
        while True:
            logical = path.relative_to(root).as_posix()
            row = c.execute('SELECT * FROM harness_evidence_locations WHERE origin_path=? AND sha256=?', (logical, expected)).fetchone()
            if row is None:
                return path
            if logical in seen:
                raise ValueError('Cyclic historical evidence location')
            seen.add(logical)
            audit = c.execute('SELECT plan_sha256,audit_json FROM harness_path_relocations WHERE migration_id=?', (row['migration_id'],)).fetchone()
            move = {'from': {'path': logical, 'sha256': expected}, 'to': {'path': row['physical_path'], 'sha256': expected}}
            if not audit or audit['plan_sha256'] != row['plan_sha256'] or move not in json.loads(audit['audit_json']).get('moves', []):
                raise ValueError('Historical evidence location lacks its original receipt')
            path = root / row['physical_path']
            if not path.resolve().is_relative_to(root):
                raise ValueError('Historical location escapes the workspace')
    finally:
        if own:
            c.close()


def evidence_path(root, ref):
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or not ref['path']:
        raise ValueError('Identify the original evidence file')
    expected = ref.get('sha256')
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Original evidence needs its recorded SHA-256')
    root = Path(root).resolve()
    path = Path(os.path.abspath(root / ref['path']))
    path = _historical_location(root, path, expected)
    logical=path.relative_to(root) if path.is_relative_to(root) else path
    if is_credential_path(logical):
        raise ValueError('Credentials are not evidence')
    if path.is_relative_to(root):
        history = plan_history_path(path, expected)
        if (history.is_file() and not history.is_symlink() and
                (not path.is_file() or sha256_file(path)!=expected)):
            path = history
    path = path.resolve(strict=True)
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    if is_credential_path(rel):
        raise ValueError('Credentials are not evidence')
    if not path.is_file() or sha256_file(path) != expected:
        raise ValueError('Original evidence bytes changed or are unavailable')
    return str(path)
