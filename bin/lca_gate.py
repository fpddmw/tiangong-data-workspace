#!/usr/bin/env python3
"""Report installed capabilities and run selected native local dataset checks."""

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile

from lca_gate_projection import SCHEMA as PROJECTION_SCHEMA, SUMMARY_FORMAT, exchange_changes, markdown, project
from lca_gate_references import KINDS, reference_rows, select_references
from workspace_files import sha256_file

ROOT = Path(__file__).resolve().parent.parent
ROLE = 'editor'
GATE_SCHEMA = 'data-workspace.lca-gate.v4'
CANDIDATE_PREFIX = 'gate-candidate-v4-'
COMPLETE = {'checks_completed', 'warning'}
DOCTOR_SCHEMA = 'data-workspace.lca-doctor.v4'
DOCTOR_RECEIPT = Path('.harness-local/v2-implementation/lca-doctor.json')
# These are the input directories consumed by the native SDK package validator.
TIDAS_CATEGORIES = ('contacts', 'flowproperties', 'flows', 'lciamethods',
                    'lifecyclemodels', 'processes', 'sources', 'unitgroups')


def command(args, role, root=None):
    return [sys.executable, '-B', str((root or ROOT) / 'bin/launch.py'), '--role', role, 'cli', *map(str, args)]


def invoke(args, role='research', *, root=None, timeout=None):
    root = root or ROOT
    return subprocess.run(command(args, role, root), cwd=root, capture_output=True, text=True,
                          timeout=timeout)


def json_object(text):
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def sdk_binding(root, descriptor):
    root = Path(root).resolve()
    # Resolve the SDK from the installed CLI package. The runtime descriptor covers only the CLI.
    module = Path(descriptor['package']['root']) / 'package.json'
    node = descriptor['node']['executable']
    program = (
        "const { createRequire } = require('node:module');"
        "process.stdout.write(createRequire(process.argv[1]).resolve('@tiangong-lca/tidas-sdk'));"
    )
    observed = subprocess.run([node, '-e', program, str(module)], cwd=root, env={},
                              capture_output=True, text=True, timeout=10)
    if observed.returncode or not observed.stdout.strip():
        return {'status': 'unavailable'}
    entry = Path(observed.stdout.strip()).resolve()
    if not entry.is_relative_to(root):
        return {'status': 'outside_workspace'}
    for directory in entry.parents:
        manifest = directory / 'package.json'
        if manifest.is_file():
            spec = json_object(manifest.read_text(encoding='utf-8'))
            if spec.get('name') == '@tiangong-lca/tidas-sdk':
                files = [manifest, *sorted(path for path in (directory / 'dist').rglob('*') if path.is_file())]
                digest = fingerprint([(str(path.relative_to(directory)), sha256_file(path)) for path in files])
                return {'status': 'available', 'version': spec.get('version'), 'content_sha256': digest}
        if directory == root:
            break
    return {'status': 'unavailable'}


def runtime(root=None):
    root = root or ROOT
    if not (root / 'bin/launch.py').is_file():
        return {'status': 'unavailable'}
    result = invoke(['runtime', 'describe', '--json'], root=root, timeout=10)
    data = json_object(result.stdout)
    if result.returncode or data.get('schema') != 'tiangong-lca.cli-runtime-descriptor.v1':
        return {'status': 'unavailable', 'exit_code': result.returncode}
    path = root / '.control/runtime.json'
    spec = json.loads(path.read_text(encoding='utf-8'))
    observed = {'platform': data['platform'], 'package_version': data['package']['version'],
                'content_sha256': data['content_sha256'], 'node_version': data['node']['version'],
                'node_sha256': data['node']['sha256']}
    binding = 'direct_installation_observed'
    try:
        sdk = sdk_binding(root, data)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        sdk = {'status': 'unavailable'}
    return {'status': 'available', 'binding': binding, **observed, 'sdk': sdk,
            'config_sha256': sha256_file(path)}


def command_capabilities(root, installed):
    probes = {
        'dataset_validation': (['dataset', 'process', 'validate'], 'input'),
        'package_validation': (['validation', 'run'], '--report-file'),
        'exact_support_read': (['dataset', 'contact', 'get'], 'version'),
        'exact_flow_read': (['dataset', 'flow', 'get'], 'version'),
        'local_references': (['dataset', 'process', 'list-references'], 'input'),
        'model_aggregation': (['dataset', 'model', 'build-resulting-process'], 'input'),
        'draft_preflight': (['dataset', 'process', 'save'], 'dry-run'),
        'cli_submission': (['review', 'submit'], 'dry-run'),
    }
    checks = {}
    for key, (args, required) in probes.items():
        if installed['status'] != 'available':
            checks[key] = 'not_run'
            continue
        try:
            result = invoke([*args, '--help'], root=root, timeout=10)
            advertised = result.returncode == 0 and required in result.stdout
        except (OSError, subprocess.SubprocessError):
            advertised = False
        remote = key in ('exact_support_read', 'exact_flow_read', 'draft_preflight', 'cli_submission')
        checks[key] = ('entry_advertised_unverified' if remote else 'entry_advertised') if advertised else 'unavailable'
    checks['unit_aware_process_qa'] = 'not_exposed_by_current_cli'
    return checks


def skill_binding(root):
    lock = root / 'skills-lock.json'
    if not lock.is_file():
        return {'status': 'lock_missing'}
    spec = json_object(lock.read_text(encoding='utf-8'))
    items = [(Path(skill['path']) / item['path'], item)
             for skill in spec.get('skills', []) for item in skill['files']]
    items.extend((Path(item['path']), item) for item in spec.get('support_files', []))
    if not items:
        return {'status': 'lock_invalid', 'lock_sha256': sha256_file(lock)}
    skills = root / '.agents/skills'
    observations = []
    for name, item in items:
        path = root / name
        actual, mode = None, None
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(skills):
            actual = sha256_file(path)
            mode = oct(stat.S_IMODE(path.stat().st_mode))
        observations.append((str(name), actual, mode, item['sha256'], item.get('mode', mode)))
    matching = sum(actual == expected and mode == expected_mode
                   for _, actual, mode, expected, expected_mode in observations)
    launcher = [actual == expected and mode == expected_mode
                for path, actual, mode, expected, expected_mode in observations
                if path.startswith('.agents/skills/scripts/')]
    return {'status': 'lock_verified' if matching == len(items) else 'installation_incomplete_or_changed',
            'launcher_verified': bool(launcher) and all(launcher),
            'locked_files': len(items), 'verified_files': matching,
            'lock_sha256': sha256_file(lock), 'observed_sha256': fingerprint(observations)}


def skill_runtime(root, skills):
    if not skills.get('launcher_verified'):
        return {'status': 'launcher_unverified'}
    result = subprocess.run([sys.executable, '-B', str(root / 'bin/env_run.py'), 'research',
                             '--without-project-env', '--', 'node',
                             '.agents/skills/scripts/run-cli.mjs', '--runtime-info'],
                            cwd=root, capture_output=True, text=True, timeout=10)
    evidence = json_object(result.stdout)
    runtime_info = evidence.get('runtime', {})
    if result.returncode or runtime_info.get('schema') != 'tiangong-lca.cli-runtime-descriptor.v1':
        return {'status': 'unavailable'}
    return {'status': 'available', 'package_version': runtime_info['package']['version'],
            'content_sha256': runtime_info['content_sha256'], 'node_sha256': runtime_info['node']['sha256']}


def local_doctor(root: Path, *, refresh: bool = True) -> dict:
    # Observe on prepare; a read-only consumer gets the last observed snapshot.
    root = Path(root).resolve()
    path = root / DOCTOR_RECEIPT
    configured = (root / '.env').is_file() and not (root / '.env').is_symlink()
    editor = 'configuration_present_unverified' if configured else 'project_env_missing'
    safe = path.parent.resolve().is_relative_to(root) and not path.is_symlink()
    previous = {}
    candidate_entry = fingerprint([
        sha256_file(source) if source.is_file() else None
        for source in (root / 'bin/launch.py', root / 'bin/env_run.py')
    ])
    if safe and path.is_file():
        try:
            previous = json_object(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError):
            pass
    if previous.get('schema') != DOCTOR_SCHEMA:
        previous = {}
    if not refresh:
        if previous:
            try:
                bindings = ((root / '.control/runtime.json', previous.get('runtime', {}).get('config_sha256')),
                            (root / 'skills-lock.json', previous.get('skills', {}).get('lock_sha256')))
                changed = (candidate_entry != previous.get('candidate_entry_sha256')
                           or any(expected is not None and (not source.is_file() or sha256_file(source) != expected)
                                  for source, expected in bindings))
            except (AttributeError, OSError):
                changed = True
            if changed:
                return {'schema': DOCTOR_SCHEMA, 'cache_status': 'binding_changed'}
            return {**previous, 'cache_status': 'last_observed', 'editor_read_and_preflight_identity': editor}
        return {'schema': DOCTOR_SCHEMA, 'cache_status': 'not_observed'}
    try:
        installed = runtime(root)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        installed = {'status': 'unavailable'}
    try:
        skills = skill_binding(root)
    except (OSError, ValueError, KeyError, TypeError):
        skills = {'status': 'unverified'}
    try:
        wrapper = skill_runtime(root, skills)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        wrapper = {'status': 'unavailable'}
    key = fingerprint([DOCTOR_SCHEMA, installed, skills, wrapper, candidate_entry])
    if previous.get('binding_sha256') == key and isinstance(previous.get('native_commands'), dict):
        return {**previous, 'cache_status': 'reused', 'editor_read_and_preflight_identity': editor}
    report = {'schema': DOCTOR_SCHEMA, 'binding_sha256': key, 'runtime': installed,
              'candidate_entry_sha256': candidate_entry,
              'skills': skills, 'skill_runtime': wrapper, 'native_commands': command_capabilities(root, installed),
              'editor_read_and_preflight_identity': editor, 'platform_submission': 'not_probed'}
    if not safe:
        return {**report, 'cache_status': 'not_written'}
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(path)
    except OSError:
        return {**report, 'cache_status': 'not_written'}
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {**report, 'cache_status': 'refreshed'}


def startup_readiness(root):
    """Use the same offline doctor for prepared turns on either native route."""
    detail = shlex.join(['python3', '-B', str(root / 'bin/lca_gate.py'), '--doctor'])
    try:
        observed = local_doctor(root)
        report = {'runtime': observed['runtime']['status'], 'skills': observed['skills']['status'],
                  'skill_runtime': observed['skill_runtime']['status'],
                  'native_commands': observed['native_commands'],
                  'editor_read_and_preflight': observed['editor_read_and_preflight_identity'],
                  'platform_submission': observed['platform_submission'],
                  'binding_sha256': observed['binding_sha256'],
                  'cache_status': observed['cache_status'], 'detail_command': detail}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {'cache_status': 'unavailable', 'detail_command': detail}
    if observed['cache_status'] in ('refreshed', 'reused'):
        report['report_path'] = str(root / DOCTOR_RECEIPT)
    return report


def doctor(live):
    report = local_doctor(ROOT)
    # The live check is opt-in and its receipt never enters the startup cache.
    if live and report.get('editor_read_and_preflight_identity') == 'configuration_present_unverified':
        accounts = json.loads((ROOT / '.control/accounts.json').read_text())
        receipt = invoke(['auth', 'identity-receipt', '--expected-project-ref', accounts['project_ref'],
                          '--expected-user-id', accounts['editor']['user_id'], '--json'], ROLE)
        proof = json_object(receipt.stdout).get('data', {}).get('receipt', {}).get('assertions', {})
        report['editor_read_and_preflight_identity'] = (
            'live_identity_verified' if receipt.returncode == 0 and proof.get('mode') == 'intent-bound'
            and proof.get('passed') is True else 'live_identity_unverified')
    return report

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def documents(path, raw=None):
    """Extract canonical bodies from caller-selected artifacts, without changing them."""
    for row in reference_rows(path, raw):
        if not isinstance(row, dict):
            raise ValueError(f'Expected a dataset object in {path}')
        bodies = [row] + [value for key, value in row.items()
                         if key in ('json', 'json_ordered', 'jsonOrdered', 'payload', 'content',
                                    'process', 'flow', 'source', 'contact', 'model') and isinstance(value, dict)]
        matches = [(kind, body) for body in bodies for key, (kind, _) in KINDS.items()
                   if isinstance(body.get(key), dict)]
        if len(matches) != 1:
            raise ValueError(f'Select one canonical body per row in {path}; response envelopes are not content')
        kind, body = matches[0]
        yield ('model' if kind == 'lifecyclemodel' else kind), body


UPSTREAM_BOUNDARIES = (
    {'boundary': 'reference_save_admission', 'issue': 'tiangong-lca/cli#504',
     'summary': 'Validation does not exercise save admission; a legitimate older exact reference may still be rejected when a newer published sibling exists.'},
    {'boundary': 'model_metadata_projection', 'issue': 'tiangong-lca/cli#505',
     'summary': 'A qualified or generated result does not prove authored Model description, use limits and rights were preserved.',
     'kinds': ('model', 'lifecyclemodel', 'package')},
    {'boundary': 'quantity_consistency', 'issue': 'tiangong-lca/cli#425',
     'summary': 'Native validation does not establish meanAmount/resultingAmount consistency or a complete mass balance.',
     'kinds': ('process', 'package')},
)


def upstream_notes(name):
    """Standing upstream limits on what this check can establish; diagnostics, never verdicts."""
    kind = name.rsplit('-', 1)[-1]
    return [{'boundary': row['boundary'], 'issue': row['issue'], 'summary': row['summary']}
            for row in UPSTREAM_BOUNDARIES if 'kinds' not in row or kind in row['kinds']]


def native_validation_failed(response,exit_code=0):
    """Interpret complete original native results without invoking a tool."""
    if not isinstance(response,dict) or not response:return True
    data=response.get('data') if isinstance(response.get('data'),dict) else {}
    validation=data.get('validation') if isinstance(data.get('validation'),dict) else {}
    return (exit_code!=0 or response.get('error') is not None or response.get('ok') is False
        or response.get('status') in ('failed','partial','unknown','incomplete')
        or validation.get('ok') is False or validation.get('status') in ('failed','incomplete'))


def run_check(name, argv, output, native):
    result = invoke([*argv, '--json'])
    (output / f'{name}.stdout.json').write_text(result.stdout, encoding='utf-8')
    if result.stderr:
        (output / f'{name}.stderr.txt').write_text(result.stderr, encoding='utf-8')
    response = json_object(result.stdout)
    data = response.get('data') if isinstance(response.get('data'), dict) else {}
    validation = data.get('validation') if isinstance(data.get('validation'), dict) else {}
    failed = native_validation_failed(response,result.returncode)
    check = {'status': 'failed' if failed else 'passed', 'exit_code': result.returncode,
             'command': command([*argv, '--json'], 'research')[2:],
             'native_report': native.name if native.is_file() else f'{name}.stdout.json',
             'native_outcome': response.get('outcome'),
             'completeness': response.get('completeness', {})}
    check['native_report_sha256'] = sha256_file(output / check['native_report'])
    if validation:
        check['payload_sha256'] = validation.get('payload_sha256')
        check['validation_layers'] = validation.get('validation_layers', {})
        check['not_checked'] = validation.get('notChecked', [])
    check['upstream_boundary_notes'] = upstream_notes(name)
    return check


def intact_report(directory, report):
    artifacts = report.get('artifacts', {})
    return bool(artifacts) and all((directory / name).is_file()
        and (directory / name).resolve().is_relative_to(directory.resolve())
        and sha256_file(directory / name) == digest for name, digest in artifacts.items())


def derived_check(package, rows, previous_report):
    directory = previous_report.parent.resolve(strict=True)
    report = json_object(previous_report.read_text())
    current = project(rows, rows.read_bytes(), package, {})
    try:
        previous = json_object((directory / 'candidate-derived.json').read_text())
        changes = exchange_changes(previous, current)
        previous_hash = previous['rows']['file_sha256']
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({'outcome': 'comparison_unavailable', 'previous_report': str(previous_report),
                          'current': current, 'diagnostic': str(exc),
                          'meaning': 'Original report remains evidence; this reader cannot compare its projection.'}, ensure_ascii=False))
        return 2
    changed, unchecked = [], []
    for original, expected in report.get('inputs', {}).items():
        path = Path(original)
        path = path if path.is_absolute() else directory / path
        try:
            if sha256_file(path) != expected:
                changed.append(original)
        except OSError:
            unchecked.append(original)
    outcome = ('stale' if current['rows']['file_sha256'] != previous_hash or changed
               else 'unverified' if unchecked else 'current')
    print(json.dumps({'outcome': outcome, 'previous': previous['rows'], 'current': current['rows'],
                      'previous_report': str(previous_report),
                      'changed_inputs': changed, 'unchecked_inputs': unchecked, **changes}, ensure_ascii=False))
    return 0 if outcome == 'current' else 1


def gate(args):
    package = args.package.resolve(strict=True)
    if not package.is_dir():
        raise ValueError('Select the original package directory')
    if args.check_derived:
        return derived_check(package, args.rows, args.check_derived)
    paths, reference_bodies, selection = select_references(
        package, args.rows, args.reference_rows_file, args.support_rows, args.tidas_dir, [])
    sources = [args.rows, *args.support_rows]
    if args.tidas_dir:
        sources += [path for kind in TIDAS_CATEGORIES
                    for path in sorted((args.tidas_dir / kind).glob('*.json'))]
    # Read each selected source once; canonical copies are execution inputs, never new scientific drafts.
    raw_inputs = {path: path.read_bytes() for path in dict.fromkeys([*sources, *paths])}
    focus_path, baseline_path = getattr(args, "focus", None), getattr(args, "baseline_rows", None)
    for path in (focus_path, baseline_path):
        if path:
            raw_inputs[path] = path.read_bytes()
    focus = json.loads(raw_inputs[focus_path]) if focus_path else None
    baseline = (baseline_path, raw_inputs[baseline_path]) if baseline_path else None
    inputs = {str(path): hashlib.sha256(raw).hexdigest() for path, raw in raw_inputs.items()}
    try:
        installed = runtime()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        installed = {'status': 'unavailable', 'diagnostic': str(exc)}
    invocation = fingerprint([GATE_SCHEMA, PROJECTION_SCHEMA, SUMMARY_FORMAT, installed, inputs, selection, reference_bodies])
    projection = project(args.rows, raw_inputs[args.rows], package, selection.get('bindings', {}), reference_bodies,
                         focus=focus, baseline=baseline)
    output = args.out_dir or package / (CANDIDATE_PREFIX + invocation)
    output = output.resolve()
    if output == package or not output.is_relative_to(package):
        raise ValueError('Choose a report directory inside the original package')
    if output.exists() and not args.out_dir and not args.retry:
        previous = json_object((output / 'gate-report.json').read_text()) if (output / 'gate-report.json').is_file() else {}
        if previous.get('invocation_sha256') == invocation and intact_report(output, previous):
            return result(previous['outcome'], output, reused=True)
        raise ValueError(f'Incomplete or changed original report at {output}; inspect it and use --retry if needed')
    if args.retry:
        output = Path(tempfile.mkdtemp(prefix=CANDIDATE_PREFIX + invocation + '-retry-', dir=package))
    else:
        output.mkdir(parents=True, exist_ok=False)
    checks = []
    report = {'schema': GATE_SCHEMA, 'runtime': installed, 'inputs': inputs,
              'invocation_sha256': invocation, 'checks': checks, 'reference_selection': selection,
              'not_checked': ['scientific adequacy and bilingual equivalence',
                              'unit-aware mass balance (no current public CLI operation)',
                              'meanAmount/resultingAmount consistency (upstream CLI issue 425)',
                              'remote reference admission and write preconditions']}
    reference_file = output / 'reference-documents.json'
    write_json(reference_file, {'documents': reference_bodies})
    number = 0
    for source in dict.fromkeys(sources):
        for kind, body in documents(source, raw_inputs[source]):
            number += 1
            stem = f'{number:03d}-{kind}'
            canonical, native = output / f'{stem}.content.json', output / f'{stem}.validation.json'
            write_json(canonical, body)
            argv = ['dataset', kind, 'validate', '--input', canonical, '--output', native]
            if kind == 'process' and reference_bodies:
                argv += ['--reference-documents', reference_file]
            check = run_check(stem, argv, output, native)
            check.update(source=str(source), canonical_input=canonical.name)
            checks.append(check)
    if args.tidas_dir:
        native = output / 'package-validation.json'
        checks.append(run_check('package', ['validation', 'run', '--engine', 'sdk',
            '--input-dir', args.tidas_dir, '--report-file', native], output, native))
    write_json(output / 'candidate-derived.json', projection)
    native_links = '\nNative validation reports:\n\n' + '\n'.join(
        f"- [{check['native_report']}]({check['native_report']}): {check['status']}" for check in checks)
    for name, summary in [('candidate-derived.md', False), ('candidate-summary.md', True)]:
        (output / name).write_text(markdown(projection, summary=summary) + native_links + '\n', encoding='utf-8')
    now_paths, now_bodies, now_selection = select_references(package, args.rows, args.reference_rows_file, args.support_rows, args.tidas_dir, [])
    changed = (any(not path.is_file() or path.read_bytes() != raw for path, raw in raw_inputs.items())
               or fingerprint([list(map(str, paths)), reference_bodies, selection]) != fingerprint([list(map(str, now_paths)), now_bodies, now_selection]))
    report['outcome'] = ('input_changed' if changed else 'failed' if any(c['status'] == 'failed' for c in checks)
                         else 'warning')
    if not checks:
        report['outcome'] = 'failed'
        report['diagnostic'] = 'No dataset documents were selected'
    report['artifacts'] = {path.name: sha256_file(path) for path in output.iterdir() if path.is_file()}
    write_json(output / 'gate-report.json', report)
    return result(report['outcome'], output)


def result(outcome, directory, *, reused=False):
    print(json.dumps({'outcome': outcome, 'report': str(directory / 'gate-report.json'),
                      'summary': str(directory / 'candidate-summary.md') if (directory / 'candidate-summary.md').is_file() else None,
                      'derived': str(directory / 'candidate-derived.md') if (directory / 'candidate-derived.md').is_file() else None,
                      'reused': reused}, ensure_ascii=False))
    return 0 if outcome in COMPLETE else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--doctor', action='store_true')
    parser.add_argument('--live-auth', action='store_true')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--rows', type=Path, help='Canonical content or original JSON/JSONL dataset rows')
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--check-derived', type=Path)
    parser.add_argument('--support-rows', action='append', type=Path, default=[])
    parser.add_argument('--reference-rows-file', action='append', type=Path, default=[])
    parser.add_argument('--tidas-dir', type=Path)
    parser.add_argument('--focus', type=Path, help='JSON mapping exact Process handles to exchange IDs and field pointers')
    parser.add_argument('--baseline-rows', type=Path, help='Original Process rows for a scoped change report')
    args = parser.parse_args()
    other = (args.package, args.rows, args.out_dir, args.retry, args.check_derived,
             args.support_rows, args.reference_rows_file, args.tidas_dir, args.focus, args.baseline_rows)
    if args.doctor:
        if any(other): parser.error('--doctor is independent of dataset checks')
        print(json.dumps(doctor(args.live_auth), ensure_ascii=False, indent=2))
        return 0
    if args.live_auth or not args.package or not args.rows:
        parser.error('provide --package and --rows, or --doctor [--live-auth]')
    if args.check_derived and any((args.out_dir, args.retry, args.support_rows, args.reference_rows_file, args.tidas_dir, args.focus, args.baseline_rows)):
        parser.error('--check-derived reads the prior report and current rows only')
    if args.retry and args.out_dir: parser.error('choose --retry or an explicit new --out-dir')
    for key in ('package', 'rows', 'out_dir', 'check_derived', 'tidas_dir', 'focus', 'baseline_rows'):
        if getattr(args, key) is not None: setattr(args, key, getattr(args, key).resolve())
    args.support_rows = [path.resolve() for path in args.support_rows]
    args.reference_rows_file = [path.resolve() for path in args.reference_rows_file]
    return gate(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
