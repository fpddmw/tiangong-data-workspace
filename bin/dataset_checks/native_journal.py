"""Read unknown operations from the installed CLI dataset journal."""
import hashlib
import json
from pathlib import Path

NATIVE_PENDING = ('dispatched', 'unknown')


def pending_writes(env, *targets):
    """Read native journal headers; large completed canonical bodies are never loaded."""
    home = Path(env.get('HOME') or Path.home()) / '.local/state/tiangong-lca/dataset-operations'
    fields = ('type','id','version','actor','environment')
    identities = {(*target['object'].split('/'),target['account'],target['environment']):target['object'] for target in targets}
    needles = {key[1].encode('ascii') for key in identities}
    rows = []
    for path in home.glob('*.json'):
        try:
            with path.open('rb') as handle:
                prefix = handle.read(2048)
                cut = prefix.find(b'"intent":')  # Published journal writes fixed headers before the large intent.
                if cut >= 0:
                    header_bytes=prefix[:cut]
                    if not any(needle in header_bytes.lower() for needle in needles):
                        continue
                    header = json.loads(header_bytes.rstrip().rstrip(b',') + b'}')
                else:
                    header = json.loads(prefix + handle.read())
            key=tuple(header.get(k) for k in fields)
            if (header.get('schemaVersion') == 1 and key in identities
                    and header.get('state') in NATIVE_PENDING):
                request_id = header.get('requestId')
                if isinstance(request_id,str) and path.name == hashlib.sha256(json.dumps(request_id,ensure_ascii=False).encode()).hexdigest()+'.json':
                    rows.append({'request_id':request_id,'action':header['action'],'state':header['state'],'object':identities[key]})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return sorted(rows,key=lambda row:row['request_id'])
