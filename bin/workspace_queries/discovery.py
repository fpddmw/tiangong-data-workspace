#!/usr/bin/env python3
"""Read existing asset and capability sources. No registry or runtime is created."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3


def terms(query):
    return re.findall(r'[a-z0-9_-]+|[\u4e00-\u9fff]+', (query or '').casefold())


def score(query, text):
    """Rank metadata, including CJK phrases, without asserting applicability."""
    text = text.casefold()
    words = terms(query)
    if not words:
        return 1
    total = 0
    for word in words:
        if word in text:
            total += 4
        elif re.fullmatch(r'[\u4e00-\u9fff]+', word):
            total += sum(word[i:i + 2] in text for i in range(len(word) - 1))
    return total


METHOD_TERMS = (('分母', '参考量', '参考属性', '数量基准'), ('投入', '投料', '交换', '清单'))


def method_score(query, text):
    """Prefer literal matches and use related concepts only as weaker hits."""
    literal = score(query, text)
    if literal or not query:
        return literal
    for group in METHOD_TERMS:
        if any(term in query for term in group) and any(term in text for term in group):
            return 1
    return 0


def json_file(path):
    if not path.is_file():
        raise ValueError('Expected a regular metadata file: ' + str(path))
    return json.loads(path.read_bytes())


def observed_failures(items):
    """Explicit native tool outcomes survive even a successful final turn."""
    from harness_hook import digest, failed_tool, failure_component
    failures = []
    for item in items:
        kind = item.get('type')
        if kind not in ('commandExecution', 'mcpToolCall', 'fileChange', 'dynamicToolCall'):
            continue
        nested = item.get('result') if isinstance(item.get('result'), dict) else {}
        response = {'exit_code': item.get('exitCode'),
                    'isError': item.get('status') == 'failed' or item.get('success') is False or nested.get('isError') is True,
                    'error': item.get('error')}
        failure = failed_tool({'tool_response': response, 'tool_input': {'command': item.get('command', '')}})
        if failure and isinstance(item.get('id'), str):
            tool = item.get('tool')
            namespace = item.get('server') or item.get('namespace')
            name = '/'.join(part for part in (namespace, tool) if isinstance(part, str) and part)
            tool_input = item.get('command') if kind == 'commandExecution' else item.get('arguments')
            if kind == 'fileChange':
                tool_input = item.get('changes')
            component = failure_component(tool_input) if kind == 'commandExecution' else None
            failures.append({'native_item_id': item['id'], 'tool_type': kind,
                             **({'component': component} if component else {}),
                             'tool_name': name if re.fullmatch(r'[A-Za-z0-9_./:-]{1,160}', name) else kind,
                             'input_sha256': digest(tool_input), **failure})
    return failures


def observed_commands(root, items):
    """Keep bounded native invocation facts, without command arguments or output.

    Only a simple, exact repository CLI invocation is recognized. Shell programs,
    reading a Skill, installed files and a model's claimed use are not execution.
    """
    root = Path(root).resolve()
    observations = []
    for item in items:
        if item.get('type') != 'commandExecution':
            continue
        command = item.get('command', '')
        if not isinstance(command, str) or any(c in command for c in ';|&`$\n<>'):
            continue
        try:
            args = shlex.split(command)
            if not args or Path(args.pop(0)).name not in ('python', 'python3'):
                continue
            if args and args[0] == '-B':
                args.pop(0)
            if not args or args[0].startswith('-'):
                continue
            cwd = Path(item.get('cwd') or root).resolve()
            path = (cwd / args[0]).resolve()
            if path.parent != root / 'bin' or path.suffix != '.py' or not path.is_file():
                continue
            observations.append({'entry_ref': {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                'native_item_id': item.get('id'), 'status': item.get('status'), 'exit_code': item.get('exitCode'),
                'basis': 'Native command observation; source fingerprint captured at readback. Scientific effect and adoption are not inferred.'})
        except (ValueError, OSError, TypeError):
            continue
    return observations


def assets(root, *, task_id=None, query=None, asset_id=None, version=None, limit=5, offset=0, context=None):
    """Read the preserved HTAM catalog, never its native task execution tables."""
    root = Path(root).resolve()
    context = context or ({'work': task_id} if task_id else {})
    path = root / '.harness-local/manager/state.sqlite'
    result = {'source': str(path), 'status': 'unavailable', 'items': [], 'total': 0,
              'authority': 'Original asset/use observations only; same task ID is a retrieval key, not ownership transfer or fresh platform state.'}
    try:
        manifest = root / '.control/agent.json'
        if manifest.is_file() and (config := json_file(manifest).get('knowledge_sources', {}).get('htam')):
            from workspace_queries.historical_methods import original_custody
            owner, _, path = original_custody(root, config)
            result.update(source=str(path), source_owner=str(owner))
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError) as exc:
        return {**result, 'reason': str(exc)}
    if not path.is_file():
        result['reason'] = 'Original asset catalog is not present on this host; no catalog initialized.'
        return result
    if not (task_id or query or asset_id):
        return {**result, 'status': 'selection_required'}
    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        if not {'assets', 'asset_versions', 'asset_uses'} <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            return {**result, 'reason': 'Unsupported original catalog schema; use its original reader.'}
        where, params = [], []
        if asset_id:
            where.append('a.id=?'); params.append(asset_id)
        elif task_id and not query:
            where.append('EXISTS(SELECT 1 FROM asset_uses u WHERE u.asset_id=a.id AND u.work_id=?)'); params.append(task_id)
        rows = c.execute('SELECT a.id,v.version AS selected_version,a.current_version,a.updated_at,v.body,v.sha256 FROM assets a '
                         'JOIN asset_versions v ON v.asset_id=a.id AND v.version=' + ('? ' if version else 'a.current_version ')
                         + ('WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY a.id',
                         ([version] if version else []) + params)
        matches = []
        for row in rows:
            row = dict(row)
            body = json.loads(row['body'])
            if body.get('access', body.get('access_scope', 'local_workspace')) not in ('local_workspace', 'workspace', 'local'):
                continue
            rank = score(query, ' '.join([row['id'], body.get('title', ''), body.get('description', ''),
                                         json.dumps(body.get('limitations', []), ensure_ascii=False)]))
            if not rank:
                continue
            item = {'id': row['id'], 'version': row['selected_version'], 'current_version': row['current_version'], 'observed_at': row['updated_at'],
                    **{k: body[k] for k in ('title', 'description', 'type', 'limitations', 'depends_on') if k in body},
                    'original_refs': body.get('provenance', []), 'declared_uses': body.get('uses', []),
                    'catalog_body_sha256': row['sha256'],
                    'detail_command': 'python3 bin/context.py --asset ' + shlex.quote(row['id']) + ' --asset-version ' + shlex.quote(row['selected_version'])}
            matches.append((rank, item))
        matches.sort(key=lambda pair: (-pair[0], pair[1]['id']))
        selected = []
        for _, item in matches if asset_id else matches[offset:offset + limit]:
            use_params = (item['id'], task_id) if task_id else (item['id'],)
            use_where = ' WHERE asset_id=? ' + ('AND work_id=? ' if task_id else '')
            use_rows = c.execute('SELECT work_id,requirement,version,body,at FROM asset_uses WHERE asset_id=? '
                                 + ('AND work_id=? ' if task_id else '') + 'ORDER BY id DESC LIMIT ? OFFSET ?',
                                 (*use_params, limit if asset_id else 2, offset if asset_id else 0)).fetchall()
            use_total=c.execute('SELECT count(*) FROM asset_uses'+use_where,use_params).fetchone()[0]
            if asset_id:
                item['available_versions'] = [r[0] for r in c.execute('SELECT version FROM asset_versions WHERE asset_id=? ORDER BY version', (item['id'],))]
                item['use_total'] = use_total
                item['use_offset'] = offset
                item['use_next_offset'] = offset + limit if offset + limit < item['use_total'] else None
                item['use_page_command'] = ('python3 bin/context.py --asset ' + item['id']
                    + (' --asset-version ' + str(version) if version is not None else '')
                    + ' --offset NEXT_OFFSET --limit ' + str(limit))
            item['use_observations'] = [{'work_id': r['work_id'], 'requirement': r['requirement'],
                'version': r['version'], 'observed_at': r['at'],
                **json.loads(r['body'])} for r in use_rows]
            page_offset=offset if asset_id else 0;page_size=limit if asset_id else 2
            detail=item['detail_command']+(' --task '+shlex.quote(task_id) if task_id else '')
            def preview(entries,kind):
                result=[]
                for number,entry in enumerate(entries,page_offset):
                    if not asset_id and len(json.dumps(entry,ensure_ascii=False).encode())>2048:
                        result.append({'asset_id':item['id'],'version':item['version'],'entry_index':number,
                            'not_expanded':True,'meaning':'Complete use statement and its conditions are not expanded.',
                            'detail_command':detail+' --offset '+str(number)+' --limit 1'})
                    else:result.append(entry)
                return result
            for field in ('original_refs','declared_uses'):
                entries=item[field];total=len(entries)
                page=entries[page_offset:page_offset+page_size]
                item[field]=preview(page,field)
                item[field+'_pagination']={'total':total,'shown':len(page),'offset':page_offset,
                    'next_offset':page_offset+page_size if page_offset+page_size<total else None,
                    'detail_command':detail,'filter':{'asset_id':item['id'],'version':item['version']}}
            item['use_observations']=preview(item['use_observations'],'use_observations')
            item['use_observations_pagination']={'total':use_total,'shown':len(use_rows),'offset':page_offset,
                'next_offset':page_offset+page_size if page_offset+page_size<use_total else None,
                'detail_command':detail,'filter':{'work_id':task_id}}
            item['original_ref_count']=item['original_refs_pagination']['total']
            item['use_total']=use_total
            item['use_observation_count_shown']=len(use_rows)
            selected.append(item)
        return {**result, 'status': 'read_only', 'items': selected, 'total': len(matches),
                'shown':len(selected),'offset': offset, 'next_offset': offset + limit if not asset_id and offset + limit < len(matches) else None,
                'coverage': 'Original catalog versions and use observations; no current applicability or authority is inferred.'}
    except (sqlite3.Error, ValueError, KeyError) as exc:
        return {**result, 'reason': str(exc)}
    finally:
        c.close()


def capabilities(root, *, query=None, manifest=None, limit=5, offset=0):
    """Combine the existing manifest, installed Skill contracts and tool bindings."""
    root = Path(root).resolve()
    candidates, issues = [], []
    if manifest is None:
        try:
            manifest = json_file(root / '.control/agent.json')
        except (OSError, ValueError) as exc:
            manifest = {}; issues.append({'source': '.control/agent.json', 'reason': str(exc)})
    for route in manifest.get('routes', []):
        if route.get('kind') not in ('knowledge', 'operations') or route.get('discovery') == 'explicit' or 'observation' in route:
            continue
        entry = route.get('entry', '')
        candidates.append({'id': route['id'], 'kind': 'documented_entry',
            'purpose': ' '.join(route.get('keywords', [])), 'entry': entry,
            'availability': 'readable' if (root / entry).is_file() else 'unavailable',
            'command': 'python3 bin/context.py --scope ' + route['id'], 'policy_keys': route.get('policy_keys', [])})
    skills = {}
    for name in ('skills-lock.json',):
        try:
            config = json_file(root / name)
            rows = config.get('skills', [])
            if isinstance(rows, dict):
                rows = [{'name': key, **value} for key, value in rows.items()]
            for item in rows:
                skills[item['name']] = {**item, 'version': item.get('commit', config.get('source', {}).get('commit')), 'lock_source': name}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append({'source': name, 'reason': str(exc)})
    for name, item in skills.items():
        path = root / item['path'] / 'SKILL.md'
        description, availability, digest = '', 'unavailable', None
        try:
            # Only explicitly registered contracts, never a recursive filesystem search.
            if not path.is_file():
                raise OSError('Registered Skill contract is not a regular file')
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            description = item.get('description', name)
            expected = next((f['sha256'] for f in item.get('files', []) if f.get('path') == 'SKILL.md'), None)
            availability = 'readable' if expected is None or digest == expected else 'changed_from_lock'
        except OSError:
            pass
        if name == 'flow-governance-review':
            description = '判断 Flow 复用、身份和单位转换。' + description
        candidates.append({'id': name, 'kind': 'skill', 'purpose': description, 'entry': str(path),
            'version': item.get('version'), 'sha256': digest, 'lock_source': item['lock_source'],
            'availability': availability, 'instruction': 'Read this Skill; use its native command and actual runtime checks.',
            'runtime_entry': 'docs/tools/data-cli.md'})
    candidates.extend([
        {'id': 'original-task', 'kind': 'harness', 'purpose': '任务 恢复 接续 当前成果 交付 task resume return',
         'entry': 'bin/harness.py', 'command': 'python3 bin/context.py', 'availability': 'readable'},
        {'id': 'maintenance', 'kind': 'harness', 'purpose': '工具 故障 问题 维护 board issue friction repair',
         'entry': 'bin/harness.py', 'command': 'python3 bin/harness.py work-maintenance', 'availability': 'readable'},
        {'id': 'browser-review', 'kind': 'browser', 'purpose': '网页 浏览器 审核 意见 review comments browser',
         'entry': 'workflows/business-flow/native-review.md', 'availability': 'host_check_required',
         'instruction': 'Use the current authorized browser/plugin read surface and the original review scope. The CLI adapter does not create browser capability or a login.'},
        {'id': 'native-cli', 'kind': 'cli', 'purpose': 'Skill 调用失败和工具排障，核实际运行时绑定、安装和 CLI。过程 流 数据集 单位 换算 检索 计算 process flow dataset unit convert',
         'entry': 'docs/tools/data-cli.md', 'availability': 'runtime_check_required',
         'command': 'python3 bin/lca_gate.py --doctor', 'runtime_binding': '.control/runtime.json'}])
    ranked = []
    for item in candidates:
        # Installed paths and the shared Skill instruction are capability metadata,
        # not matching terms for every query containing the generic word "Skill".
        searchable = [item['id'], item.get('purpose', '')]
        if item['kind'] != 'skill':
            searchable.extend((item.get('entry', ''), item.get('instruction', '')))
        ranked.append((score(query, ' '.join(searchable)), item))
    ranked = [p for p in ranked if p[0]]
    exact = (query or '').strip().casefold()
    ranked.sort(key=lambda p: (p[1]['id'].casefold() != exact, -p[0], p[1]['availability'] != 'readable', p[1]['id']))
    return {'items': [item for _, item in ranked[offset:offset + limit]], 'total': len(ranked),
            'offset': offset, 'next_offset': offset + limit if offset + limit < len(ranked) else None,
            'source_issues': issues, 'authority': 'Capability discovery only. Read the selected contract and check current host, runtime and operation authority.'}
