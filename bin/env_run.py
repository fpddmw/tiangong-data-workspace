#!/usr/bin/env python3
"""Load one private env entry and inject a role-specific subset into an argv process."""
import argparse,json,os,re,stat,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent

def cli_state_home(root):
 home=Path(root).resolve()/'.harness-local/cli-state'
 if home.resolve()!=home or not home.resolve().is_relative_to(Path(root).resolve()):
  raise ValueError('CLI state directory must remain in the original workspace')
 return home

def read_env(path):
 info=path.lstat()
 if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:raise ValueError('Central env must be an owned regular file with 0600 permissions')
 result={}
 for n,line in enumerate(path.read_text().splitlines(),1):
  line=line.strip()
  if not line or line.startswith('#'):continue
  if line.startswith('export '):line=line[7:].lstrip()
  key,sep,value=line.partition('=')
  if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key):raise ValueError(f'Invalid env field on line {n}')
  value=value.strip()
  if value.startswith('"'):value=json.loads(value)
  elif value.startswith("'") and value.endswith("'"):value=value[1:-1]
  result[key]=value
 return result

def cli_write_directories(root):
 # Retained task state and the current CLI's two native mutation journals.
 # CLI 0.3.1 stores these journals under HOME, independently of XDG_STATE_HOME.
 return [cli_state_home(root)/'tiangong-lca-cli',
         Path.home()/'.local/state/tiangong-lca/dataset-operations',
         Path.home()/'.local/state/tiangong-lca/review-operations']

def role_env(role,values,root=None):
 root=Path(root) if root is not None else ROOT
 keep=('PATH','HOME','USER','LOGNAME','TMPDIR','LANG','LC_ALL','TERM','SSL_CERT_FILE','SSL_CERT_DIR')
 keep+=('HTAM_DELIVERY_ROOT','HTAM_DELIVERY_RUN_ID','HTAM_DELIVERY_REQUEST_ID','HTAM_DELIVERY_RUN_HOME',
        'HTAM_DSH_CAPACITY_ROOT','HTAM_DSH_CAPACITY_RUN_ID')
 out={k:v for k,v in os.environ.items() if k in keep}
 out['XDG_STATE_HOME']=str(cli_state_home(root))
 public=('TIANGONG_LCA_API_BASE_URL','TIANGONG_LCA_REGION','TIANGONG_LCA_SUPABASE_PUBLISHABLE_KEY','TIANGONG_LCA_OAUTH_CLIENT_ID','TIANGONG_LCA_OAUTH_REDIRECT_URI')
 for k in public:
  if k in values:out[k]=values[k]
 if role in ('editor','manager'):
  key='DATA_EDITOR_SESSION_FILE' if role=='editor' else 'DATA_MANAGER_SESSION_FILE'
  if not values.get(key):raise ValueError(f'Missing {key}')
  out.update(TIANGONG_LCA_AUTH_MODE='oauth',TIANGONG_LCA_SESSION_FILE=values[key])
  accounts=json.loads((root/'.control/accounts.json').read_text())
  out.update(TIANGONG_LCA_EXPECTED_PROJECT_REF=accounts['project_ref'],
             TIANGONG_LCA_EXPECTED_USER_ID=accounts[role]['user_id'])
 if role=='research':
  for k,v in values.items():
   if k.startswith(('TIANGONG_REPORT_','TIANGONG_RESEARCH_','TIANGONG_REGION','TIANGONG_LCA_KB_SEARCH_','TIANGONG_LCA_UNSTRUCTURED_','TIANGONG_LCA_REVIEW_LLM_','DIFY_','TIANGONG_SCI_','TIANGONG_AI_','UNSTRUCTURED_')):out[k]=v
 if role=='storage-read':
  for k,v in values.items():
   if k.startswith('S3_'):out[k]=v
 if role=='database-read':
  if 'CONN' not in values:raise ValueError('Database connection unavailable')
  out['CONN']=values['CONN'];out['PGOPTIONS']='-c default_transaction_read_only=on'
 # No process receives all roles, inherited tokens or unrelated service credentials.
 return out

def tool_cwd(root):
 root=Path(root).resolve();cwd=root/'work'
 if not cwd.resolve().is_relative_to(root):
  raise ValueError('Tool execution directory must remain in the workspace')
 cwd.mkdir(exist_ok=True)
 if (cwd/'.env').exists() or (cwd/'.env').is_symlink():
  raise ValueError('Tool work directory must not contain a nested .env')
 return cwd

def tool_environment(root,env):
 """Expose configured commands; only the invoked command needs to be installed."""
 root=Path(root).resolve();spec=json.loads((root/'.control/runtime.json').read_text())
 selected=dict(env)
 paths=[]
 if spec.get('node'):
  paths.append(str((root/spec['node']).parent))
 for key,variable in (('cli','TIANGONG_LCA_CLI_COMMAND'),('ai_cli','TIANGONG_AI_CLI_BIN')):
  if spec.get(key):selected[variable]=str(root/spec[key])
 if selected.get('HTAM_DELIVERY_ROOT')==str(root) and selected.get('HTAM_DELIVERY_RUN_ID'):
  selected['TIANGONG_LCA_CLI_COMMAND']=str(Path(__file__).with_name('native_cli_capture.py'))
 selected['PATH']=os.pathsep.join([*paths,env.get('PATH','')])
 return selected

def skill_cli_environment(root,argv,env):
 """Use one installed command for direct CLI calls and ordinary skill scripts."""
 root=Path(root).resolve();skills=(root/'.agents/skills').resolve()
 if argv and Path(argv[0]).name in ('tiangong-lca','tiangong-ai'):
  spec=json.loads((root/'.control/runtime.json').read_text())
  key='cli' if Path(argv[0]).name=='tiangong-lca' else 'ai_cli'
  return [str(root/spec['node']),str(root/spec[key]),*argv[1:]],tool_environment(root,env),tool_cwd(root)
 if len(argv)<2 or argv[1].startswith('-'):
  return argv,env,root
 script=Path(argv[1]);script=(script if script.is_absolute() else root/script).resolve()
 try:relative=script.relative_to(skills)
 except ValueError:return argv,env,root
 if relative.parts[0] in ('foundry-tidas-import','foundry-tidas-authoring'):
  return argv,env,root
 if not script.is_file():return argv,env,root
 selected=tool_environment(root,env)
 interpreter=argv[0]
 if Path(interpreter).name in ('node','node.exe'):
  interpreter=str(root/json.loads((root/'.control/runtime.json').read_text())['node'])
 return [interpreter,str(script),*argv[2:]],selected,tool_cwd(root)

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('role',choices=['editor','manager','research','storage-read','database-read'])
 p.add_argument('--check',action='store_true')
 p.add_argument('--without-project-env',action='store_true',
                help='Run a bound ordinary research skill with no project env or home session')
 a,command=p.parse_known_args()
 argv=command[1:] if command and command[0]=='--' else command
 if a.without_project_env:
  if a.role!='research' or a.check or not argv:
   raise ValueError('--without-project-env requires an ordinary research skill command')
  argv,env,cwd=skill_cli_environment(ROOT,argv,role_env('research',{}))
  if 'TIANGONG_LCA_CLI_COMMAND' not in env:
   raise ValueError('--without-project-env requires a bound ordinary research skill')
  with tempfile.TemporaryDirectory(prefix='tiangong-local-skill-') as home:
   env['HOME']=home
   if len(argv)>=2 and Path(argv[1]).resolve()==(ROOT/json.loads((ROOT/'.control/runtime.json').read_text())['cli']).resolve():
    from delivery_evidence import execute_native
    raise SystemExit(execute_native(ROOT,argv[:2],argv[2:],env=env,cwd=cwd))
   raise SystemExit(subprocess.call(argv,env=env,cwd=cwd))
 envfile=ROOT/'.env'
 if envfile.stat().st_mode & 0o077:raise ValueError('.env permissions must be 0600')
 env=role_env(a.role,read_env(envfile))
 if a.check:print(json.dumps({'role':a.role,'injected_fields':sorted(set(env)-set(os.environ)),'secret_values':'never printed'}));return
 if not argv:raise ValueError('An explicit argv command is required')
 direct=bool(argv and Path(argv[0]).name=='tiangong-lca')
 argv,env,cwd=skill_cli_environment(ROOT,argv,env)
 if direct:
  from delivery_evidence import execute_native
  raise SystemExit(execute_native(ROOT,argv[:2],argv[2:],env=env,cwd=cwd))
 raise SystemExit(subprocess.call(argv,env=env,cwd=cwd))
if __name__=='__main__':
 try:main()
 except (ValueError,OSError,json.JSONDecodeError) as exc:print(json.dumps({'error':str(exc)}),file=sys.stderr);sys.exit(1)
