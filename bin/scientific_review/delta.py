"""Read-only comparison of two packets from the same author assignment."""
from collections import defaultdict
import contextlib
import hashlib
import json
from pathlib import Path
import re

import harness_tasks as tasks
from dataset_checks import projection
from dataset_checks.references import KINDS, REFERENCE_TYPES, nested_references, reference_body, reference_rows
from production_review import _save_fixed, scientific_path


def packet_bodies(root, packet):
    bodies, unavailable, ambiguous = {}, [], set()
    for ref in packet['artifact_refs']:
        path = Path(ref['path'])
        if path.suffix.lower() not in ('.json','.jsonl'):
            continue
        try:
            path=scientific_path(root,path)
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=ref['sha256']:
                raise ValueError('Original frozen artifact changed.')
            rows = reference_rows(path,raw)
        except (OSError,ValueError):
            unavailable.append(ref)
            continue
        for row in rows:
            key, body = reference_body(row)
            if not body and isinstance(row,dict):
                key, body = reference_body(row.get('content'))
            if not body:
                continue
            if key in bodies and bodies[key]['body'] != body:
                ambiguous.add(key)
            bodies[key] = {'ref':ref,'body':body}
    for key in ambiguous:
        bodies.pop(key)
    return bodies, unavailable, sorted(ambiguous)


def _original_opinion(root, previous):
    from scientific_review.rounds import completed_return, recorded_assessments, _original_text
    with contextlib.closing(tasks._connect(root,True)) as c:
        assessments = recorded_assessments(c, previous['task_id'])
        for row in c.execute('SELECT * FROM work_returns WHERE request_id=? ORDER BY created_at DESC,id DESC', (previous['id'],)):
            if (assessments.get(row['id'], {}).get('conclusion') == 'not_assessed' or not completed_return(c, row)):
                continue
            ref = tasks._decode(row)['result_ref']
            if _original_text(root, ref).strip():
                return {'return_id': row['id'], 'opinion_ref': tasks.evidence_ref(root, ref)}
    raise ValueError('The exact prior review has no complete original opinion. Select a returned review ID or request an initial review.')


def baseline(root, parent, native, current, prior_id):
    from scientific_review.queue import author_review
    previous, old = author_review(root,parent,native,prior_id,allow_lineage=True)
    current_bodies, _, _ = packet_bodies(root,current)
    old_bodies, _, _ = packet_bodies(root,old)
    if not ({key[:2] for key in current_bodies} & {key[:2] for key in old_bodies}):
        raise ValueError('No exact scientific object links these review packets. Select the matching original review or request an initial review.')
    return {'review_id':previous['id'],'submission_ref':previous['input_ref'], **_original_opinion(root,previous)}


def prior_candidates(root, parent, current, *, at=None, connection=None):
    """Match exact known objects. Only an unambiguous author pointer wins across purposes."""
    from production_review import _review_packet
    from harness_package import author_lineage
    from scientific_review.rounds import completed_return, recorded_assessments, _original_text
    wanted = {key[:2] for key in packet_bodies(root,current)[0]}
    if not wanted:
        return None, []
    handles = set(re.findall(r'(?<![0-9a-f])[0-9a-f]{8,32}(?![0-9a-f])', current['review_scope']))
    at = at or tasks._now()
    with (contextlib.nullcontext(connection) if connection else contextlib.closing(tasks._connect(root,True))) as c:
        lineage = author_lineage(c,parent)
        assessments = recorded_assessments(c, parent['task_id'], at=at)
        rows = c.execute("""SELECT q.id,q.input_ref_json,ret.id AS return_id,ret.result_ref_json
          ,ret.run_id
          FROM work_requests q JOIN work_returns ret ON ret.request_id=q.id
          WHERE q.parent_request_id IN (SELECT value FROM json_each(?)) AND q.independent=1
          AND q.created_at<=? AND ret.created_at<=?
          ORDER BY ret.created_at DESC,ret.id""", (tasks._json(lineage),at,at))
        preferred, matches, same_purpose = [], [], []
        for row in rows:
            if (assessments.get(row['return_id'], {}).get('conclusion') == 'not_assessed'
                    or not completed_return(c, row, at=at)):
                continue
            handle = row['id'].split(':')[-1]
            explicit = any(handle.startswith(prefix) for prefix in handles)
            ref = json.loads(row['input_ref_json'])
            try:
                old = _review_packet(root,ref)
                known = {key[:2] for key in packet_bodies(root,old)[0]}
            except (KeyError,TypeError,ValueError,OSError):
                continue
            if not (wanted<=known and (explicit or wanted == known)):
                continue
            opinion = json.loads(row['result_ref_json'])
            try:
                if not _original_text(root, opinion).strip():
                    continue
            except (OSError, ValueError):
                # Keep an exact reference available to the caller even when
                # it cannot verify its text now. Do not select it automatically.
                matches.append({'review_id': row['id'], 'submission_ref': ref, 'return_id': row['return_id'],
                                'opinion_ref': opinion, 'original_unavailable': True})
                continue
            item = {'review_id':row['id'],'submission_ref':ref,'return_id':row['return_id'],
                    'opinion_ref':opinion}
            if explicit:
                preferred.append(item)
            if current.get('scientific_purpose') and old.get('scientific_purpose') == current['scientific_purpose']:
                same_purpose.append(item)
            matches.append(item)
    if len(preferred)==1:
        return preferred[0], matches
    if not preferred and len(same_purpose)==1:
        return same_purpose[0], matches
    return None, matches


def _values(before,after,pointer):
    result=[]
    for key in projection.field_changes(before,after,pointer):
        left,right = projection.pointer_value(before,key),projection.pointer_value(after,key)
        languages=projection.language_changes(left,right)
        result.append({'pointer':key,'before':left,'after':right,
                       **({'changed_languages':languages} if languages else {})})
    return result


def _reference_key(value):
    value = value if isinstance(value,dict) else {}
    return (REFERENCE_TYPES.get(str(value.get('@type','')).casefold()),value.get('@refObjectId'),value.get('@version'))


def _exchange(row, bodies, inventory):
    if row is None:
        return None
    ref = row.get('referenceToFlowDataSet')
    chain,status = projection.unit_chain(ref,bodies,inventory)
    return {'direction':row.get('exchangeDirection'),'mean_amount':row.get('meanAmount'),
            'resulting_amount':row.get('resultingAmount'),'declared_unit':row.get('unitName'),
            'reference_unit':projection.at(chain,'unit','name'),'unit_status':status,
            'exact_flow_ref':ref,'exchange':row}


def _exchange_diff(before, after, left, right):
    _, old = projection.exchange_members(before)
    _, new = projection.exchange_members(after)
    if old is None or new is None:
        return {'issues':['missing_or_ambiguous_exchange_id'],'changes':[]}
    by_flow_old, by_flow_new = defaultdict(list), defaultdict(list)
    for members,index in ((old,by_flow_old),(new,by_flow_new)):
        for number,row in members.items():
            key = _reference_key(row.get('referenceToFlowDataSet'))
            if all(isinstance(k,str) for k in key):
                index[key,row.get('exchangeDirection')].append(number)
    renumbered = [{'exact_flow_and_direction':key,'before':numbers[0],'after':by_flow_new[key][0]}
                  for key,numbers in by_flow_old.items() if len(numbers)==len(by_flow_new.get(key,[]))==1
                  and numbers!=by_flow_new[key]]
    changes=[]
    for number in sorted(old.keys()|new.keys()):
        row,next_row=old.get(number),new.get(number)
        if row!=next_row:
            changes.append({'exchange_id':number,'change':'added' if row is None else 'removed' if next_row is None else 'changed',
                            'before':_exchange(row,*left),'after':_exchange(next_row,*right),
                            'fields':_values(row,next_row,'') if row is not None and next_row is not None else []})
    return {'issues':['possible_exchange_renumbering'] if renumbered else [],'possible_renumbered':renumbered,'changes':changes}


def _unit_inputs(items):
    bodies = {'/'.join(key):item['body'] for key,item in items.items()}
    return bodies,{key:True for key in bodies}


def _paired(old,new):
    fingerprints = {key[:2] for key in old.keys()|new.keys()}
    pairs = [(key,key) for key in sorted(old.keys()&new.keys())]
    gaps = []
    for key in sorted(fingerprints):
        missing = sorted(k for k in old.keys()-new.keys() if k[:2]==key)
        added = sorted(k for k in new.keys()-old.keys() if k[:2]==key)
        if len(missing)==len(added)==1:
            pairs.append((missing[0],added[0]))
        elif missing or added:
            gaps.append({'kind':'unpaired_object_or_version','old':missing,'new':added})
    return pairs,gaps


def _consumers(bodies, identities):
    result=[]
    for key,item in bodies.items():
        for pointer,ref in nested_references(item['body']):
            target=_reference_key(ref)
            if all(isinstance(k,str) for k in target) and target in identities:
                result.append({'target':target,'consumer':key,'pointer':pointer,'packet_ref':item['ref']})
    return result


def compare(root, old_packet, new_packet, *, consumer_refs=()):
    old, missing_old, ambiguous_old = packet_bodies(root,old_packet)
    new, missing_new, ambiguous_new = packet_bodies(root,new_packet)
    pairs,gaps = _paired(old,new)
    changes, identities = [],set()
    left,right = _unit_inputs(old),_unit_inputs(new)
    for before_key,after_key in pairs:
        before,after = old[before_key],new[after_key]
        if before['body']==after['body']:
            continue
        body_key = next(key for key,value in KINDS.items() if value[0]==before_key[0])
        before_body,after_body = before['body'][body_key],after['body'][body_key]
        fields=_values(before['body'],after['body'],'')
        item={'old_identity':before_key,'new_identity':after_key,'old_ref':before['ref'],'new_ref':after['ref'],
              'fields':fields,'self_uri':{
                  name:projection.tiangong_self_uri(projection.at(body,'administrativeInformation','publicationAndOwnership',
                                                                 'common:permanentDataSetURI'),*key)
                  for name,body,key in (('before',before_body,before_key),('after',after_body,after_key))}}
        if before_key[0]=='process':
            exchanges = _exchange_diff(before_body,after_body,left,right)
            prefix='/processDataSet/exchanges'
            if not exchanges['issues']:
                fields=[field for field in fields if field['pointer']!=prefix+'/exchange']
            basis={}
            for name,body,key,inputs in (('before',before_body,before_key,left),('after',after_body,after_key,right)):
                found=[]
                for exchange_id in projection.array(projection.at(body,'processInformation','quantitativeReference','referenceToReferenceFlow')):
                    exchange=projection.internal_member(projection.at(body,'exchanges','exchange'),exchange_id)
                    row=_exchange(exchange,*inputs)
                    found.append({'id':exchange_id,'unit_chain':{'unit':{'name':row.get('reference_unit') if row else None}}})
                basis[name]=projection.declared_process_basis(body,key,exchanges=found)
            item.update(fields=fields,exchange_diff=exchanges,reference_basis=basis)
            for row in exchanges['changes']:
                for version in ('before','after'):
                    if row[version]:
                        ref=_reference_key(row[version]['exact_flow_ref'])
                        if all(isinstance(k,str) for k in ref):identities.add(ref)
        changes.append(item)
        identities.update((before_key,after_key))
    for gap in gaps:
        identities.update((*gap['old'],*gap['new']))
    extra,missing_consumers,ambiguous_consumers=packet_bodies(root,{'artifact_refs':consumer_refs})
    left_refs={ref.get('id',ref['path']):ref for ref in old_packet['artifact_refs']}
    right_refs={ref.get('id',ref['path']):ref for ref in new_packet['artifact_refs']}
    body_ref_ids={item['ref'].get('id',item['ref']['path']) for item in (*old.values(),*new.values())}
    supporting=[{'name':name,'before':left_refs.get(name),'after':right_refs.get(name)}
                for name in sorted((left_refs.keys()|right_refs.keys())-body_ref_ids)
                if (left_refs.get(name) or {}).get('sha256')!=(right_refs.get(name) or {}).get('sha256')]
    return {'schema':'htam.review-field-delta.v1','old_refs':old_packet['artifact_refs'],'new_refs':new_packet['artifact_refs'],
            'changes':changes,'alignment_gaps':gaps,'unreadable_json_artifacts':{'old':missing_old,'new':missing_new},
            'ambiguous_identities':{'old':ambiguous_old,'new':ambiguous_new},
            'supporting_changes':supporting,
            'known_consumers':{'old':_consumers(old,identities),'new':_consumers(new,identities),
                               'linked':_consumers(extra,identities)},
            'consumer_scope':{'basis':'Exact references in frozen artifacts and supplied original consumer records',
                              'linked_refs':list(consumer_refs),'unreadable':missing_consumers,
                              'ambiguous':ambiguous_consumers,'external':'unknown'}}


def freeze(root, directory, packet):
    from production_review import _review_packet
    source=packet['prior_review']
    old=_review_packet(root,source['submission_ref'])
    delta=compare(root,old,packet,consumer_refs=packet.get('consumer_refs', ()))
    ref=_save_fixed(root,directory/'field-delta.json',(json.dumps(delta,ensure_ascii=False,indent=2)+'\n').encode())
    changed=sum(len(x['fields'])+len(x.get('exchange_diff',{}).get('changes',[])) for x in delta['changes'])
    index=['# 冻结稿差异索引','','[完整字段值、交换和消费者](field-delta.json)。','']
    for item in delta['changes']:
        ids=['/'.join(item[key]) for key in ('old_identity','new_identity')]
        index.extend(['## '+(ids[0] if ids[0]==ids[1] else ' → '.join(ids)),''])
        index.extend('- `'+row['pointer']+'`'+(' · '+', '.join(row['changed_languages']) if row.get('changed_languages') else '')
                     for row in item['fields'])
        index.extend('- 交换 '+row['exchange_id']+' · '+row['change'] for row in item.get('exchange_diff',{}).get('changes',[]))
        index.extend('- '+problem for problem in item.get('exchange_diff',{}).get('issues',[]))
        index.append('')
    if delta['alignment_gaps'] or any(delta['ambiguous_identities'].values()) or any(delta['unreadable_json_artifacts'].values()):
        index.append('完整报告列出无法对齐、读取或唯一确认的对象。按原指针核完整材料。')
    index_ref=_save_fixed(root,directory/'field-delta-index.md',('\n'.join(index)+'\n').encode())
    return {'index_ref':index_ref,'delta_ref':ref,'objects_changed':len(delta['changes']),'fields_and_exchanges_changed':changed,
            'alignment_groups':len(delta['alignment_gaps'])}
