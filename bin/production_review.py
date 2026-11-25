"""Exact candidate versions for an author-requested independent review.

Scientific judgments stay in the reviewer report. The host records only the
original author, selected files, actual tool request and native receipt.
"""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import harness_tasks as tasks
import workspace_files as entry
from codex_host import rpc_pages


class ReviewCapabilityUnavailable(entry.ProductionError):
    """The selected host profile cannot access the exact review originals."""


def scientific_path(root, value, *, must_exist=True):
    path = entry.safe_path(root, value, must_exist=False)
    if must_exist and not path.is_file():
        raise entry.ProductionError('The exact original review file is unavailable.')
    rel = path.relative_to(Path(root).resolve(strict=True))
    if any(part in entry.PRIVATE_CONTROL_PARTS for part in rel.parts) or entry.is_credential_path(rel):
        raise entry.ProductionError('Scientific review cannot access private control, credential or native state storage.')
    return path


def mcp_inventory(host, thread_id=None):
    params = {'detail': 'toolsAndAuthOnly'}
    if thread_id:
        params['threadId'] = thread_id
    return [server for page in rpc_pages(host.call, 'mcpServerStatus/list', params) for server in page]


def program_home(request, *, root=None):
    if root is not None:
        from harness_package import work_home
        return work_home(root, request)
    return Path(request['input_ref']['path']).parent / ('program-' + entry.sha256(request['id'].encode())[:16])


def _save_fixed(root, path, raw):
    path = scientific_path(root, path, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.'+path.name+'.') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
            try:
                os.link(output.name,path)
            except FileExistsError:
                pass
    if path.read_bytes() != raw:
        raise ValueError('Fixed submission bytes differ from the original fingerprint.')
    return {'path': str(path.resolve()), 'sha256': entry.sha256(raw)}


def freeze_validation_report(root, home, source, raw):
    """Freeze only the native reports named and hashed by this validation report."""
    if b'data-workspace.lca-gate.v4' not in raw:
        return None
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(report,dict) or report.get('schema')!='data-workspace.lca-gate.v4':
        return None
    root = Path(root).resolve()
    checks = report.get('checks')
    for check in checks if isinstance(checks, list) else ():
        if not isinstance(check,dict) or not isinstance(check.get('native_report'),str):
            continue
        relative = Path(check['native_report'])
        if relative.is_absolute():
            continue  # Kept as an exact original path in the report.
        try:
            location = Path(os.path.abspath(source.parent/relative))
            linked = scientific_path(root,location)
            content = linked.read_bytes()
            if entry.sha256(content)==check.get('native_report_sha256'):
                _save_fixed(root,Path(home)/location.relative_to(root),content)
        except (OSError,ValueError,entry.ProductionError):
            continue  # The original report still returns; the missing check remains visible.
    return _save_fixed(root,Path(home)/source.relative_to(root),raw)


def _fix_artifact(root, home, artifact):
    ref = tasks.evidence_ref(root, artifact)
    source = scientific_path(root, ref['path'])
    raw = source.read_bytes()
    if entry.sha256(raw) != ref['sha256']:
        raise ValueError('Artifact changed while freezing its submitted bytes.')
    fixed = freeze_validation_report(root,home/'fixed'/ref['sha256'],source,raw) or _save_fixed(
        root,home/'fixed'/(ref['sha256']+source.suffix),raw)
    return {**artifact, **fixed, 'version': ref['sha256']}


def _review_packet(root, ref):
    _, contents = read_original(root,ref['path'],ref['sha256'])
    return json.loads(contents)


def _program_runs(root, request):
    with contextlib.closing(tasks._connect(root, True)) as c:
        return [tasks._run_view(c, row) for row in c.execute('''SELECT r.* FROM work_runs r
            JOIN work_requests q ON q.id=r.request_id
            WHERE q.id=? OR (q.parent_request_id=? AND q.independent=1)
            ORDER BY r.created_at,r.id''', (request['id'], request['id']))]


def _program_result(root, run):
    if run['latest_receipt']:
        return _review_packet(root, run['latest_receipt']).get('result') or {}
    return {}


def read_original(root, value, digest):
    path = scientific_path(root, value)
    raw = path.read_bytes()
    if entry.sha256(raw) != digest:
        raise ValueError('The original input changed before launch.')
    return path, raw.decode('utf-8')


def independent_task_input(root, request, role, core, *, input_ref=None):
    if role != 'scientific_reviewer':
        return None
    if not request['independent'] or request['kind'] != 'review':
        raise ValueError('Select the original independent review request.')
    ref = core.evidence_ref(root, input_ref or request['input_ref'])
    result = {'authors': request['authors'], 'evidence_refs': [ref], 'evidence_index': ref['path']}
    from scientific_review.rounds import submission_ref
    with contextlib.closing(core._connect(root, True)) as c:
        submission = submission_ref(c, request)
    if submission != request['input_ref']:
        result['frozen_submission_ref'] = core.evidence_ref(root, submission)
    try:
        packet = _review_packet(root,submission)
    except (ValueError,OSError):
        return result
    if isinstance(packet, dict) and (facts := packet.get('facts_ref')):
        try:
            scientific_path(root, facts['path'])
            result['facts_ref'] = core.evidence_ref(root, facts)
        except (ValueError, OSError, KeyError, TypeError):
            pass
    if isinstance(packet,dict) and packet.get('focus') and packet.get('prior_review'):
        result['focus'] = packet['focus']
        result['prior_review'] = packet['prior_review']
    return result
