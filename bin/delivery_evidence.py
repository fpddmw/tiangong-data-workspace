"""Original delivery evidence, selected bodies and native CLI observations.

This module reads facts. It does not validate remotely, adopt scientific uses,
select files by directory age, or replay an operation to obtain its receipt.
"""
import gzip
import hashlib
import json
import os
import errno
import sqlite3
import stat
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid

import harness_tasks as tasks

CLI_SCHEMA = 'htam.delivery-native-cli.v1'
CONTEXT_KEYS = ('HTAM_DELIVERY_ROOT', 'HTAM_DELIVERY_RUN_ID', 'HTAM_DELIVERY_REQUEST_ID', 'HTAM_DELIVERY_RUN_HOME')


def canonical_body(value):
    if isinstance(value, dict) and isinstance(value.get('json_ordered'), dict):
        inner = value['json_ordered']
        identity = body_identity(inner)
        if identity and all(value.get(key, expected) == expected
                            for key, expected in zip(('type', 'id', 'version'), identity)):
            return inner
    return value


def same_review_body(first, second):
    if not same_body(first, second):
        return False
    def context(value):
        # Retain all non-identity wrapper metadata and explicit validation context.
        fields = ('json_ordered', 'type', 'id', 'version')
        if canonical_body(value) is not value:
            return {key: item for key, item in value.items() if key not in fields}
        return {key: value[key] for key in ('semantic_context',) if key in value}
    return content_digest(context(first)) == content_digest(context(second))


def body_identity(body):
    if not isinstance(body, dict):
        return None
    body = canonical_body(body)
    for kind, key, info in [('process', 'processDataSet', 'processInformation'),
                           ('flow', 'flowDataSet', 'flowInformation'),
                           ('source', 'sourceDataSet', 'sourceInformation'),
                           ('contact', 'contactDataSet', 'contactInformation'),
                           ('model', 'lifeCycleModelDataSet', 'lifeCycleModelInformation')]:
        data = body.get(key)
        if not isinstance(data, dict):
            continue
        try:
            ident = data[info]['dataSetInformation']['common:UUID']
            version = data['administrativeInformation']['publicationAndOwnership']['common:dataSetVersion']
        except (KeyError, TypeError):
            return None
        return (kind, ident, version) if isinstance(ident, str) and isinstance(version, str) else None
    return None


def content_digest(body, *, ordered=False):
    raw = json.dumps(body, ensure_ascii=False, sort_keys=not ordered,
                     separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def same_body(first, second):
    return (body_identity(first) is not None and body_identity(first) == body_identity(second)
            and content_digest({k:v for k,v in canonical_body(first).items() if k != 'semantic_context'})
            == content_digest({k:v for k,v in canonical_body(second).items() if k != 'semantic_context'}))


def changed_paths(first,second,path=''):
    if type(first) is not type(second):return [path]
    if isinstance(first,dict):
        return [p for key in sorted(set(first)|set(second)) for p in (
            [path+'/'+key] if key not in first or key not in second else changed_paths(first[key],second[key],path+'/'+key))]
    if isinstance(first,list):
        if len(first)!=len(second):return [path]
        return [p for i,(a,b) in enumerate(zip(first,second)) for p in changed_paths(a,b,path+'/'+str(i))]
    return [] if first==second else [path]


def complete_envelope(body):
    if not isinstance(body, dict):
        return False
    completeness = body.get('completeness', {})
    return (isinstance(completeness, dict) and body.get('truncated') is not True
            and body.get('complete') is not False
            and not any(completeness.get(k) is True for k in ('truncated', 'hasMore', 'has_more'))
            and completeness.get('complete') is not False
            and completeness.get('completeContent') is not False)


def operation(argv):
    """Only actual public dataset/review operations; never auth responses."""
    if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
        return None
    if len(argv) >= 3 and argv[0] == 'dataset' and argv[2] in ('validate', 'create', 'save', 'get'):
        return ' '.join(argv[:3])
    if argv[:3] == ['dataset', 'model', 'result-group']:
        return 'dataset model result-group'
    if len(argv) >= 2 and argv[0] == 'review' and argv[1] in ('submit', 'resubmit'):
        return ' '.join(argv[:2])
    return None


def _json(root, ref):
    try:
        path = Path(tasks.evidence_ref(root, ref)['path'])
        if path.suffix=='.gz':
            with gzip.open(path,'rb') as compressed:
                limit=32*1024*1024
                data=compressed.read(limit+1)
                return json.loads(data) if len(data)<=limit else None
        return json.loads(path.read_bytes())
    except (OSError, ValueError, TypeError, KeyError, EOFError):
        return None


def _subject(value):
    if not isinstance(value, dict):
        return None
    result = (value.get('type'), value.get('id'), value.get('version'))
    return result if all(isinstance(v, str) and v for v in result) else None


def _output_only_failure(doc):
    codes = [w.get('code') for w in doc.get('warnings', []) if isinstance(w, dict)]
    error = doc.get('error')
    if isinstance(error, dict):
        codes.append(error.get('code'))
    return bool(codes) and all(c == 'OUTPUT_UNAVAILABLE' for c in codes)


def _read_success(doc):
    return (complete_envelope(doc) and
            (doc.get('status') == 'completed' and doc.get('error') is None
             or doc.get('status') == 'partial' and _output_only_failure(doc)))


def group_snapshot(value):
    """Read native Model/Process identity, revision and complete membership."""
    target=('model',value.get('modelId'),value.get('version'))
    snapshot=value.get('snapshot')
    if not isinstance(snapshot,dict) or not snapshot.get('revision'):
        return None
    model=snapshot.get('model')
    if not isinstance(model,dict) or body_identity(model.get('json_ordered'))!=target:
        return None
    if (model.get('id'),model.get('version'))!=target[1:]:
        return None
    members,processes=snapshot.get('members'),snapshot.get('processes')
    if not isinstance(members,list) or not isinstance(processes,list) or not all(
            isinstance(row,dict) for row in [*members,*processes]):
        return None
    keys=[(row.get('id'),row.get('version')) for row in members]
    objects={}
    for row in processes:
        ident=body_identity(row.get('json_ordered'))
        if (ident!=('process',row.get('id'),row.get('version')) or
                (row.get('model_id'),row.get('model_version') or row.get('version'))!=target[1:]):
            return None
        objects[ident]=row['json_ordered']
    if len(keys)!=len(set(keys)) or len(objects)!=len(processes) or set(keys)!={key[1:] for key in objects}:
        return None
    return snapshot, {target:model['json_ordered'],**objects}


def _group_document(doc):
    if doc.get('operation')=='dataset model result-group' and doc.get('schemaVersion')==1:
        value=doc.get('data')
        if isinstance(value,dict):
            return value, doc.get('context') or {}
    if doc.get('modelId') and doc.get('version') and 'status' in doc:
        return doc, {}
    return {}, {}


def _binding(value, context):
    actor=value.get('expectedActor') or (context.get('actor') or {}).get('id')
    environment=value.get('expectedEnvironment') or context.get('environment')
    return (actor,environment) if actor and environment else None


def _time(value):
    try:
        result=datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except (TypeError,ValueError):
        return None


def explicit_files(root, paths, *, home):
    """Freeze only the caller's exact paths, with generated technical identity."""
    from production_review import scientific_path, _save_fixed
    refs, seen = [], set()
    for value in paths:
        path = scientific_path(root, value)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        fixed = _save_fixed(root, Path(home) / 'evidence' / digest / path.name, raw)
        refs.append({'id': 'file:' + digest, 'version': digest, **fixed})
    return refs


def selected_files(root, paths, *, home):
    """Freeze the chosen body and retain any native envelope as evidence."""
    from production_review import _save_fixed
    refs = []
    identities = set()
    for original in explicit_files(root, paths, home=home):
        doc = _json(root, original)
        body = doc
        if isinstance(doc,dict) and doc.get('outcome')=='dataset_read' and _read_success(doc):
            data = doc['data']
            body = data.get('content')
            if body_identity(body) != _subject(data.get('subject')):
                raise ValueError('The selected readback must have the exact subject and full body.')
            completeness = doc['completeness']
            if not (completeness.get('returned')==1 and completeness.get('truncated') is False
                    and completeness.get('hasMore') is False):
                raise ValueError('Select a complete native readback or the original canonical body.')
        ident = body_identity(body)
        if not ident or ident[:2] in identities:
            raise ValueError('Select one exact canonical version per dataset identity.')
        identities.add(ident[:2])
        if body is doc:
            fixed = original
        else:
            refs.append(original)
            path = Path(original['path']).parent / 'selected-body.json'
            fixed = _save_fixed(root, path, tasks._json(body).encode())
        refs.append({**fixed,'id':'selected:'+':'.join(ident),'version':ident[2]})
    return refs


def context_environment(root, lease):
    return {CONTEXT_KEYS[0]:str(Path(root).resolve()),CONTEXT_KEYS[1]:lease.value['run_id'],
            CONTEXT_KEYS[2]:lease.value['request_id'],CONTEXT_KEYS[3]:str(lease.path.parent)} if lease else {}


def _option(argv, key):
    values=[]
    for index,arg in enumerate(argv):
        if arg==key and index+1<len(argv):values.append(argv[index+1])
        elif arg.startswith(key+'='):values.append(arg[len(key)+1:])
    return values[0] if len(values)==1 else None


def _capture_home(root, environment):
    from harness_package import launch_spec
    if environment.get(CONTEXT_KEYS[0])!=str(Path(root).resolve()) or not environment.get(CONTEXT_KEYS[1]):
        return None
    # The host supplies technical coordinates. The sandboxed native launcher
    # archives files only; it never needs management database write permission.
    if environment.get(CONTEXT_KEYS[2]) and environment.get(CONTEXT_KEYS[3]):
        directory=Path(environment[CONTEXT_KEYS[3]]).resolve()
        if directory.name!=environment[CONTEXT_KEYS[1]] or directory.parent.name!='runs':
            raise ValueError('Original native archive home does not match the run')
        return {'id':environment[CONTEXT_KEYS[1]],'request_id':environment[CONTEXT_KEYS[2]]},directory/'delivery'/('operation-'+uuid.uuid4().hex)
    with __import__('contextlib').closing(tasks._connect(root,True,timeout=.2)) as c:
        run=tasks._run_view(c,tasks._row(c,'work_runs',environment[CONTEXT_KEYS[1]]))
    spec=launch_spec(root,run)
    if not spec.get('evidence_home'):raise ValueError('Original native operation has no exact run home')
    return run,Path(spec['evidence_home'])/'runs'/run['id']/'delivery'/('operation-'+uuid.uuid4().hex)


def _association_notice(run_id,reason):
    value={'delivery_evidence':{'status':'needs_association','run_id':run_id,'reason':reason,
        'recovery':'python3 bin/harness.py work-return --run '+str(run_id)+' --artifact /ABS/ORIGINAL-RECEIPT'}}
    try:print(json.dumps(value,ensure_ascii=False),file=sys.stderr)
    except (OSError,ValueError):pass


def _no_dispatch(root, home, record, stage, error):
    from production_review import _save_fixed
    failure = {'stage':stage,'reason':type(error).__name__}
    if isinstance(error,OSError) and error.errno is not None:
        failure.update(system_code=errno.errorcode.get(error.errno,str(error.errno)),
                       system_reason=os.strerror(error.errno))
    elif isinstance(error,json.JSONDecodeError):
        failure.update(line=error.lineno,column=error.colno)
    elif isinstance(error,sqlite3.Error) and getattr(error,'sqlite_errorname',None):
        failure['system_code']=error.sqlite_errorname
    elif isinstance(error,ValueError) and stage=='authorization':
        failure['detail']=str(error)
    if record is not None:
        record.update(finished_at=tasks._now(),exit_code=1,dispatch='not_started',local_failure=failure)
        try:
            _save_fixed(root,home/'record.json',(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode())
        except (OSError,ValueError):
            failure['record_unavailable']=True
    print(json.dumps({'error':'Managed write withheld before native dispatch',**failure},ensure_ascii=False),file=sys.stderr)
    return 1


def _frozen_stdin(raw, archived):
    """Supply fd 0 from an anonymous regular file, never from the evidence path."""
    snapshot=tempfile.TemporaryFile(mode='w+b')
    try:
        info=os.fstat(snapshot.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink:
            raise ValueError('The execution snapshot must be an anonymous regular file.')
        if snapshot.write(raw)!=len(raw):
            raise OSError(errno.EIO,'Short execution snapshot write')
        snapshot.flush()
        snapshot.seek(0)
        if (os.fstat(snapshot.fileno()).st_size!=len(raw) or
                hashlib.file_digest(snapshot,'sha256').hexdigest()!=archived['sha256']):
            raise OSError(errno.EIO,'Execution snapshot and original archive differ')
        snapshot.seek(0)
        return snapshot
    except BaseException:
        snapshot.close()
        raise


def execute_native(root,command,argv,*,env,cwd):
    """Tee one already-authorized CLI call without changing its native result.

    Auth/identity, arbitrary scripts and non-context standalone calls retain the
    original execution path. Dataset inputs and operation receipts are the only
    captured payloads; environments/credentials are never recorded.
    """
    from production_review import scientific_path,_save_fixed
    from dataset_write_authority import admit, exact_action, public_argv
    root,cwd=Path(root).resolve(),Path(cwd).resolve()
    if os.environ.get(CONTEXT_KEYS[0]) != str(root) or not os.environ.get(CONTEXT_KEYS[1]):
        return subprocess.call(command+argv,env=env,cwd=cwd)
    argv = public_argv(argv)
    mutation = exact_action(argv)
    selected = operation(argv) or (' '.join(argv[:3]) if mutation else None)
    if not selected:
        return subprocess.call(command+argv,env=env,cwd=cwd)
    run_id=os.environ.get(CONTEXT_KEYS[1])
    try:
        run,home=_capture_home(root,os.environ)
        home=scientific_path(root,home,must_exist=False);home.mkdir(parents=True,exist_ok=False)
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        if mutation:
            return _no_dispatch(root,None,None,'archive_home',exc)
        _association_notice(run_id,type(exc).__name__+': original operation archive unavailable')
        return subprocess.call(command+argv,env=env,cwd=cwd)
    record={'schema':CLI_SCHEMA,'source':'trusted_public_cli','run_id':run['id'],'request_id':run['request_id'],
        'operation':selected,'argv':list(argv),'cwd':str(cwd),'capture_complete':False,'started_at':tasks._now()}
    input_arg,output_arg=_option(argv,'--input'),_option(argv,'--output')
    input_source,input_raw,input_error=None,None,None
    if not mutation and input_arg and input_arg!='-':
        try:
            input_source=scientific_path(root,Path(input_arg) if Path(input_arg).is_absolute() else cwd/input_arg)
            input_raw=input_source.read_bytes()
            record['input_ref']=_save_fixed(root,home/'input.json',input_raw)
        except (OSError,ValueError) as exc:input_error=type(exc).__name__
    execution_argv = argv
    if mutation:
        if not input_arg:
            return _no_dispatch(root,home,record,'input_option',ValueError())
        if input_arg!='-':
            try:
                input_source=scientific_path(root,cwd/input_arg,must_exist=False)
            except (OSError,ValueError) as exc:
                return _no_dispatch(root,home,record,'input_path',exc)
            try:
                source_fd=os.open(input_source,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK)
            except OSError as exc:
                return _no_dispatch(root,home,record,'input_open',exc)
            with os.fdopen(source_fd,'rb') as source:
                try:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise OSError(errno.EINVAL,'Managed input is not a regular file')
                    input_raw=source.read()
                except OSError as exc:
                    return _no_dispatch(root,home,record,'input_read',exc)
        else:
            try:
                input_raw=sys.stdin.buffer.read()
            except OSError as exc:
                return _no_dispatch(root,home,record,'input_read',exc)
        try:
            record['input_ref'] = _save_fixed(root, home/'input.json', input_raw)
        except (OSError,ValueError) as exc:
            return _no_dispatch(root,home,record,'input_archive',exc)
        record['input_bytes']=len(input_raw)
        # Only these direct dataset mutations have no relative-path operands in their JSON.
        if input_arg != '-':
            options = [i for i, arg in enumerate(argv) if arg == '--input' or arg.startswith('--input=')]
            i, = options
            end = i + (2 if argv[i] == '--input' else 1)
            execution_argv = [*argv[:i], '--input', '-', *argv[end:]]
    output_source,output_before=None,None
    if output_arg and output_arg!='-':
        try:
            output_source=scientific_path(root,Path(output_arg) if Path(output_arg).is_absolute() else cwd/output_arg,must_exist=False)
            if output_source.exists():
                output_before=(hashlib.sha256(output_source.read_bytes()).hexdigest(),output_source.stat().st_mtime_ns)
        except (OSError,ValueError):output_source=None
    errors=[]
    destinations={name:home/(name+'.bin') for name in ('stdout','stderr')}
    streams={}
    try:
        for name,path in destinations.items():streams[name]=path.open('xb')
    except OSError as exc:
        for stream in locals().get('streams',{}).values():stream.close()
        if mutation:
            return _no_dispatch(root,home,record,'result_archive',exc)
        _association_notice(run_id,'Original stdout/stderr archive unavailable')
        return subprocess.call(command+argv,env=env,cwd=cwd)
    inherited=dict(env)
    for key in (*CONTEXT_KEYS,'HTAM_DSH_CAPACITY_ROOT','HTAM_DSH_CAPACITY_RUN_ID'):
        if key in os.environ:inherited[key]=os.environ[key]
    snapshot=None
    try:
        if mutation:
            try:
                snapshot=_frozen_stdin(input_raw,record['input_ref'])
            except (OSError,ValueError) as exc:
                return _no_dispatch(root,home,record,'input_freeze',exc)
            record['input_transport']='anonymous_regular_file'
            try:
                decision = admit(root, run, mutation, input_raw, inherited)
            except (json.JSONDecodeError,UnicodeError) as exc:
                return _no_dispatch(root,home,record,'input_json',exc)
            except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
                return _no_dispatch(root,home,record,'authorization',exc)
            record['write_authority'] = decision
            if decision['status'] != 'admitted':
                record.update(finished_at=tasks._now(), exit_code=1, dispatch='not_started')
                _save_fixed(root,home/'record.json',(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode())
                print(json.dumps({'error': 'Managed mutation withheld', **decision}), file=sys.stderr)
                return 1
        process=subprocess.Popen(command+execution_argv,env=inherited,cwd=cwd,
            stdin=snapshot if mutation else subprocess.PIPE if input_arg=='-' else None,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        record['dispatch']='started'
    except OSError as exc:
        if mutation:
            return _no_dispatch(root,home,record,'native_start',exc)
        raise
    finally:
        if snapshot is not None:
            snapshot.close()
        if 'process' not in locals():
            for stream in streams.values():stream.close()
    def drain(name,source,destination):
        try:
            while chunk:=source.read1(65536):
                try:streams[name].write(chunk)
                except OSError:errors.append(name+'_archive_failed')
                try:destination.write(chunk);destination.flush()
                except (OSError,ValueError):pass  # A closed preview never replays an action.
        finally:
            try:streams[name].flush();streams[name].close()
            except (OSError,ValueError):errors.append(name+'_archive_failed')
            source.close()
    stdout=getattr(sys.stdout,'buffer',sys.stdout);stderr=getattr(sys.stderr,'buffer',sys.stderr)
    threads=[threading.Thread(target=drain,args=(name,getattr(process,name),target),daemon=True)
             for name,target in [('stdout',stdout),('stderr',stderr)]]
    for thread in threads:thread.start()
    def pipe_input():
        output=None
        try:
            try:output=(home/'input.json').open('xb')
            except OSError:errors.append('stdin_archive_failed')
            while chunk:=sys.stdin.buffer.read1(65536):
                if output is not None:
                    try:output.write(chunk)
                    except OSError:errors.append('stdin_archive_failed')
                try:process.stdin.write(chunk);process.stdin.flush()
                except (OSError,ValueError):break
        finally:
            if output is not None:
                try:output.close()
                except OSError:errors.append('stdin_archive_failed')
            try:process.stdin.close()
            except (OSError,ValueError):pass
    if not mutation and input_arg=='-':
        input_thread=threading.Thread(target=pipe_input,daemon=True)
        input_thread.start()
        threads.append(input_thread)
    exit_code=process.wait()
    for thread in threads:thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):errors.append('original_stream_not_closed')
    try:
        record['exit_code']=exit_code
        record['finished_at']=tasks._now()
        record['capture_complete']=not errors
        if errors:record['capture_issues']=sorted(set(errors))
        for name,path in destinations.items():
            if not any(t.is_alive() for t in threads):
                record[name+'_ref']={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        if mutation:
            record['input_unchanged'] = True  # Executed bytes were supplied by the host.
        elif input_arg=='-' and (home/'input.json').exists() and not errors:
            input_raw=(home/'input.json').read_bytes()
            record['input_ref']={'path':str(home/'input.json'),'sha256':hashlib.sha256(input_raw).hexdigest()}
            record['input_unchanged']=True
        elif input_source is not None and input_raw is not None:
            record['input_unchanged']=input_source.read_bytes()==input_raw
        else:record['input_unchanged']=False
        if input_error:record['input_issue']=input_error
        if input_raw is not None:
            try:value=json.loads(input_raw)
            except (ValueError,UnicodeError):value=None
            body=value.get('content') if isinstance(value,dict) and not body_identity(value) else value
            if ident:=body_identity(body):
                fixed=_save_fixed(root,home/'candidate.json',json.dumps(body,ensure_ascii=False,indent=2).encode())
                record['candidate_ref']={'id':':'.join(ident),'version':ident[2],**fixed}
        if output_source is not None and output_source.is_file():
            raw=output_source.read_bytes();digest=hashlib.sha256(raw).hexdigest()
            record['output_fresh']=output_before is None or digest!=output_before[0] or (
                '--force' in argv and output_source.stat().st_mtime_ns!=output_before[1])
            if record['output_fresh']:record['output_ref']=_save_fixed(root,home/'output.json',raw)
        fixed=_save_fixed(root,home/'record.json',(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode())
        if errors:_association_notice(run['id'],'; '.join(sorted(set(errors))))
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        _association_notice(run['id'],type(exc).__name__+': successful native result retained; explicit association is available')
    return exit_code


def ingest_run(root,run,actor):
    """Trusted host indexes exact archives; failure never changes native work."""
    from harness_package import launch_spec
    from production_review import scientific_path
    try:
        spec=launch_spec(root,run)
        if not spec.get('evidence_home'):return {'status':'no_original_archive_home'}
        home=scientific_path(root,Path(spec['evidence_home'])/'runs'/run['id']/'delivery',must_exist=False)
        refs=[]
        for path in sorted(home.glob('operation-*/record.json')):
            raw=path.read_bytes();doc=json.loads(raw)
            if (not isinstance(doc,dict) or doc.get('run_id')!=run['id'] or doc.get('request_id')!=run['request_id']
                    or doc.get('schema')!=CLI_SCHEMA or doc.get('source')!='trusted_public_cli'):continue
            refs.append({'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()})
        if refs:
            with tasks.transaction(root,timeout=.2) as c:
                for ref in refs:tasks.apply(c,root,'run-delivery-evidence',actor,{'id':run['id'],'record_ref':ref})
        return {'status':'indexed','records':len(refs)}
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        return {'status':'association_pending','reason':type(exc).__name__,
                'recovery':'Read the same original run again or explicitly associate its saved record.json; no native action is replayed.'}


class EvidenceSet:
    """One request/continuation collection, retaining per-object bindings."""
    def __init__(self, c, root, request, returned, reader, *, extra_refs=()):
        from harness_package import author_lineage
        import harness_scope
        self.c, self.root, self.request, self.returned, self.reader = c, Path(root), request, returned, reader
        self.lineage = author_lineage(c, request)
        self.inventory, self.parsed, self.records, self.selection_missing = {}, {}, [], []
        self.history = [r for r in harness_scope._returns(c, request['task_id']) if r['request_id'] in self.lineage]
        for r in self.history:
            self.add(r['result_ref'])
            for ref in r['artifact_refs']:
                self.add(ref)
        for ref in [returned['result_ref'], *returned.get('artifact_refs', []), *extra_refs]:
            self.add(ref)
        placeholders = ','.join('?' for _ in self.lineage)
        events = c.execute('''SELECT e.*,r.request_id,r.created_at AS run_created FROM work_run_events e
            JOIN work_runs r ON r.id=e.run_id WHERE r.request_id IN (''' + placeholders + ''')
            AND e.kind='delivery-evidence' ORDER BY e.id''', self.lineage).fetchall()
        for event in events:
            payload = json.loads(event['payload_json'])
            ref = payload.get('record_ref')
            doc = self.peek(ref)
            if not isinstance(doc, dict) or doc.get('schema') != CLI_SCHEMA:
                continue
            if doc.get('run_id') != event['run_id'] or doc.get('request_id') != event['request_id']:
                continue
            self.add(ref)
            for source in [doc.get('input_ref'), doc.get('stdout_ref'), doc.get('stderr_ref'), doc.get('output_ref')]:
                if source:
                    self.add(source)
            self.records.append((ref, doc, doc.get('started_at',event['run_created'])))
        # Explicit association is also the recovery path when archive indexing
        # failed after the native operation. It reads the same original record.
        known={tasks._json(ref) for ref,_,_ in self.records}
        for ref in list(self.inventory.values()):
            doc=self.peek(ref)
            if not isinstance(doc,dict) or doc.get('schema')!=CLI_SCHEMA or tasks._json(ref) in known:
                continue
            run=c.execute('SELECT * FROM work_runs WHERE id=? AND request_id IN ('+placeholders+')',
                          [doc.get('run_id'),*self.lineage]).fetchone()
            if not run or doc.get('request_id')!=run['request_id']:continue
            for key in ('input_ref','candidate_ref','stdout_ref','stderr_ref','output_ref'):
                self.add(doc.get(key))
            self.records.append((ref,doc,doc.get('started_at',run['created_at'])))
        self.records.sort(key=lambda item:item[2])
        self.artifacts = self._selected()

    def add(self, ref):
        if isinstance(ref, dict) and isinstance(ref.get('sha256'), str) and isinstance(ref.get('path'), str):
            self.inventory[tasks._json(ref)] = ref

    def resolve(self, ref):
        if not isinstance(ref, dict):
            return ref
        try:
            tasks.evidence_ref(self.root, ref)
            return ref
        except (OSError, ValueError, KeyError, TypeError):
            for alternative in self.inventory.values():
                if alternative.get('sha256') != ref.get('sha256'):
                    continue
                try:
                    tasks.evidence_ref(self.root, alternative)
                    return {**ref, **alternative}
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        return ref

    def peek(self, ref):
        resolved = self.resolve(ref)
        key = tasks._json(resolved)
        if key not in self.parsed:
            self.parsed[key] = _json(self.root, resolved)
        return self.parsed[key]

    def available(self, ref):
        try:
            tasks.evidence_ref(self.root, self.resolve(ref))
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def require(self, ref):
        return self.reader.read(self.resolve(ref))

    def _selected(self):
        cutoff = self.returned.get('created_at', '9999')
        choices = [self.returned, *reversed([r for r in self.history if r['created_at'] <= cutoff])]
        selected, selected_at = [], ''
        for report in choices:
            refs = report.get('artifact_refs', [])
            exact = [a for a in refs if a.get('id','').startswith('selected:')]
            if exact:
                selected = exact
                selected_at = report.get('created_at', '')
                break
            if any(body_identity(self.peek(a)) or not self.available(a) for a in refs):
                selected = list(refs)
                selected_at = report.get('created_at', '')
                break
        members = {}
        for ref in selected:
            body = self.peek(ref)
            if ident := body_identity(body):
                members[ident] = ref
            elif not self.available(ref):
                self.selection_missing.append(ref)
                self.require(ref)  # A missing selected original is not an empty selection.
        if exact and report is self.returned:
            return list(members.values())
        # A real validation/write input is an explicit choice by the author.
        # Remote gets of background references do not select working candidates.
        for record_ref, record, created in self.records:
            if created > cutoff or created < selected_at or not str(record.get('operation', '')).endswith((' validate', ' create', ' save')):
                continue
            body = self.peek(record.get('input_ref'))
            candidate = body.get('content') if isinstance(body, dict) and not body_identity(body) else body
            if not body_identity(candidate) or record.get('input_unchanged') is not True:
                continue
            ref = record.get('candidate_ref')
            if ref and same_body(self.peek(ref), candidate):
                self.add(ref)
                identity = body_identity(candidate)
                previous = members.get(identity)
                if previous is None or not same_body(self.peek(previous), candidate):
                    members[identity] = ref
        return list(members.values())

    def documents(self):
        result = []
        captured={tasks._json(record.get('stdout_ref')) for _,record,_ in self.records}
        for ref in list(self.inventory.values()):
            doc = self.peek(ref)
            if isinstance(doc, dict) and not body_identity(doc) and tasks._json(ref) not in captured:
                result.append((ref, doc, None))
        for record_ref, record, _ in self.records:
            ref = record.get('stdout_ref')
            doc = self.peek(ref)
            if isinstance(doc, dict):
                result.append((ref, doc, (record_ref, record)))
        return result

    def group_facts(self, body, facts, documents):
        writes, observed, unresolved = [], [], {}
        resolved=set()
        ident=body_identity(body)
        for ref,doc,provenance in documents:
            value,context=_group_document(doc)
            if not value or doc.get('error') or not complete_envelope(doc):
                continue
            status=value.get('status')
            record_ref,record=provenance or (None,{})
            raw=self.peek(record.get('input_ref')) if record.get('input_unchanged') else {}
            raw=raw if isinstance(raw,dict) else {}
            target=('model',value.get('modelId'),value.get('version'))
            if raw and (raw.get('modelId'),raw.get('version'))!=target[1:]:
                continue
            binding=_binding(value,context)
            expected=_binding(raw,{})
            if binding and expected and binding!=expected:
                continue
            binding=binding or expected
            receipt=value.get('receipt') or {}
            operation_id=receipt.get('operationId') or value.get('operationId')
            key=(target,binding,operation_id)
            if status=='unknown' and operation_id and target==ident:
                unresolved[key]=ref
            if status not in ('verified','observed','committed_readback_unverified'):
                continue
            if status in ('verified','committed_readback_unverified') and operation_id and binding:
                if not raw or raw.get('action') in ('save','recover') and (
                        raw.get('action')!='recover' or raw.get('operationId')==operation_id):
                    resolved.add(key)
            group=group_snapshot(value)
            if not group or not same_body(group[1].get(ident),body):
                continue
            snapshot,_=group
            if status=='observed' and binding and not receipt and raw.get('action','inspect')=='inspect':
                observed.append((ref,snapshot,binding))
            if (status=='verified' and operation_id and receipt.get('savedAt') and
                    receipt.get('revision')==snapshot['revision'] and receipt.get('members')==snapshot['members']
                    and raw.get('action','save') in ('save','recover')):
                writes.append((ref,snapshot,binding,record_ref,record.get('input_ref')))
        for ref,snapshot,binding in observed:
            self.require(ref)
            facts['full_readback'].append(ref)
            for saved,written,owner,record_ref,input_ref in writes:
                if written==snapshot and owner==binding and saved not in facts['saved']:
                    self.require(saved)
                    if record_ref and input_ref:
                        self.require(record_ref)
                        self.require(input_ref)
                    facts['saved'].append(saved)
        for key,ref in unresolved.items():
            if key not in resolved:
                self.require(ref)
                facts['unknown_effects'].append(ref)

    def _input_matches(self, record, candidate, *, scientific=False):
        if not record or record.get('capture_complete') is not True or record.get('input_unchanged') is not True:
            return False
        value = self.peek(record.get('input_ref'))
        if isinstance(value, dict) and not body_identity(value):
            value = value.get('content')
        return (same_review_body if scientific else same_body)(value, candidate)

    def native_facts(self, artifact):
        body = self.require(artifact)
        ident = body_identity(body)
        facts = {'structure': [], 'saved': [], 'full_readback': [], 'submitted': [], 'published': [],
                 'validation_coverage': [], 'unknown_effects': [], 'reading_limits': []}
        if not ident:
            return facts
        documents=self.documents()
        if ident[0] in ('model','process'):
            self.group_facts(body,facts,documents)
        digest_sources = {artifact['sha256']:artifact, content_digest(body):artifact,
                          content_digest(body,ordered=True):artifact}
        for source in self.inventory.values():
            value=self.peek(source)
            if isinstance(value,dict) and not body_identity(value) and body_identity(value.get('content')):
                value=value['content']
            if same_body(value,body):
                digest_sources[source['sha256']]=source
                digest_sources[content_digest(value,ordered=True)]=source
        canonical = canonical_body(body)
        for ordered in (False, True):
            digest_sources[content_digest(canonical, ordered=ordered)] = artifact
        validation_digests = {artifact['sha256']: artifact, content_digest(body): artifact,
                              content_digest(body, ordered=True): artifact}
        if isinstance(body, dict) and 'semantic_context' in body:
            validation_input = {**canonical, 'semantic_context': body['semantic_context']}
            for ordered in (True, False):
                validation_digests[content_digest(validation_input, ordered=ordered)] = artifact
        for source in self.inventory.values():
            value = self.peek(source)
            if isinstance(value, dict) and not body_identity(value) and body_identity(value.get('content')):
                value = value['content']
            if same_review_body(value, body):
                validation_digests[source['sha256']] = source
                for ordered in (True, False):
                    validation_digests[content_digest(value, ordered=ordered)] = source
        digests = set(digest_sources)
        submissions, submitted_readbacks = [], []
        for ref, doc, provenance in documents:
            record_ref, record = provenance or (None, None)
            operation_name = doc.get('operation', '')
            data, context = doc.get('data'), doc.get('context')
            data = data if isinstance(data, dict) else {}
            context = context if isinstance(context, dict) else {}
            subject = _subject(data.get('subject'))
            target = _subject((data.get('request') or {}).get('target')) if operation_name in ('review submit','review resubmit') else None
            relevant = subject == ident or target == ident or self._input_matches(record, body)
            if doc.get('schemaVersion') == 1 and relevant:
                if doc.get('outcome') in ('unknown', 'effect_unknown') and operation_name.endswith((' create', ' save', ' submit', ' resubmit', ' publish')):
                    self.require(ref);facts['unknown_effects'].append(ref)
                if (operation_name.endswith((' create', ' save')) and _read_success(doc)
                        and doc.get('outcome') in ('dataset_written','dataset_created','dataset_saved')
                        and doc.get('error') is None and context.get('actor', {}).get('id')
                        and context.get('environment') and context.get('requestId')):
                    digest = data.get('contentDigest') or context.get('effectiveInput', {}).get('contentDigest')
                    bound = digest in digests or same_body(data.get('content'), body) or self._input_matches(record, body)
                    if bound:
                        self.require(ref)
                        if digest in digest_sources:self.require(digest_sources[digest])
                        if record_ref:self.require(record_ref)
                        facts['saved'].append(ref)
                if (operation_name.endswith(' get') and doc.get('outcome') == 'dataset_read'
                        and _read_success(doc) and context.get('observation', {}).get('source') == 'remote'):
                    complete = doc.get('completeness', {})
                    complete = complete.get('returned') == 1 and complete.get('truncated') is False and complete.get('hasMore') is False
                    content, content_ref = data.get('content'), ref
                    # A partial output failure never points at the old disk file.
                    if content is None and doc.get('status') == 'completed':
                        outputs = []
                        if record and record.get('output_fresh') is True and record.get('output_ref'):
                            outputs.append(record['output_ref'])
                        for output in ([] if record else doc.get('artifacts', [])):
                            if output.get('purpose') != 'complete_result' or not output.get('path'):
                                continue
                            path = Path(output['path'])
                            path = path if path.is_absolute() else Path(ref['path']).parent / path
                            try:
                                raw = path.read_bytes()
                                if 'bytes' in output and len(raw) != output['bytes']:continue
                                outputs.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()})
                            except OSError:
                                continue
                        for output in outputs:
                            if same_body(self.peek(output), body):
                                content, content_ref = self.peek(output), output
                                break
                    if complete and same_body(content, body):
                        self.require(ref);self.require(content_ref)
                        if record_ref:self.require(record_ref)
                        facts['full_readback'].append(ref)
                        state = data.get('subject', {}).get('stateCode')
                        if state == 20 or type(state) is int and 100 <= state < 200:
                            submitted_readbacks.append((ref, content_ref, context, data['subject']))
                        if type(state) is int and 100 <= state < 200:facts['published'].append(ref)
                if (operation_name in ('review submit', 'review resubmit') and complete_envelope(doc)
                        and doc.get('status') in ('accepted','completed')
                        and doc.get('outcome') in ('review_submitted','review_resubmitted') and doc.get('error') is None):
                    native=(data.get('receipt') or {}).get('data') or {}
                    review=native.get('review') or {}
                    review_id=data.get('reviewId')
                    if (review_id and (target or subject)==ident and context.get('environment')
                            and (context.get('actor') or {}).get('id') and context.get('requestId')
                            and review.get('id',review_id)==review_id
                            and review.get('data_id',ident[1])==ident[1]
                            and review.get('data_version',ident[2])==ident[2]):
                        submissions.append((ref,context,review))
            validation = None
            if doc.get('schemaVersion') == 1 and operation_name.endswith(' validate'):
                validation = data.get('validation')
                if isinstance(validation, dict):
                    local = validation.get('local', validation)
                    bound = isinstance(local, dict) and local.get('payload_sha256') in validation_digests or self._input_matches(record, body, scientific=True)
                    if isinstance(local,dict) and _subject(local) not in (None,ident):bound=False
                    passed = (_read_success(doc) and doc.get('outcome') == 'validation_passed'
                              and validation.get('status') in ('passed', 'valid', 'completed'))
                else:
                    bound = passed = False
            elif doc.get('payload_sha256') in validation_digests and _subject(doc) == ident:
                validation, local = doc, doc
                bound = True
                passed = doc.get('status') == 'passed' and doc.get('ok') is not False
            elif isinstance(doc.get('local'),dict) and _subject(doc['local'])==ident and doc['local'].get('payload_sha256') in validation_digests:
                # --output contains the full native validation object, while
                # its stdout envelope may explicitly carry a truncated preview.
                validation,local=doc,doc['local'];bound=True
                passed=complete_envelope(doc) and doc.get('status')=='passed' and local.get('ok') is not False
            else:
                bound = passed = False
            if bound and passed:
                from lca_gate import native_validation_failed
                if native_validation_failed(local):continue
                self.require(ref)
                if (source := validation_digests.get(local.get('payload_sha256'))) is not None:
                    self.require(source)
                if record_ref:self.require(record_ref)
                facts['structure'].append(ref)
                facts['validation_coverage'].append({'ref': ref, 'checks': validation.get('checks', []),
                    'not_checked': validation.get('notChecked', []), 'local_output_failed': doc.get('status') == 'partial'})
            elif (relevant and operation_name.endswith(' validate')
                  or _subject(doc)==ident and ('validator' in doc or 'validation_layers' in doc)):
                self.require(ref)
                facts['reading_limits'].append({'ref':ref,'reason':
                    'Native validation lacks supported complete result/content binding; original retained, no inferred pass.'})
            if doc.get('schema') == 'data-workspace.lca-gate.v4' and artifact['sha256'] in doc.get('inputs', {}).values():
                from lca_gate import native_validation_failed
                okay = doc.get('outcome') in ('checks_completed', 'warning') and bool(doc.get('checks'))
                for check in doc.get('checks', []):
                    native_ref = {'path': str(Path(ref['path']).parent / check.get('native_report', 'missing')),
                                  'sha256': check.get('native_report_sha256', '')}
                    native = self.peek(native_ref)
                    if check.get('status') != 'passed' or not complete_envelope(check) or not complete_envelope(native) or native_validation_failed(native):
                        okay = False
                    self.require(native_ref)
                if okay:
                    self.require(ref)
                    for path, sha in doc.get('inputs', {}).items():self.require({'path':path,'sha256':sha})
                    facts['structure'].append(ref)
                else:
                    self.require(ref)
                    facts['reading_limits'].append({'ref':ref,'reason':'Bound gate contains a failed, incomplete or unavailable original native check.'})
        for ref,context,review in submissions:
            for readback,content_ref,read_context,subject in submitted_readbacks:
                issued=_time(review.get('created_at') or (context.get('observation') or {}).get('time'))
                observed=_time((read_context.get('observation') or {}).get('time'))
                if not (issued and observed and observed>=issued and
                        context['environment']==read_context.get('environment')):
                    continue
                checksum=review.get('submitted_revision_checksum')
                if checksum:
                    from review_adapter import native_body_checksum
                    try:
                        source=tasks.evidence_ref(self.root,content_ref)
                        raw=json.loads(Path(source['path']).read_bytes(),parse_float=Decimal)
                    except (OSError,ValueError):
                        continue
                    value=(raw.get('data') or {}).get('content') if content_ref==readback else raw
                    if checksum!=native_body_checksum(canonical_body(value)):
                        continue
                elif issued!=_time(subject.get('modifiedAt')):
                    continue
                self.require(ref)
                facts['submitted'].extend([ref,readback])
                break
        return facts


def return_artifacts(c, root, request, result):
    """An empty new turn is not a withdrawal of the last explicit submission."""
    import harness_scope
    if result.get('reviewed_artifacts'):
        return result['reviewed_artifacts']
    pseudo = {'result_ref':request['input_ref'], 'artifact_refs':[], 'created_at':tasks._now()}
    pool = EvidenceSet(c, root, request, pseudo, harness_scope.Evidence(root))
    if pool.artifacts:
        return pool.artifacts
    for previous in reversed(pool.history):
        if previous['artifact_refs']:
            return previous['artifact_refs']
    return []
