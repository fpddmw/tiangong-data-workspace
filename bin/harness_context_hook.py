#!/usr/bin/env python3
"""Fail-open, read-only responsibility context for registered native identities."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import harness_tasks as t
import harness_snapshot as snapshot
from execution_lease import FileLock,atomic_json,ExecutionBusy
from safe_diagnostics import safe_value

ROOT=Path(__file__).resolve().parent.parent
EVENTS=('SessionStart','UserPromptSubmit','PostToolUse')


def _diagnostic(root,session,code,detail):
    # A deletable derived diagnostic, once per session/error. Never a task write.
    try:
        ident=hashlib.sha256(str(session or 'unidentified').encode()).hexdigest()
        key=hashlib.sha256(str(code).encode()).hexdigest()
        path=Path(root)/'.control/hook-cache/diagnostics'/ident/(key+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('x') as f:json.dump(safe_value({'code':code,'detail':detail}),f,ensure_ascii=False)
        except FileExistsError:pass
    except OSError:pass


def event_actor(root,event,environment):
    session=event.get('session_id');cwd=event.get('cwd')
    if not isinstance(session,str) or not session.strip() or '\0' in session:raise ValueError('Missing complete native session_id')
    if not isinstance(cwd,str) or not Path(cwd).resolve(strict=True).is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError('Event working directory is outside the formal workspace')
    if environment.get('CODEX_THREAD_ID') and environment['CODEX_THREAD_ID']!=session:
        raise ValueError('Event and environment native identities conflict')
    actor={'host':'codex','kind':'thread','id':session,'instance':session,'cwd':str(root)}
    child=event.get('agent_id') or event.get('subagent_id')
    if child:
        with contextlib.closing(t._connect(root,True,timeout=0.1)) as c:
            rows=c.execute("SELECT native_identity_json,request_id FROM work_runs WHERE target_host='codex' AND native_identity_json IS NOT NULL")
            for row in rows:
                native=json.loads(row[0])
                if (native.get('agent_id')==child and native.get('id')==session or
                    native.get('id')==child and native.get('parent_session_id')==session):
                    return native,row['request_id']
        raise ValueError('Exact child-session binding is not established; parent context withheld')
    return actor,None


def emit_context(root,event,*,environment=None):
    environment=os.environ if environment is None else environment
    session=event.get('session_id') if isinstance(event,dict) else None
    try:
        if not isinstance(event,dict):raise ValueError('Hook event must be a JSON object')
        name=event.get('hook_event_name')
        if name not in EVENTS:return {}
        if name=='SessionStart' and event.get('source') not in ('startup','resume','clear','compact'):return {}
        actor,bound_request=event_actor(root,event,environment)
        value=snapshot.bundle(root,actor,request_id=bound_request)
        task_set=sorted(view['task']['id'] for view in value['tasks'])
        cache_key=hashlib.sha256(t._json([t.native_key(actor),actor.get('agent_id'),bound_request]).encode()).hexdigest()
        path=Path(root)/'.control/hook-cache'/(cache_key+'.json')
        with FileLock(root,['context-cache',cache_key]):
            try:cache=json.loads(path.read_bytes())
            except (OSError,ValueError):cache={'epoch':0}
            if name=='SessionStart':cache['epoch']=cache.get('epoch',0)+1
            epoch=cache.get('epoch',0)
            key={'native_session_id':actor['id'],'task_set':task_set,'snapshot_hash':value['snapshot_hash'],'context_epoch':epoch}
            if name!='SessionStart' and cache.get('last')==key:return {}
            text=snapshot.render(value)
            if len(text.encode())>snapshot.HOOK_INLINE_BUDGET_BYTES:raise ValueError('Final context exceeds the local inline budget')
            output={'hookSpecificOutput':{'hookEventName':name,'additionalContext':text}}
            # Successful construction only. No end-to-end stdout acknowledgment
            # is claimed; every SessionStart restores independently of this cache.
            atomic_json(path,{'epoch':epoch,'last':key})
            return output
    except (ValueError,OSError,KeyError,TypeError,t.sqlite3.Error,TimeoutError,ExecutionBusy) as exc:
        _diagnostic(root,session,type(exc).__name__+':'+str(exc),str(exc))
        return {}


def dsh_context(root,request_id,run_id,session_id,*,budget=snapshot.HOOK_INLINE_BUDGET_BYTES):
    """Host-known DSH request/run/native Session, never inherited parent identity."""
    with contextlib.closing(t._connect(root,True,timeout=0.1)) as c:
        run=t._row(c,'work_runs',run_id)
        if run['request_id']!=request_id or not run['native_identity_json']:raise ValueError('Original run/native binding is not observed yet')
        native=json.loads(run['native_identity_json'])
        if native.get('host')!='deepseek_harness' or native.get('id')!=session_id:raise ValueError('DSH native Session differs from its original run')
    value=snapshot.bundle(root,native,request_id=request_id)
    return {'text':snapshot.render(value,budget=budget),'snapshot_hash':value['snapshot_hash']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--dsh',action='store_true');parser.add_argument('--request');parser.add_argument('--run-id');parser.add_argument('--session-id')
    parser.add_argument('--inline-budget',type=int,default=snapshot.HOOK_INLINE_BUDGET_BYTES)
    args=parser.parse_args(argv);root=args.root.resolve()
    if not 1024<=args.inline_budget<=snapshot.HOOK_INLINE_BUDGET_BYTES:parser.error('Inline budget must be 1024..8192 bytes')
    previous=None
    if threading.current_thread() is threading.main_thread():
        previous=signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM,lambda *_:(_ for _ in ()).throw(TimeoutError('Local context read exceeded one second')))
        signal.setitimer(signal.ITIMER_REAL,1)
    try:
        if args.dsh:
            try:result=dsh_context(root,args.request,args.run_id,args.session_id,budget=args.inline_budget)
            except (ValueError,OSError,KeyError,TypeError,t.sqlite3.Error) as exc:
                _diagnostic(root,args.session_id,type(exc).__name__,str(exc));result={'text':''}
        else:
            try:event=json.load(sys.stdin)
            except ValueError:event={}
            result=emit_context(root,event)
        print(json.dumps(result,ensure_ascii=False))
    except (ValueError,OSError,KeyError,TypeError,t.sqlite3.Error,TimeoutError) as exc:
        _diagnostic(root,args.session_id,type(exc).__name__,str(exc));print('{}')
    finally:
        if previous is not None:signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
    return 0


if __name__=='__main__':raise SystemExit(main())
