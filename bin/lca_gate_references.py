"""Select exact local QA reference bodies from the original package only."""

import hashlib
import json
from pathlib import Path
import re

KINDS = {
    'flowDataSet': ('flow', 'flowInformation'),
    'flowPropertyDataSet': ('flowproperty', 'flowPropertiesInformation'),
    'unitGroupDataSet': ('unitgroup', 'unitGroupInformation'),
    'processDataSet': ('process', 'processInformation'),
    'sourceDataSet': ('source', 'sourceInformation'),
    'contactDataSet': ('contact', 'contactInformation'),
    'LCIAMethodDataSet': ('lciamethod', 'LCIAMethodInformation'),
    'lifeCycleModelDataSet': ('lifecyclemodel', 'lifeCycleModelInformation'),
}
REFERENCE_TYPES = {
    'flow data set': 'flow', 'flow property data set': 'flowproperty', 'unit group data set': 'unitgroup',
    'source data set': 'source', 'contact data set': 'contact', 'process data set': 'process',
    'lcia method data set': 'lciamethod', 'life cycle model data set': 'lifecyclemodel',
}
QA_KINDS = {'flow', 'flowproperty', 'unitgroup'}
QA_CATEGORIES = ('flows', 'flowproperties', 'unitgroups')
CATEGORIES = (*QA_CATEGORIES, 'contacts', 'sources', 'processes', 'lciamethods', 'lifecyclemodels')
UUID = re.compile(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}')
VERSION = re.compile(r'[0-9]{2}\.[0-9]{2}\.[0-9]{3}')
SHA256 = re.compile(r'[0-9a-f]{64}')
ROW_FILES = ('reference-definitions.rows.jsonl', 'reference-rows.jsonl', 'reference-rows.json')


def object_value(value):
    return value if isinstance(value, dict) else {}


def reference_rows(path, raw=None):
    text = path.read_text(encoding='utf-8') if raw is None else raw.decode('utf-8')
    if path.suffix.lower() == '.jsonl':
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    rows = object_value(data).get('rows', data)
    return rows if isinstance(rows, list) else [rows]


def reference_body(row):
    if not isinstance(row, dict):
        return None, None
    wrappers = ('json_ordered', 'jsonOrdered', 'json', 'payload', *(kind for kind, _ in KINDS.values()))
    payload = next((row[key] for key in wrappers if isinstance(row.get(key), dict)), row)
    for key, (kind, information) in KINDS.items():
        body = payload.get(key)
        if not isinstance(body, dict):
            continue
        info = object_value(object_value(body.get(information)).get('dataSetInformation'))
        admin = object_value(body.get('administrativeInformation'))
        publication = object_value(admin.get('publicationAndOwnership'))
        ident, version = info.get('common:UUID'), publication.get('common:dataSetVersion')
        if not isinstance(ident, str) or not UUID.fullmatch(ident):
            return kind, None
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            return kind, None
        if row.get('id', ident) != ident or row.get('version', version) != version:
            return kind, None
        return (kind, ident, version), payload
    return None, None


def manifest_references(directory, package):
    bound, inputs, issues = {}, [], []
    for name, field, path_field in (('manifest.json', 'files', 'path'),
                                    ('frozen-source.json', 'selected', 'file')):
        path = directory / name
        if not path.is_file() or not path.resolve().is_relative_to(package):
            continue
        inputs.append(path)
        try:
            entries = object_value(json.loads(path.read_text(encoding='utf-8'))).get(field, [])
        except (OSError, UnicodeError, ValueError):
            issues.append({'kind': 'manifest_unreadable', 'path': str(path)})
            continue
        for item in entries if isinstance(entries, list) else []:
            item = object_value(item)
            name = item.get(path_field)
            if not isinstance(name, str):
                continue
            relative = Path(name)
            if relative.name not in ROW_FILES and not (
                    relative.parent.name in CATEGORIES and relative.suffix.lower() == '.json'):
                continue
            if relative.is_absolute() or not (directory / relative).resolve().is_relative_to(package):
                issues.append({'kind': 'manifest_reference_outside_package', 'path': str(path)})
                continue
            target = (directory / relative).resolve()
            expected = item.get('sha256')
            bound.setdefault(target, set()).add(expected if isinstance(expected, str) and SHA256.fullmatch(expected) else '')
    return bound, inputs, issues


def nested_references(value, pointer=''):
    if isinstance(value, dict):
        if '@refObjectId' in value:
            yield pointer, value
        for key, nested in value.items():
            escaped = key.replace('~', '~0').replace('/', '~1')
            yield from nested_references(nested, pointer + '/' + escaped)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from nested_references(nested, pointer + '/' + str(index))


def explicit_bundle_dependencies(directory, rows):
    categories = {'source': 'sources', 'contact': 'contacts', 'process': 'processes',
                  'lciamethod': 'lciamethods', 'lifecyclemodel': 'lifecyclemodels'}
    try:
        originals = reference_rows(rows)
    except (OSError, UnicodeError, ValueError):
        return
    directory = directory.resolve()
    for row in originals:
        key, payload = reference_body(row)
        if not payload or key[0] != 'process':
            continue
        for _, ref in nested_references(payload):
            category = categories.get(REFERENCE_TYPES.get(str(ref.get('@type', '')).casefold()))
            ident, version = ref.get('@refObjectId'), ref.get('@version')
            if (not category or not isinstance(ident, str) or not UUID.fullmatch(ident)
                    or not isinstance(version, str) or not VERSION.fullmatch(version)):
                continue
            for stem in (ident, ident + '_' + version, ident + '__' + version):
                candidate = (directory / category / (stem + '.json')).resolve()
                if candidate.is_relative_to(directory) and candidate.is_file():
                    yield candidate


def select_references(package, rows, explicit, support, tidas_dir, model_bundles):
    package = package.resolve()
    paths, issues, inputs, bound = {}, [], [], {}
    roots = [rows.parent, package, package / 'inputs', package / 'native']
    roots = list(dict.fromkeys(root for root in roots if root.resolve().is_relative_to(package)))
    if explicit:
        paths.update((path.resolve(), 'explicit') for path in explicit)
    else:
        for directory in roots:
            declared, manifests, warnings = manifest_references(directory, package)
            inputs.extend(manifests)
            issues.extend(warnings)
            for path, hashes in declared.items():
                bound.setdefault(path, set()).update(hashes)
            for name in ROW_FILES:
                path = directory / name
                if path.is_file() and path.resolve().is_relative_to(package):
                    paths[path.resolve()] = 'original_package'
        bundles = [*model_bundles]
        if tidas_dir:
            bundles.append(tidas_dir)
        for root in roots:
            bundles.extend((root, root / 'tidas_bundle', root / 'tidas'))
        explicit_bundles = {*model_bundles, tidas_dir}
        for directory in dict.fromkeys(bundles):
            if directory not in explicit_bundles and not directory.resolve().is_relative_to(package):
                continue
            for category in QA_CATEGORIES:
                for path in sorted((directory / category).glob('*.json')):
                    if path.is_file() and (directory in explicit_bundles or path.resolve().is_relative_to(package)):
                        paths[path.resolve()] = 'native_tidas_layout'
        if tidas_dir:
            paths.update((path, 'native_tidas_layout') for path in explicit_bundle_dependencies(tidas_dir, rows))
        paths.update((path.resolve(), 'manifest_bound') for path in bound)
        for path in support:
            paths.setdefault(path.resolve(), 'support_rows')

    inputs.extend(paths)
    selected, sources, conflicts = {}, {}, set()
    for path, origin in paths.items():
        try:
            raw = path.read_bytes()
            observed = hashlib.sha256(raw).hexdigest()
            if path in bound and bound[path] != {observed}:
                issues.append({'kind': 'reference_binding_mismatch', 'path': str(path),
                               'expected_file_sha256': sorted(bound[path]), 'observed_file_sha256': observed})
            records = reference_rows(path, raw)
        except (OSError, UnicodeError, ValueError):
            issues.append({'kind': 'reference_binding_mismatch' if path in bound else 'reference_rows_unreadable',
                           'path': str(path)})
            continue
        invalid = 0
        for number, row in enumerate(records, 1):
            key, payload = reference_body(row)
            if payload is None:
                if key is not None or origin != 'support_rows':
                    invalid += 1
                continue
            sources.setdefault(key, []).append({'path': str(path), 'file_sha256': observed,
                                                 'row': number, 'selection': origin})
            if key in selected and selected[key] != payload:
                conflicts.add(key)
            else:
                selected[key] = payload
        if invalid:
            issues.append({'kind': 'invalid_reference_rows', 'path': str(path), 'count': invalid})

    for key in sorted(conflicts):
        selected.pop(key)
        issues.append({'kind': 'ambiguous_exact_reference', 'identity': '/'.join(key),
                       'paths': sorted({item['path'] for item in sources[key]})})
    qa_rows = [body for key, body in selected.items() if key[0] in QA_KINDS]
    return list(dict.fromkeys(inputs)), qa_rows, {
        'selection': 'explicit' if explicit else 'original_package',
        'sources': [str(path) for path in paths],
        'exact_rows': len(qa_rows),
        'bindings': {'/'.join(key): sources[key] for key in sorted(selected)},
        'issues': issues,
    }
