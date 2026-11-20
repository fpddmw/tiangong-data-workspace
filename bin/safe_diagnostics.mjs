// Keep these rules aligned with safe_diagnostics.py and shared fixtures.
export function safeText(value) {
  return String(value)
    .replace(/\bBearer\s+[A-Za-z0-9_.~+/=-]+/gi, 'Bearer [REDACTED]')
    .replace(/(["']?(?:authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|secret)["']?\s*[:=]\s*)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)/gi, '$1[REDACTED]')
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[REDACTED JWT]')
    .replace(/\b(?:sk-[A-Za-z0-9_-]{12,}|sb_secret_[A-Za-z0-9_-]+)/g, '[REDACTED KEY]')
    .replace(/([?&](?:code|state|access_token|refresh_token|id_token|session_state)=)[^&#\s]*/gi, '$1[REDACTED]')
}
export function safeValue(value, omitTransportPayloads=false) {
  if (Array.isArray(value)) return value.map(item=>safeValue(item,omitTransportPayloads))
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) =>
    [k, omitTransportPayloads && /^(body|response|request|headers)$/.test(k.toLowerCase()) ? '[OMITTED transport/auth payload]' : /^(authorization|cookie|cookies|set_cookie|setcookie|apikey|api_key|access_token|accesstoken|refresh_token|refreshtoken|id_token|idtoken|client_secret|clientsecret|password|secret|token)$/.test(k.toLowerCase().replaceAll('-','_')) ? '[REDACTED]' : safeValue(v,omitTransportPayloads)]))
  return typeof value === 'string' ? safeText(value) : value
}
export function safeError(value) {
  if (!value || typeof value !== 'object') return safeValue(value,true)
  const allowed=['code','name','type','message','status','cause','details','stack']
  const result=Object.fromEntries(Object.entries(value).filter(([k])=>allowed.includes(k)).map(([k,v])=>[k,k==='cause'?safeError(v):safeValue(v,true)]))
  const omitted=Object.keys(value).filter(k=>!allowed.includes(k)).sort()
  if(omitted.length)result.omitted_fields=omitted
  return result
}
