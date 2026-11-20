"""Lossless safe diagnostics. Presentation is separate from source/classification."""
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import threading

DIRECTORY = contextvars.ContextVar('diagnostic_directory', default=None)
_lock = threading.Lock()
# Match credential *values*, not words explaining an authorization failure.
_assignment = re.compile(r'''(?ix)(["']?(?:authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|secret)["']?\s*[:=]\s*)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''')
_bearer = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9_.~+/=-]+')
_jwt = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')
_key = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|sb_secret_[A-Za-z0-9_-]+)\b')
_callback = re.compile(r'(?i)([?&](?:code|state|access_token|refresh_token|id_token|session_state)=)[^&#\s]*')
SENSITIVE_KEYS = {'authorization', 'cookie', 'set-cookie', 'apikey', 'access_token', 'refresh_token',
                  'id_token', 'client_secret', 'password', 'secret', 'api_key', 'token', 'cookies', 'set_cookie',
                  'accesstoken', 'refreshtoken', 'idtoken', 'clientsecret', 'setcookie'}


def safe_text(value):
    value = str(value)
    value = _bearer.sub('Bearer [REDACTED]', value)
    value = _assignment.sub(lambda m: m[1] + '[REDACTED]', value)
    value = _jwt.sub('[REDACTED JWT]', value)
    value = _key.sub('[REDACTED KEY]', value)
    return _callback.sub(lambda m: m[1] + '[REDACTED]', value)


def safe_value(value, *, omit_transport_payloads=False):
    if isinstance(value, dict):
        return {k: '[OMITTED transport/auth payload]' if omit_transport_payloads and k.casefold() in ('body','response','request','headers') else '[REDACTED]' if k.casefold().replace('-', '_') in SENSITIVE_KEYS else safe_value(v, omit_transport_payloads=omit_transport_payloads)
                for k, v in value.items()}
    if isinstance(value, list):
        return [safe_value(v, omit_transport_payloads=omit_transport_payloads) for v in value]
    return safe_text(value) if isinstance(value, str) else value


def safe_error(value):
    if not isinstance(value, dict):
        return safe_value(value, omit_transport_payloads=True)
    allowed={'code','name','type','message','status','cause','details','stack'}
    result={k: safe_error(v) if k == 'cause' else safe_value(v, omit_transport_payloads=True) for k,v in value.items() if k in allowed}
    omitted=sorted(set(value)-allowed)
    if omitted:result['omitted_fields']=omitted
    return result


def retain(value, *, directory=None, source='native_diagnostic', code=None, complete=True):
    """Content-addressed safe event, written once. Failure never leaks raw text."""
    directory = directory or DIRECTORY.get()
    if directory is None:
        return {'code': code, 'source': source, 'diagnostic_available': False,
                'detail': 'diagnostic unavailable: no original run directory'}
    body = {'source': source, 'code': code, 'complete': complete, 'diagnostic': safe_value(value, omit_transport_payloads=True)}
    raw = (json.dumps(body, ensure_ascii=False, sort_keys=True) + '\n').encode()
    fingerprint = hashlib.sha256(raw).hexdigest()
    path = Path(directory) / ('diagnostic-' + fingerprint + '.json')
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open('xb') as f:
                    f.write(raw); f.flush(); os.fsync(f.fileno())
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise OSError('Diagnostic content conflict')
    except OSError:
        return {'code': code, 'source': source, 'diagnostic_available': False,
                'detail': 'diagnostic unavailable: safe record could not be saved'}
    return {'code': code, 'source': source, 'diagnostic_available': True, 'complete': complete,
            'detail': 'Full safe diagnostic not expanded; read diagnostic_ref.',
            'diagnostic_ref': {'path': str(path), 'sha256': fingerprint}}
