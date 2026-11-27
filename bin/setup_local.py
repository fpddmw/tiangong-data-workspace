#!/usr/bin/env python3
"""Initialize and diagnose machine-local workspace configuration without exposing secrets."""
import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from env_run import accounts_config_path, read_env, runtime_config_path  # noqa: E402
import lca_gate  # noqa: E402


PLACEHOLDER_MARKERS = ('<', '>', 'replace_with_')


def _is_placeholder(value):
    if not isinstance(value, str) or not value.strip():
        return True
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def init_local(root):
    """Create missing machine-local config files from tracked templates; never overwrite existing files."""
    root = Path(root).resolve()
    created = []
    skipped = []

    env_target = root / '.env'
    env_template = root / '.env.example'
    if env_target.exists():
        skipped.append({'path': '.env', 'reason': 'already_exists'})
    elif env_template.is_file():
        shutil.copyfile(env_template, env_target)
        os.chmod(env_target, 0o600)
        created.append({'path': '.env', 'from': '.env.example', 'mode': '0600'})
    else:
        skipped.append({'path': '.env', 'reason': 'template_missing'})

    pairs = (
        ('.control/runtime.template.json', '.control/runtime.local.json', '.control/runtime.json'),
        ('.control/accounts.template.json', '.control/accounts.local.json', '.control/accounts.json'),
    )
    for template_rel, local_rel, legacy_rel in pairs:
        local_target = root / local_rel
        legacy_target = root / legacy_rel
        template_path = root / template_rel
        if local_target.exists():
            skipped.append({'path': local_rel, 'reason': 'already_exists'})
        elif legacy_target.exists():
            skipped.append({'path': local_rel, 'reason': f'using_existing:{legacy_rel}'})
        elif template_path.is_file():
            local_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template_path, local_target)
            created.append({'path': local_rel, 'from': template_rel})
        else:
            skipped.append({'path': local_rel, 'reason': 'template_missing'})

    return {'schema': 'data-workspace.local-init.v1', 'created': created, 'skipped': skipped}


def _check_env(root):
    path = root / '.env'
    if not path.exists():
        return {'status': 'missing', 'path': '.env', 'configured_roles': [], 'hint': 'Run python3 bin/setup_local.py --init and fill .env (chmod 0600).'}
    try:
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (mode & 0o077):
            return {'status': 'insecure_permissions', 'path': '.env', 'mode': oct(mode), 'hint': 'Run chmod 600 .env'}
        values = read_env(path)
    except (OSError, ValueError) as exc:
        return {'status': 'invalid', 'path': '.env', 'error': str(exc)}
    nonempty = {k for k, v in values.items() if isinstance(v, str) and v.strip()}
    roles = []
    if 'DATA_EDITOR_SESSION_FILE' in nonempty and 'TIANGONG_LCA_API_BASE_URL' in nonempty:
        roles.append('editor')
    if 'DATA_MANAGER_SESSION_FILE' in nonempty and 'TIANGONG_LCA_API_BASE_URL' in nonempty:
        roles.append('manager')
    if 'CONN' in nonempty:
        roles.append('database-read')
    if {'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY', 'S3_BUCKET'} <= nonempty:
        roles.append('storage-read')
    if any(k.startswith(('TIANGONG_LCA_KB_SEARCH_', 'TIANGONG_LCA_UNSTRUCTURED_', 'TIANGONG_AI_', 'TIANGONG_SCI_', 'TIANGONG_REPORT_', 'DIFY_', 'UNSTRUCTURED_')) for k in nonempty):
        roles.append('research')
    return {'status': 'ready' if nonempty else 'empty', 'path': '.env', 'mode': oct(mode),
            'configured_key_count': len(nonempty), 'configured_roles': sorted(roles)}


def _check_runtime(root):
    path = runtime_config_path(root)
    rel = path.relative_to(root).as_posix()
    if not path.is_file():
        return {'status': 'missing', 'path': rel, 'hint': 'Copy .control/runtime.template.json to .control/runtime.local.json'}
    try:
        spec = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'status': 'invalid', 'path': rel, 'error': str(exc)}
    checks = {}
    issues = []
    for key in ('node', 'cli'):
        raw = spec.get(key)
        if _is_placeholder(raw):
            checks[key] = 'unconfigured'
            issues.append(f'runtime.{key} is unconfigured')
        else:
            target = root / raw
            ok = target.is_file()
            checks[key] = 'ok' if ok else 'missing_file'
            if not ok:
                issues.append(f'runtime.{key} file missing: {raw}')
    for optional_key in ('foundry_runtime_cli', 'ai_cli'):
        raw = spec.get(optional_key)
        if not raw:
            checks[optional_key] = 'optional_unset'
        elif _is_placeholder(raw):
            checks[optional_key] = 'unconfigured'
        else:
            ok = (root / raw).is_file()
            checks[optional_key] = 'ok' if ok else 'missing_file'
            if not ok:
                issues.append(f'runtime.{optional_key} file missing: {raw}')
    manifest_raw = spec.get('manifest')
    manifest_sha = spec.get('manifest_sha256')
    if manifest_raw and not _is_placeholder(manifest_raw):
        mpath = root / manifest_raw
        if not mpath.is_file():
            checks['manifest'] = 'missing_file'
            issues.append(f'runtime.manifest missing: {manifest_raw}')
        elif manifest_sha and not _is_placeholder(manifest_sha):
            actual = hashlib.sha256(mpath.read_bytes()).hexdigest()
            checks['manifest'] = 'ok' if actual == manifest_sha else 'sha256_mismatch'
            if actual != manifest_sha:
                issues.append('runtime.manifest_sha256 mismatch')
        else:
            checks['manifest'] = 'unverified_no_sha256'
    else:
        checks['manifest'] = 'optional_unset'
    ext = {}
    if isinstance(spec.get('external_materials'), dict):
        for name, loc in spec['external_materials'].items():
            if not loc or _is_placeholder(loc):
                ext[name] = 'unset'
            else:
                target = Path(loc).expanduser()
                if not target.is_absolute():
                    target = root / target
                ext[name] = 'ok' if target.exists() else 'missing_path'
    return {'status': 'ready' if not issues else 'incomplete', 'path': rel,
            'checks': checks, 'external_materials': ext, 'issues': issues}


def _check_accounts(root):
    path = accounts_config_path(root)
    rel = path.relative_to(root).as_posix()
    if not path.is_file():
        return {'status': 'missing', 'path': rel, 'hint': 'Copy .control/accounts.template.json to .control/accounts.local.json'}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'status': 'invalid', 'path': rel, 'error': str(exc)}
    issues = []
    project_ref = data.get('project_ref')
    editor_id = (data.get('editor') or {}).get('user_id') if isinstance(data.get('editor'), dict) else None
    manager_id = (data.get('manager') or {}).get('user_id') if isinstance(data.get('manager'), dict) else None
    if _is_placeholder(project_ref):
        issues.append('accounts.project_ref is unconfigured')
    if _is_placeholder(editor_id):
        issues.append('accounts.editor.user_id is unconfigured')
    return {
        'status': 'ready' if not issues else 'incomplete',
        'path': rel,
        'project_configured': not _is_placeholder(project_ref),
        'editor_configured': not _is_placeholder(editor_id),
        'manager_configured': not _is_placeholder(manager_id),
        'issues': issues,
    }


def _check_skills(root):
    lock = root / 'skills-lock.json'
    result = lca_gate.skill_binding(root)
    if lock.is_file():
        try:
            spec = json.loads(lock.read_text(encoding='utf-8'))
            result['primary_source'] = spec.get('source')
            result['additional_sources'] = spec.get('additional_sources', [])
        except (OSError, ValueError):
            pass
    return result


def doctor(root):
    root = Path(root).resolve()
    env_info = _check_env(root)
    runtime_info = _check_runtime(root)
    accounts_info = _check_accounts(root)
    skills_info = _check_skills(root)
    ok = (
        env_info['status'] == 'ready'
        and runtime_info['status'] == 'ready'
        and accounts_info['status'] == 'ready'
        and skills_info.get('status') == 'lock_verified'
    )
    return {
        'schema': 'data-workspace.local-doctor.v1',
        'ok': ok,
        'env': env_info,
        'runtime': runtime_info,
        'accounts': accounts_info,
        'skills': skills_info,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(ROOT), help='Workspace root directory')
    parser.add_argument('--init', action='store_true', help='Create missing local config files from templates')
    parser.add_argument('--doctor', action='store_true', help='Inspect local config readiness (default)')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    payload = {}
    if args.init:
        payload['init'] = init_local(root)
    if args.doctor or not args.init:
        payload['doctor'] = doctor(root)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if 'doctor' in payload and not payload['doctor']['ok']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
