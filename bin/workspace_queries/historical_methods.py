"""Read preserved HTAM methods in place; no journal, rule or adoption writes.

The optional manifest locates original documents and catalogs independently.
The former observations journal owns candidate/version/lifecycle history. These
are historical discovery candidates, never current instructions or permission.
"""
import hashlib
import json
from pathlib import Path
import sqlite3

from workspace_queries import discovery


# Navigation to the maintained sections. The files own the method, conditions,
# counterexamples and case links. No publication entry or extra copy is needed.
GUIDANCE = {
    'quantity-basis': ('分母与参考量', 'workflows/dataset-review/candidate-preparation.md', '核分母与参考量',
                       ('evidence', 'prepare', 'validation', 'scientific-review'),
                       '分母 参考量 参考属性 数量基准 denominator reference quantity',
                       'htam-note:research-methods:cf2d8d1fc08c'),
    'exchange-notes': ('改交换后同步量账和说明', 'workflows/dataset-review/门禁与引用检查.md', '改交换后同步量账和说明',
                       ('prepare', 'validation', 'scientific-review', 'review-feedback'),
                       '交换 投入 量账 说明 碳 dioxide inventory notes', None),
    'hash-kind': ('标明哈希类型', 'workflows/dataset-review/门禁与引用检查.md', '标明哈希类型',
                  ('validation', 'save', 'readback', 'scientific-review', 'submission', 'review-feedback'),
                  '哈希 指纹 字节 投影 hash digest payload sha256 读回', None),
    'residual': ('残差分析与用途', 'workflows/dataset-review/科学独审.md', '完整候选的科学独审',
                 ('validation', 'scientific-review', 'submission', 'review-feedback'),
                 '残差 质量平衡 质量偏差 收率 residual mass balance a07', None),
    'background-boundary': ('背景与前景边界', 'workflows/dataset-review/system-planning.md', '上游产品、共享与递归边界',
                            ('evidence', 'prepare', 'scientific-review', 'review-feedback'),
                            '背景 前景 边界 上游 供方 递归 background boundary provider', None),
}


def workspace_guidance(root, *, query=None, method_id=None):
    items, issues = [], []
    for name, (title, path, heading, steps, aliases, historical) in GUIDANCE.items():
        ident = 'guidance:' + name
        if method_id and method_id != ident or not method_id and not discovery.score(query, title + ' ' + aliases):
            continue
        original = Path(root) / path
        if not original.is_file():
            issues.append({'source': path, 'reason': 'Original file missing'})
        items.append({'id': ident, 'title': title, 'section': heading,
                      'method_ref': {'path': str(original)}, 'source_state': 'workspace_original'})
    return items, issues


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(base, name):
    base = Path(base).resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or any(p.startswith('.env') or p in
            ('.git', '.secrets', '.ssh', '.aws') for p in path.parts):
        raise ValueError('Knowledge reference resolves outside its declared source or into credentials')
    return path


def original_custody(root, config):
    """Read optional original pointers, independently of old host layout and hashes."""
    recovery = {}
    if config.get('recovery_ref'):
        locator = Path(root) / config['recovery_ref']['path']
        if locator.is_file():
            recovery = json.loads(locator.read_bytes())
    owner = Path(recovery.get('source_root') or root).resolve()
    database = Path(recovery.get('source_database') or Path(root) / '.harness-local/manager/state.sqlite').resolve()
    return owner, recovery, database


def sources(root, manifest=None):
    root = Path(root).resolve()
    manifest = manifest if manifest is not None else discovery.json_file(root / '.control/agent.json')
    config = manifest.get('knowledge_sources', {}).get('htam') or {}
    owner, recovery, database = original_custody(root, config)
    retired = recovery.get('retired_source_root')
    return owner, Path(retired).resolve() if retired else None, database, config


def document_methods(root, retired, config):
    items, issues = [], []
    for ref in config.get('documents', []):
        try:
            if not Path(ref['path']).is_absolute() and retired is None:
                raise ValueError('Original document root is unavailable; the catalog and other sources remain readable')
            path = safe_path(retired or Path(ref['path']).parent, ref['path'])
            raw = path.read_bytes()
            observed = digest(raw)
            items.append({'id': ref['id'], 'method_ref': {'path': str(path), 'sha256': observed},
                          'recorded_ref': ref, 'matches_recorded_bytes': observed == ref.get('sha256'),
                          'source_state': 'historical_document'})
        except (OSError, ValueError, KeyError) as exc:
            issues.append({'source_ref': ref, 'reason': str(exc)})
    return items, issues


def journal_methods(root, retired, database):
    if not database.is_file():
        return [], [{'source': str(database), 'reason': 'Original catalog unavailable'}]
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        items = []
        for row in c.execute("SELECT id,asset,body,at FROM observations WHERE kind='note' ORDER BY id"):
            try:
                value = json.loads(row['body'])
            except (ValueError, TypeError):
                value = {'original_body': row['body']}
            if not isinstance(value, dict):
                value = {'original_body': value}
            experience = value.get('experience', {})
            if experience.get('candidate', {}).get('access', 'local_workspace') != 'local_workspace':
                continue
            items.append({'id': 'observation:' + str(row['id']), 'asset': row['asset'],
                'observed_at': row['at'], 'record': value,
                'source_ref': {'database': str(database), 'table': 'observations', 'id': row['id']}})
    return items, []


def discover(root, *, query=None, method_id=None, limit=10, offset=0, manifest=None):
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Use limit 1..100 and nonnegative offset.')
    guides, issues = workspace_guidance(root, query=query, method_id=method_id)
    items = guides
    if not (method_id and method_id.startswith('guidance:')):
        try:
            owner, retired, database, config = sources(root, manifest)
            docs, missing = document_methods(owner, retired, config)
            try:
                rows, unavailable = journal_methods(owner, retired, database)
            except (OSError, ValueError, sqlite3.Error) as exc:
                rows, unavailable = [], [{'source': str(database), 'reason': str(exc)}]
            items += docs + rows
            issues += missing + unavailable
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            issues.append({'reason': str(exc)})
    selected = [item for item in items if item['id'] == method_id] if method_id else [
        item for item in items if discovery.score(query, json.dumps(item, ensure_ascii=False))]
    return {'status': 'read_only', 'items': selected[offset:offset + limit],
            'total': len(selected), 'offset': offset,
            'next_offset': offset + limit if offset + limit < len(selected) else None,
            'source_issues': issues}
