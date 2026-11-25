"""Display exact candidate quantities and references without a scientific verdict."""

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dataset_checks.references import (REFERENCE_TYPES, UUID, VERSION, nested_references,
                                 object_value, reference_body, reference_rows)
SCHEMA = 'data-workspace.lca-candidate-projection.v3'
SUMMARY_FORMAT = 'candidate-summary.v1'

def reference_status(value, inventory):
    identifier, version = value.get('@refObjectId'), value.get('@version')
    kind = REFERENCE_TYPES.get(str(value.get('@type', '')).casefold())
    if not isinstance(identifier, str) or not UUID.fullmatch(identifier):
        return {'status': 'invalid_identifier', 'declared_id': identifier}
    if not isinstance(version, str) or not VERSION.fullmatch(version) or kind is None:
        return {'status': 'incomplete_reference', 'declared_id': identifier, 'declared_version': version}
    uri = value.get('@uri')
    if isinstance(uri, str):
        try:
            stem = Path(urlsplit(uri).path).stem
        except ValueError:
            return {'status': 'invalid_reference_uri', 'declared_id': identifier}
        if UUID.fullmatch(stem) and stem.casefold() != identifier.casefold():
            return {'status': 'reference_uri_conflict', 'declared_id': identifier, 'uri_id': stem}
    key = '/'.join((kind, identifier, version))
    return ({'status': 'bound_local_body', 'handle': key}
            if key in inventory else {'status': 'unresolved', 'requested': key})


def _processes(path, raw):
    selected, duplicate, issues = {}, set(), []
    for index, row in enumerate(reference_rows(path, raw), 1):
        key, payload = reference_body(row)
        if key == 'process':
            issues.append({'kind': 'process_identity_conflict', 'row': index})
        elif payload is not None and key[0] == 'process':
            if key in selected:
                duplicate.add(key)
            else:
                selected[key] = payload
    for key in sorted(duplicate):
        selected.pop(key)
        issues.append({'kind': 'ambiguous_process_identity', 'identity': '/'.join(key)})
    return selected, issues


def array(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def at(value, *fields):
    for field in fields:
        value = object_value(value).get(field)
    return value


def internal_member(rows, identifier):
    if not isinstance(identifier, (int, str)) or isinstance(identifier, bool):
        return None
    matches = [entry for entry in array(rows) if str(at(entry, '@dataSetInternalID')) == str(identifier)]
    return matches[0] if len(matches) == 1 else None


def unit_chain(reference, bodies, inventory):
    chain = {}
    for name, kind, dataset in (
        ('flow', 'flow', 'flowDataSet'),
        ('property', 'flowproperty', 'flowPropertyDataSet'),
        ('unit_group', 'unitgroup', 'unitGroupDataSet'),
    ):
        status = reference_status(object_value(reference), inventory)
        handle = status.get('handle')
        chain[name] = status
        body = at(bodies.get(handle), dataset)
        if body is None or not handle.startswith(kind + '/'):
            return chain, 'unresolved_' + name
        chain[name]['body_sha256'] = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if name == 'flow':
            identifier = at(body, 'flowInformation', 'quantitativeReference', 'referenceToReferenceFlowProperty')
            member = internal_member(at(body, 'flowProperties', 'flowProperty'), identifier)
            if member is None:
                return chain, 'missing_or_ambiguous_reference_property'
            reference = at(member, 'referenceToFlowPropertyDataSet')
        elif name == 'property':
            chain[name]['declared_label'] = at(reference, 'common:shortDescription')
            chain[name]['dataset_name'] = at(body, 'flowPropertiesInformation', 'dataSetInformation', 'common:name')
            reference = at(body, 'flowPropertiesInformation', 'quantitativeReference', 'referenceToReferenceUnitGroup')
        else:
            identifier = at(body, 'unitGroupInformation', 'quantitativeReference', 'referenceToReferenceUnit')
            member = internal_member(at(body, 'units', 'unit'), identifier)
            unit = at(member, 'name')
            if not isinstance(unit, str) or not unit.strip():
                return chain, 'missing_or_ambiguous_reference_unit'
            chain['unit'] = {'id': str(identifier), 'name': unit}
    return chain, 'resolved'


def flow_descriptions(bodies, handles):
    descriptions = {}
    for handle in sorted(handles):
        info = at(bodies.get(handle), 'flowDataSet', 'flowInformation', 'dataSetInformation')
        if info:
            descriptions[handle] = {'name':info.get('name'), 'classification':info.get('classificationInformation')}
    return descriptions


def property_display(prop):
    handle = prop.get('handle') or prop.get('requested')
    if prop.get('dataset_name'):
        return f"{handle} · {json.dumps(prop['dataset_name'], ensure_ascii=False)}"
    return handle


def escape(key):
    return key.replace('~', '~0').replace('/', '~1')


def field_changes(before, after, pointer):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [path for key in sorted(before.keys() | after.keys())
                for path in (field_changes(before[key],after[key],pointer+'/'+escape(key))
                             if key in before and key in after else [pointer+'/'+escape(key)])]
    # Keep full arrays, including both languages of an edited multilingual field.
    return [pointer]


def pointer_value(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('Report fields must use absolute JSON pointers.')
    value = document
    for part in pointer[1:].split('/'):
        key = part.replace('~1', '/').replace('~0', '~')
        # Expand a selected language leaf to its multilingual parent.
        if isinstance(value, list) and any(isinstance(item, dict) and '@xml:lang' in item for item in value):
            return value
        if isinstance(value, dict) and '@xml:lang' in value:
            return value
        if isinstance(value, list) and key.isascii() and key.isdecimal():
            value = value[int(key)] if int(key) < len(value) else None
        else:
            value = object_value(value).get(key)
    return value


def exchange_members(body):
    rows = array(at(body, 'exchanges', 'exchange'))
    members = {str(at(row, '@dataSetInternalID')): row for row in rows}
    if len(rows) != len(members) or 'None' in members:
        return rows, None
    return rows, members


def tiangong_self_uri(uri, kind, ident, version):
    """Compare only recognized first-party exact dataset locators."""
    if not isinstance(uri,str):
        return {'status':'absent','uri':uri}
    result = {'uri':uri,'status':'not_an_exact_tiangong_locator'}
    try:
        parsed=urlsplit(uri)
    except ValueError:
        return result
    if parsed.scheme!='https' or parsed.fragment:
        return result
    details={'process':'process','flow':'flow','source':'source','contact':'contact','lifeCycleModel':'lifecyclemodel',
             'productFlow':'flow','elementaryFlow':'flow','wasteFlow':'flow','flowProperty':'flowproperty','unitGroup':'unitgroup'}
    xml={'processes':'process','flows':'flow','sources':'source','contacts':'contact',
         'lifecyclemodels':'lifecyclemodel','flowproperties':'flowproperty','unitgroups':'unitgroup'}
    if parsed.netloc=='lcdn.tiangong.earth':
        detail=parsed.path.removeprefix('/datasetdetail/').removesuffix('.xhtml')
        if detail not in details or parsed.path!='/datasetdetail/'+detail+'.xhtml':
            return result
        uri_kind=details[detail]
        query=parse_qs(parsed.query,keep_blank_values=True)
        ids, versions=query.get('uuid',[]),query.get('version',[])
        if len(ids)!=1 or len(versions)!=1:
            return result
        uuid, revision=ids[0],versions[0]
    elif parsed.netloc=='data.tiangong.earth' and not parsed.query:
        path=parsed.path.split('/')
        if len(path)!=3 or path[0] or path[1] not in xml or not path[2].endswith('.xml'):
            return result
        uri_kind=xml[path[1]]
        uuid, _, revision=path[2].removesuffix('.xml').partition('_')
        revision=revision or None
    else:
        return result
    if not UUID.fullmatch(uuid) or revision is not None and not VERSION.fullmatch(revision):
        return result
    matches=uri_kind==kind and uuid.lower()==ident.lower() and revision in (None,version)
    return {'uri':uri,'status':'matches' if matches else 'differs','uri_kind':uri_kind,
            'uri_id':uuid,'uri_version':revision,'body_kind':kind,'body_id':ident,'body_version':version}


def declared_process_basis(body, identity, *, exchanges=None):
    information=object_value(at(body,'processInformation','dataSetInformation'))
    quantitative=object_value(at(body,'processInformation','quantitativeReference'))
    exchange_rows=at(body,'exchanges','exchange')
    projected={str(item['id']):item for item in exchanges or []}
    declared=[]
    for number in array(quantitative.get('referenceToReferenceFlow')):
        row=internal_member(exchange_rows,number)
        declared.append({'reference_exchange_id':number,'exchange_found':row is not None,
            'direction':at(row,'exchangeDirection'),'mean_amount':at(row,'meanAmount'),
            'resulting_amount':at(row,'resultingAmount'),'declared_unit':at(row,'unitName'),
            'reference_unit':at(projected.get(str(number)),'unit_chain','unit','name'),
            'flow_ref':at(row,'referenceToFlowDataSet')})
    uri=at(body,'administrativeInformation','publicationAndOwnership','common:permanentDataSetURI')
    return {'self_uri':tiangong_self_uri(uri,*identity),'reference_exchanges':declared,
            'reference_type':quantitative.get('@type'),'functional_unit_or_other':quantitative.get('functionalUnitOrOther'),
            'title':information.get('name')}


def language_changes(before,after):
    """Use language tags in actual changed values, never words or quantities."""
    def texts(value):
        changed={}
        for item in array(value):
            if isinstance(item,dict) and isinstance(lang:=item.get('@xml:lang'),str):
                changed.setdefault(lang,[]).append(item)
        return changed
    old,new=texts(before),texts(after)
    return sorted(lang for lang in old.keys()|new.keys() if old.get(lang)!=new.get(lang))


def project(path, raw, package, inventory, reference_documents=(), *, focus=None, baseline=None):
    bodies = {"/".join(key): payload for document in reference_documents
              for key, payload in [reference_body(document)] if payload is not None}
    processes, issues = _processes(path, raw)
    originals = _processes(*baseline)[0] if baseline else {}
    focus = focus or {}
    if not isinstance(focus, dict) or set(focus) - {'/'.join(key) for key in processes}:
        raise ValueError('Select exact Process handles in the current rows for report focus.')
    digest = hashlib.sha256(raw).hexdigest()
    result = {'schema': SCHEMA, 'rows': {'path': str(path), 'file_sha256': digest},
              'processes': {}, 'reference_inventory': {}, 'reference_consumers': {}, 'issues': issues}
    for key, payload in processes.items():
        kind, identifier, version = key
        handle = '/'.join(key)
        body = payload['processDataSet']
        exchanges, members = exchange_members(body)
        selection = focus.get(handle, {})
        if not isinstance(selection, dict) or set(selection) - {'exchanges', 'fields'}:
            raise ValueError('Report focus uses exchanges and fields for each selected Process.')
        selected = selection.get('exchanges', [])
        fields = selection.get('fields', [])
        if (not isinstance(selected, list) or any(not isinstance(value, str) for value in selected)
                or not isinstance(fields, list) or any(not isinstance(value, str) for value in fields)):
            raise ValueError('Report focus requires exchange IDs and JSON pointers as strings.')
        selected, fields = set(selected), set(fields)
        previous = originals.get((kind, identifier, version))
        if previous:
            previous_body = previous['processDataSet']
            _, old = exchange_members(previous_body)
            if members is None or old is None:
                issues.append({'kind': 'ambiguous_exchange_identity', 'identity': handle})
            else:
                selected.update(i for i in old.keys() | members.keys() if old.get(i) != members.get(i))
            for field in sorted(body.keys() | previous_body.keys()):
                if field != 'exchanges':
                    fields.update(field_changes(previous_body.get(field), body.get(field),
                                                '/processDataSet/' + escape(field)))
        else:
            old = {}
        if selection.get('exchanges') and (members is None or set(selection['exchanges']) - members.keys()):
            raise ValueError('Select unambiguous exchange IDs present in ' + handle)
        if baseline and not previous:
            issues.append({'kind': 'baseline_process_unavailable', 'identity': handle})
        amount_rows, details = [], []
        for index, exchange in enumerate(exchanges):
            exchange = object_value(exchange)
            reference = object_value(exchange.get('referenceToFlowDataSet'))
            chain, status = unit_chain(reference, bodies, inventory)
            exchange_id = str(exchange.get('@dataSetInternalID', ''))
            row = {'id': exchange_id, 'direction': exchange.get('exchangeDirection'),
                   'mean_amount': exchange.get('meanAmount'), 'resulting_amount': exchange.get('resultingAmount'),
                   'flow': chain['flow'], 'unit_chain': chain, 'unit_status': status,
                   'variable': exchange.get('referenceToVariable'),
                   'declared_unit': exchange.get('unitName')}
            amount_rows.append(row)
            if exchange_id in selected:
                details.append({**row, 'pointer': f'/processDataSet/exchanges/exchange/{index}',
                                'value': exchange})
            for ref in chain.values():
                linked = ref.get('handle') or ref.get('requested')
                if linked:
                    result['reference_consumers'].setdefault(linked, []).append({'process': handle, 'exchange': exchange_id})
        field_rows = []
        for field in sorted(fields):
            value=pointer_value(payload,field)
            langs=language_changes(pointer_value(previous,field),value) if previous else []
            field_rows.append({'pointer':field,'value':value,**({'changed_languages':langs} if langs else {})})
        removed = sorted(selected - members.keys()) if old is not None and members is not None else []
        refs = [{'pointer': pointer, **reference_status(value, inventory)}
                for pointer, value in nested_references(payload)]
        for ref in refs:
            if 'handle' in ref:
                result['reference_inventory'][ref['handle']] = inventory[ref['handle']]
        for entry in amount_rows:
            for ref in entry['unit_chain'].values():
                if 'handle' in ref:
                    result['reference_inventory'][ref['handle']] = inventory[ref['handle']]
        projection = {'kind': kind, 'id': identifier, 'version': version,
                      'exchange_count': len(exchanges), 'exchanges': amount_rows, 'references': refs,
                      'declared_basis':declared_process_basis(body,key,exchanges=amount_rows),
                      'focus': {'exchanges': details, 'fields': field_rows, 'removed_exchange_ids': removed,
                                'active': bool(focus or baseline)}}
        result['processes'][handle] = projection
    result['flow_descriptions'] = flow_descriptions(bodies, result['reference_consumers'])
    return result


def markdown(projection, *, summary=False):
    def cell(value):
        return str(value if value is not None else '未提供').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')

    lines = ['# 当前候选交换与引用', '',
             '候选正文：[原件](<' + projection['rows']['path'] + '>)。',
             '候选文件 SHA-256 `' + projection['rows']['file_sha256'] + '`。']
    if summary:
        lines.extend(('', '完整字段、双语原文及结构值见[详细报告](candidate-derived.md)。'))
        if projection['issues']:
            lines.extend(('', '## 对照限制', ''))
            lines.extend('- ' + json.dumps(issue, ensure_ascii=False) for issue in projection['issues'])
    for handle, process in projection['processes'].items():
        lines.extend(('', '## ' + handle, '', f"当前交换 {process['exchange_count']} 条。"))
        if basis:=process.get('declared_basis'):
            lines.extend(('', '| 自身份 URI | 参考交换、数量与单位 | 标题原文 | 功能单位或分母原文 |', '|---|---|---|---|',
                          '| ' + ' | '.join(cell(json.dumps(basis[key],ensure_ascii=False)) for key in
                            ('self_uri','reference_exchanges','title','functional_unit_or_other')) + ' |'))
        lines.extend(('', '| 交换 ID | 方向 | meanAmount | resultingAmount | 精确 Flow | 属性正文 | 单位组 | 候选声明单位 | 参考单位或缺件 |',
                      '|---|---|---|---|---|---|---|---|---|'))
        focus = process['focus']
        exchange_rows = focus['exchanges'] if focus['active'] else process['exchanges']
        for exchange in exchange_rows:
            flow = exchange['flow']
            values = (exchange['id'], exchange['direction'], exchange['mean_amount'],
                      exchange['resulting_amount'], flow.get('handle') or flow.get('requested')
                      or flow.get('declared_id') or flow['status'],
                      property_display(exchange['unit_chain'].get('property', {})),
                      exchange['unit_chain'].get('unit_group', {}).get('handle'),
                      exchange.get('declared_unit'),
                      exchange['unit_chain'].get('unit', {}).get('name') or exchange['unit_status'])
            lines.append('| ' + ' | '.join(cell(value) for value in values) + ' |')
        lines.extend(('', '两列数量直接来自候选；按原合同、变量和参考单位解释其关系，结合来源核实量账。', ''))
        lines.append('当前 CLI 不提供原 qa process 量账；本表直接展示数量，不证明质量平衡或两字段语义一致。')
        if summary and focus['active']:
            lines.extend(('', f"展开了 {len(focus['exchanges'])} 条交换、{len(focus['fields'])} 个字段。"))
            lines.extend(f"- `{item['pointer']}`"+(' · '+', '.join(item['changed_languages']) if item.get('changed_languages') else '')
                         for item in focus['fields'])
        if not summary:
            for item in [*focus['fields'], *focus['exchanges']]:
                # JSON encodes the original text losslessly, including line breaks.
                lines.extend(('', '### ' + item['pointer'], '', '````json',
                              json.dumps(item['value'], ensure_ascii=False, indent=2), '````'))
        if focus['removed_exchange_ids']:
            lines.append('删除的交换 ID：' + ', '.join(focus['removed_exchange_ids']))
        missing = sum(row['status'] != 'bound_local_body' for row in process['references'])
        lines.append(f"候选声明 {len(process['references'])} 处引用，其中 {missing} 处缺少当前本地正文绑定或标识不完整。")
    if not summary and projection.get('flow_descriptions'):
        lines.extend(('', '## 所引用 Flow 的原名和分类', '', '| 准确 Flow | 数据集原名 | 所选分类路径 |', '|---|---|---|'))
        for handle, info in projection['flow_descriptions'].items():
            lines.append('| ' + ' | '.join([cell(handle), cell(json.dumps(info['name'], ensure_ascii=False)),
                                            cell(json.dumps(info['classification'], ensure_ascii=False))]) + ' |')
    shared = {ref: uses for ref, uses in projection['reference_consumers'].items()
              if len({use['process'] for use in uses}) > 1 and ref.startswith('flow/')}
    if shared:
        lines.extend(('', '## 本次候选的共享 Flow 与使用对象', ''))
        for ref, uses in shared.items():
            lines.append('- ' + ref + ' · ' + ', '.join(f"{use['process']} [{use['exchange']}]" for use in uses))
    lines.extend(('', '以上展示当前候选与原生检查覆盖。作者结合原文核数量、各语言说明和用途是否一致；'
                  '科学边界、已知排除项及采用条件沿原证据说明。', ''))
    return '\n'.join(lines)


def exchange_changes(previous, current):
    old, new = previous['processes'], current['processes']
    changes = {}
    for key in sorted(old.keys() & new.keys()):
        left = {entry['id']: entry for entry in old[key]['exchanges']}
        right = {entry['id']: entry for entry in new[key]['exchanges']}
        if len(left) != old[key]['exchange_count'] or len(right) != new[key]['exchange_count']:
            changes[key] = {'state': 'ambiguous_exchange_identity'}
            continue
        fields = ('id', 'direction', 'mean_amount', 'resulting_amount')
        def comparable(entry):
            flow = entry['flow']
            return ({**{field: entry[field] for field in fields},
                     'flow': flow.get('handle') or flow.get('requested') or flow.get('declared_id')})
        revised = [ident for ident in sorted(left.keys() & right.keys()) if comparable(left[ident]) != comparable(right[ident])]
        if revised or left.keys() != right.keys():
            changes[key] = {'added': sorted(right.keys() - left.keys()), 'removed': sorted(left.keys() - right.keys()),
                            'changed': [{'id': ident, 'before': comparable(left[ident]), 'after': comparable(right[ident])}
                                        for ident in revised]}
    return {'added_processes': sorted(new.keys() - old.keys()), 'removed_processes': sorted(old.keys() - new.keys()),
            'exchange_changes': changes}
