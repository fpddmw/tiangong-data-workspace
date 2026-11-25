#!/usr/bin/env python3
"""Query original workspace records and source files without generating instructions."""
import argparse
import collections
from datetime import date
import importlib
import sqlite3
import json
import shlex
import re
import subprocess
import sys
from pathlib import Path
import workspace_files as files

ROOT = Path(__file__).resolve().parent.parent
ROLES = ("coordinator", "data", "calculator")  # Native/legacy execution roles stay stable.
BUSINESS_ROLES = {
    "todo": "coordinator", "plan": "coordinator",
    "data-editor": "data", "review-feedback": "coordinator",
}
ORG_ROLES = ("global_main", "task_lead", "executor", "maintainer")
ALL_ROLES = (*ROLES, *BUSINESS_ROLES)
MAX_INPUT_BYTES = 4 * 1024 * 1024
ROUTE_KINDS = ("system", "production", "knowledge", "history", "operations")


class ContextError(ValueError):
    pass


def read_json(path, label):
    try:
        if not path.is_file():
            raise ContextError(f"Expected a regular file: {label}.")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ContextError(f"Invalid object: {label}.")
        return value
    except (OSError, ValueError) as exc:
        raise ContextError(f"Cannot read valid {label}; context is blocked.") from exc


def verified_bytes(root, ref, label):
    try:
        expected = ref["sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ContextError("Invalid digest.")
        path = Path(ref["path"])
        path = (root / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
        if not path.is_relative_to((root / "archive").resolve()):
            raise ContextError("Evidence must resolve inside this archive.")
        if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
            raise ContextError("Invalid evidence file or size.")
        payload = path.read_bytes()
        if files.sha256(payload) != expected:
            raise ContextError("Digest mismatch.")
        return payload, {"path": str(path.relative_to(root)), "sha256": expected}
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ContextError(f"Missing, changed or invalid source: {label}; select new evidence explicitly.") from exc


def parse(payload, fmt, label):
    try:
        if fmt == "jsonl":
            result = [json.loads(line) for line in payload.splitlines() if line.strip()]
            if not all(isinstance(item, dict) for item in result):
                raise ContextError("Expected object rows.")
            return result
        if fmt == "json":
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise ContextError("Expected object.")
            return result
        if fmt == "text":
            return payload.decode("utf-8")
        raise ContextError("Unsupported format.")
    except (UnicodeError, ValueError) as exc:
        raise ContextError(f"Invalid structured source: {label}.") from exc


def pick(obj, fields):
    if not isinstance(obj, dict):
        raise ContextError("Expected an object in the verified owner source.")
    return {field: obj[field] for field in fields if field in obj}


def principles_projection(root, policy):
    """Read the selected original guidance; historical approval is separate evidence."""
    config = policy.get('data_principles')
    if not config:
        return None
    raw, ref = source_ref(root, config['document']['path'], 'data principles')
    return {'document': ref, 'text': raw.decode('utf-8'),
            'authorization': config.get('authorization'),
            'integrity': 'Current original document read; historical authorization retained as a separate reference.'}


def relative_path(value, label):
    """Validate untrusted manifest paths before touching the filesystem."""
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ContextError(f"Invalid path: {label}.")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ContextError(f"Expected normalized project-relative path: {label}.")
    if files.is_credential_path(path) or '.git' in path.parts:
        raise ContextError(f"Credential or private path is forbidden: {label}.")
    return path


def safe_path(root, value, label, directory=False):
    relative = relative_path(value, label)
    try:
        path = files.safe_path(root, relative, must_exist=not directory)
        relative_path(str(path.relative_to(Path(root).resolve())), label)
        if not (path.is_dir() if directory else path.is_file()):
            raise ContextError(f"Wrong path type: {label}.")
        return path
    except (OSError, files.ProductionError) as exc:
        raise ContextError(f"Missing source: {label}; no files are initialized.") from exc


def source_ref(root, value, label, expected=None, max_bytes=None):
    path = safe_path(root, value, label)
    if max_bytes is None:
        payload = path.read_bytes()
    else:
        # Only callers with an explicitly bounded local operation opt in.
        with path.open('rb') as stream:
            payload = stream.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ContextError(f"Oversized source: {label}.")
    digest = files.sha256(payload)
    if expected is not None and (not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected) or digest != expected):
        raise ContextError(f"Changed or invalid source: {label}; select evidence explicitly.")
    return payload, {"path": value, "sha256": digest}


def bound_ref(root, ref, label, max_bytes=None):
    if not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str):
        raise ContextError(f"Invalid bound reference: {label}.")
    return source_ref(root, ref.get("path"), label, ref["sha256"], max_bytes=max_bytes)


def policy_value(policy, key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", key):
        raise ContextError("Policy key must be a dot-separated object key or list index.")
    value = policy
    try:
        for part in key.split("."):
            if isinstance(value, list):
                if not part.isdecimal() or str(int(part)) != part:
                    raise KeyError(part)
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise KeyError(part)
        return value
    except (KeyError, IndexError) as exc:
        raise ContextError(f"Unknown policy key: {key}.") from exc


def selected_policy(policy, key, sources):
    return {"schema": "data-workspace.policy-selection.v1", "source": sources["policy"], "key": key,
            "value": policy_value(policy, key),
            "authority": "Exact selected policy subtree; check bound source evidence and current native state before action."}


def route_matches_query(route, terms):
    text = " ".join([route["id"], route["path"], *route["keywords"]]).casefold()
    return all(bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text))
               if re.fullmatch(r"[0-9]+", term) else term in text for term in terms)


def workspace_module(name):
    # Import the actual workspace reader when invoked by file path.
    directory = str(Path(__file__).resolve().parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module(name)


def build_daily_context(root, *, role=None, task=None, request=None, query=None, limit=10, offset=0,detail=False):
    current = workspace_module('harness_snapshot').read(root, task_id=task, request_id=request,
                                                       limit=limit, offset=offset,detail=detail)
    result = {'schema': 'data-workspace.daily-context.v1', 'workspace': str(root.resolve()),
              'role': role, 'current': current,
              'entry': str(root.resolve() / 'AGENTS.md')}
    if query:
        discovery = workspace_module('workspace_queries.discovery')
        result['capabilities'] = discovery.capabilities(root, query=query, limit=limit, offset=offset)
        result['assets'] = discovery.assets(root, query=query, limit=limit, offset=offset)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--role', help='Optional reader label; grants no role or operation permission.')
    parser.add_argument('--task')
    parser.add_argument('--request')
    parser.add_argument('--query')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--detail',action='store_true')
    parser.add_argument('--output')
    parser.add_argument('--json', action='store_true', help='Output is always JSON original-record data.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--principles', action='store_true')
    mode.add_argument('--policy-key')
    mode.add_argument('--routes', action='store_true')
    mode.add_argument('--scope')
    mode.add_argument('--asset')
    mode.add_argument('--method')
    parser.add_argument('--asset-version')
    parser.add_argument('--include-history', action='store_true')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if not 1 <= args.limit <= 100 or args.offset < 0:
            raise ValueError('Use limit 1..100 and a nonnegative offset.')
        if args.principles or args.policy_key:
            raw, source = source_ref(root, '.control/policy.json', 'current policy')
            policy = parse(raw, 'json', 'current policy')
            value = ({'data_principles': principles_projection(root, policy), 'source': source} if args.principles
                     else selected_policy(policy, args.policy_key, {'policy': source}))
        elif args.routes or args.scope:
            raw, source = source_ref(root, '.control/agent.json', 'route catalogue')
            routes = parse(raw, 'json', 'route catalogue')['routes']
            selected = [r for r in routes if r['id'] == args.scope] if args.scope else [
                r for r in routes if (args.include_history or r['kind'] != 'history')
                and route_matches_query(r, (args.query or '').casefold().split())]
            if args.scope and not selected:
                raise ValueError('No route with the selected id.')
            value = {'source': source, 'routes': selected[args.offset:args.offset + args.limit],
                     'total': len(selected), 'offset': args.offset,
                     'next_offset': args.offset + args.limit if args.offset + args.limit < len(selected) else None}
        elif args.asset:
            value = workspace_module('workspace_queries.discovery').assets(root, asset_id=args.asset,
                task_id=args.task,version=args.asset_version, limit=args.limit, offset=args.offset)
        elif args.method:
            value = workspace_module('workspace_queries.historical_methods').discover(root, method_id=args.method,
                limit=args.limit, offset=args.offset)
        else:
            value = build_daily_context(root, role=args.role, task=args.task, request=args.request,
                                        query=args.query, limit=args.limit, offset=args.offset,detail=args.detail)
        if args.output:
            value=workspace_module('harness_snapshot').export(root,args.output,value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, UnicodeError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'unavailable', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
