"""Isolated native-context facts; never open the production database."""
import hashlib,json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin'))
import harness_tasks as t
import harness_scope as s
root=Path(sys.argv[2]).resolve(strict=True)
if not (root/'ISOLATED-CONTEXT-FIXTURE').is_file():raise ValueError('Explicit isolated fixture marker required')
actor=t.verified_actor(root)
def ref(path):return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
if sys.argv[1]=='init':
 path=root/'original.md';path.write_text('Isolated native context qualification; no production, credentials, model service or remote writes.')
 source=ref(path);t.initialize(root,actor,source,'isolated-native-context')
 config=root/'runtime.json';config.write_text('{"fixture":"isolated-native-context"}')
 for name,count in [('A',3),('B',9)]:
  ident='native-context-'+name
  t.mutate(root,'create',actor,{'id':ident,'owner_actor':actor,'original_ref':source,'plan_ref':source,'summary':'Original '+ident,'required_scope':[]})
  ids=['P'+str(i) for i in range(count)]
  s.apply_scope(root,ident,{'items':[{'scope_id':sid,'kind':'deliverable','label':ident+' '+sid,'source_refs':[source]} for sid in ids],
   'verified_scope_ids':ids,'source_ref':source},actor)
  t.mutate(root,'request',actor,{'id':'request-'+name,'task_id':ident,'kind':'plan','recipient_role':'executor','input_ref':source,'summary':ident,
   'plan_revision':1,'required_scope':ids})
elif sys.argv[1]=='bind':
 name,session=sys.argv[3:5];run='run-'+name;config=ref(root/'runtime.json')
 t.mutate(root,'run-intent',actor,{'id':run,'request_id':'request-'+name,'config_ref':config,'input_refs':[ref(root/'original.md')],
  'target_host':'deepseek_harness','operation':'start'})
 native={'host':'deepseek_harness','kind':'session','id':session,'instance':session,'cwd':str(root)}
 receipt=root/('native-session-'+name+'.json')
 receipt.write_text(json.dumps({'schema':'data-workspace.agent-runtime-observation.v1','source':'native_cli_json','host':'deepseek_harness',
  'run_id':run,'request_id':'request-'+name,'config_sha256':config['sha256'],'native_identity':native,'native_status':'running',
  'observed_at':t._now(),
  'observation':{'source':'Native isolated Agent registry returned this exact Session before model work'}}))
 t.mutate(root,'run-bind',actor,{'id':run,'native_identity':native,'receipt':ref(receipt)})
else:raise ValueError('init or bind')
