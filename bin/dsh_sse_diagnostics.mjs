// Observation at the native model stream and the installed OpenAI-compatible SDK.
import { AsyncLocalStorage } from 'node:async_hooks'
import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { safeText } from './safe_diagnostics.mjs'

const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')
const malformed = 'Error reading response: malformed server-sent event JSON.'
const currentRequest = new AsyncLocalStorage()
const observedResponses = new WeakMap()
const timestamp = () => Math.round(performance.timeOrigin + performance.now())
const errorCode = code => typeof code === 'string' && /^[A-Za-z][A-Za-z0-9_.-]{0,100}$/u.test(code) ? code : undefined

function requestIds(headers) {
  return Object.fromEntries(['x-request-id','request-id','x-deepseek-request-id','traceparent']
    .map(key => [key, headers.get(key)])
    .filter(([,value]) => value && /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/u.test(value))
    .map(([key,value]) => [key,safeText(value)]))
}

function observeSdkFetch(client, current) {
  const original = client?.fetchWithTimeout
  if (typeof original !== 'function') return () => {}
  async function fetchWithTimeout(url, init, ...args) {
    const attempt = current.getStore()
    if (!attempt) return original.call(this,url,init,...args)
    const request = {sent_at_ms:timestamp()}
    attempt.http.push(request)
    try {
      const headers = new Headers(init?.headers)
      if (!headers.has('traceparent')) {
        headers.set('traceparent', `00-${randomBytes(16).toString('hex')}-${randomBytes(8).toString('hex')}-00`)
      }
      const trace = headers.get('traceparent')
      if (/^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/u.test(trace)) request.client_traceparent = trace
      const response = await original.call(this,url,{...init,headers},...args)
      Object.assign(request, {headers_at_ms:timestamp(), status:response.status,
        request_ids:requestIds(response.headers)})
      observedResponses.set(response,request)
      return response
    } catch (error) {
      request.failed_at_ms = timestamp()
      throw error
    }
  }
  client.fetchWithTimeout = fetchWithTimeout
  return () => {if (client.fetchWithTimeout === fetchWithTimeout) client.fetchWithTimeout = original}
}

export async function installRequestTiming(ctx, moduleEntry, {provider, sessionId, position, output}) {
  let offFetch = () => {}
  try {
    const sdk = dirname(createRequire(moduleEntry).resolve('openai'))
    const {OpenAI} = await import(pathToFileURL(join(sdk,'client.mjs')))
    offFetch = observeSdkFetch(OpenAI?.prototype, currentRequest)
  } catch { /* The native content timestamps still apply to other adapters. */ }
  async function* observe(next, attempt) {
    let stream
    try {
      stream = currentRequest.run(attempt, () => next()[Symbol.asyncIterator]())
      while (true) {
        const item = await currentRequest.run(attempt, () => stream.next())
        if (item.done) break
        const chunk = item.value
        const at = timestamp()
        if ((['text-delta','reasoning-delta'].includes(chunk.type) && chunk.text?.length)
            || (chunk.type === 'tool-call-delta' && chunk.argumentsDelta?.length)
            || (chunk.type === 'block-end' && attempt.first_content_at_ms === undefined)) {
          attempt.first_content_at_ms ??= at
          attempt.max_content_gap_ms = Math.max(attempt.max_content_gap_ms ?? 0, at - (attempt.last_content_at_ms ?? at))
          attempt.last_content_at_ms = at
        }
        if (chunk.type === 'finish') {
          attempt.terminal = chunk.reason?.kind
          attempt.error_code = errorCode(chunk.reason?.failure?.code)
          attempt.finished_at_ms = at
        }
        yield chunk
      }
    } catch (error) {
      attempt.terminal = 'error'
      attempt.error_code = errorCode(error?.code)
      throw error
    } finally {
      try {await currentRequest.run(attempt, () => stream?.return?.())}
      finally {
        attempt.finished_at_ms ??= timestamp()
        output({type:'htam_request_timing', ...attempt})
      }
    }
  }
  const off = ctx.on('llm/stream', (options,next) => {
    const {turn,step} = position()
    if (options.provider !== provider || options.sessionId !== sessionId || !turn || !step) return next()
    return observe(next, {turn, step, started_at_ms:timestamp(), http:[]})
  })
  return () => {off();offFetch()}
}

function eventData(bytes, LineDecoder) {
  const decoder = new LineDecoder()
  const lines = [...decoder.decode(bytes), ...decoder.flush()]
  let event = null
  const values = []
  for (const raw of lines) {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /u, '')
    if (field === 'event') event = value
    if (field === 'data') values.push(value)
  }
  return event || values.length ? {event, data:values.join('\n')} : null
}

class FrameObserver {
  constructor(response, lineModule, config) {
    this.response = response
    this.lineModule = lineModule
    this.config = config
    this.attempt = currentRequest.getStore()
    const wire = observedResponses.get(response)
    this.wire = wire ? (wire.sse_body = {bytes_received:0}) : undefined
    this.pending = Buffer.alloc(0)
    this.normal = config.metadataOnly ? null : createHash('sha256')
    this.normalCount = 0
    this.offset = 0
    this.bytesRead = 0
    this.boundaries = []
    this.started = this.lastByte = performance.now()
    this.maxGap = 0
  }

  record(raw, boundary) {
    const item = eventData(raw, this.lineModule.LineDecoder)
    if (item?.data === '[DONE]') this.done = true
    if (item && !this.done) {
      try {
        const value = JSON.parse(item.data)
        const type = value?.type ?? item.event
        const failure = value?.error ?? value?.response?.error ?? value
        if (this.wire && ['error','response.failed'].includes(type)) {
          this.wire.error_event = {type,at_ms:timestamp(), code:errorCode(failure?.code),
            byte_length:raw.length,raw_sha256:sha256(raw)}
        }
      }
      catch {
        this.failure = {raw:Buffer.from(raw), boundary, event:item.event,
          start:this.offset, end:this.offset + raw.length, ordinal:this.normalCount + 1}
        return
      }
      this.normalCount++
      this.normal?.update(raw)
    }
    this.offset += raw.length
    this.boundaries = this.boundaries.filter(part => part.end > this.offset)
  }

  read({value,done}) {
    if (done) {
      if (this.wire) this.wire.eof_at_ms = timestamp()
      if (!this.done && !this.failure && this.pending.length) this.record(this.pending, 'eof')
      return
    }
    const chunk = Buffer.from(value)
    if (chunk.length) {
      const at = performance.now()
      this.firstByte ??= at
      this.maxGap = Math.max(this.maxGap, at-this.lastByte)
      this.lastByte = at
      if (this.wire) {
        const observedAt = Math.round(performance.timeOrigin + at)
        this.wire.first_byte_at_ms ??= observedAt
        this.wire.last_byte_at_ms = observedAt
        this.wire.bytes_received += chunk.length
      }
    }
    if (this.done || this.failure || this.oversized) return
    if ((this.config.minimal || this.config.metadataOnly) && this.pending.length + chunk.length > 262144) {
      this.pending = Buffer.alloc(0); this.boundaries = []; this.oversized = true
      return
    }
    if (!this.config.metadataOnly) this.boundaries.push({start:this.bytesRead,end:this.bytesRead + chunk.length})
    this.bytesRead += chunk.length
    this.pending = Buffer.concat([this.pending,chunk])
    while (!this.done && !this.failure) {
      const end = this.lineModule.findDoubleNewlineIndex(this.pending)
      if (end < 0) break
      this.record(this.pending.subarray(0,end), 'blank_line')
      this.pending = this.pending.subarray(end)
    }
  }

  requestIds() {
    return requestIds(this.response.headers)
  }

  readError(error) {
    if (this.wire) this.wire.read_error = {at_ms:timestamp(),name:errorCode(error?.name),code:errorCode(error?.code)}
  }

  write(prefix, value) {
    const directory = this.config.directory
    mkdirSync(directory, {recursive:true,mode:0o700})
    writeFileSync(join(directory,prefix + randomUUID() + '.json'), JSON.stringify(value,null,2) + '\n',
      {flag:'wx',mode:0o600})
  }

  settle(error) {
    if (this.settled) return
    this.settled = true
    const malformedError = error?.name === 'SyntaxError' && error.message === malformed
    if (malformedError && this.attempt) {
      const frame = this.failure
      this.attempt.sse_failures ??= []
      this.attempt.sse_failures.push({request_ids:this.requestIds(), status:this.response.status,
        frame:frame ? {boundary:frame.boundary, byte_length:frame.raw.length,
          raw_sha256:sha256(frame.raw)} : null})
    }
    if (this.config.metadataOnly) return
    if (this.config.timings) this.write('sse-timing-', {
      schema:'htam.dsh-sse-timing.v1', observed_at:new Date().toISOString(), request_ids:this.requestIds(),
      status:this.response.status, scope:'client_response_body_after_headers',
      headers_to_first_byte_ms:this.firstByte === undefined ? null : this.firstByte-this.started,
      max_observed_no_data_ms:Math.max(this.maxGap,performance.now()-this.lastByte),
    })
    if (!malformedError) return
    const {status,headers} = this.response
    const frame = this.failure
    const decoded = frame?.raw.toString('utf8')
    const retained = decoded === undefined || this.config.minimal ? undefined : safeText(decoded)
    const value = {schema:'htam.dsh-sse-diagnostic.v1', source:'installed_sdk_sse_response', observed_at:new Date().toISOString(),
      status, media_type:safeText(headers.get('content-type') ?? ''), request_ids:this.requestIds(),
      error:malformed, normal_event_count:this.normalCount, normal_events_sha256:this.normal.digest('hex'),
      bytes_observed:this.bytesRead, failed_event:frame ? {
        ...(!this.config.minimal ? {event:frame.event && safeText(frame.event)} : {}), ordinal:frame.ordinal, boundary:frame.boundary,
        byte_start:frame.start, byte_end:frame.end, byte_length:frame.raw.length, raw_sha256:sha256(frame.raw),
        chunks:this.boundaries.filter(part => part.start < frame.end && part.end > frame.start),
        utf8_lossless:Buffer.from(decoded).equals(frame.raw),
        ...(retained === undefined ? {body_omitted:true} : {
          redacted:retained !== decoded, retained_sha256:sha256(retained), retained_frame:retained}),
      } : null}
    this.write('sse-', value)
  }
}

export function observeSseDecoder(Stream, lineModule, config) {
  const original = Stream.fromSSEResponse
  const watched = new WeakSet()
  function fromSSEResponse(response,...args) {
    let observer
    try {
      const body = response.body
      if (body?.getReader && !watched.has(body) && (!config.metadataOnly || currentRequest.getStore())
          && (!config.origin || new URL(response.url).origin === config.origin)) {
        observer = new FrameObserver(response,lineModule,config)
        const getReader = body.getReader
        const capture = observer
        body.getReader = function (...args) {
          const reader = getReader.apply(this,args)
          try {
            const read = reader.read
            reader.read = function (...args) {
              const pending = read.apply(this,args)
              pending.then(result => {try {capture.read(result)} catch {}}, error => {try {capture.readError(error)} catch {}})
              return pending
            }
          } catch {}
          return reader
        }
        watched.add(body)
      }
    } catch {observer = undefined}
    const stream = original.call(this,response,...args)
    if (observer) {
      const iterator = stream.iterator
      stream.iterator = function (...args) {
        const decoded = iterator.apply(this,args)
        const next = decoded.next
        decoded.next = function (...args) {
          const pending = next.apply(this,args)
          pending.then(value => {if (value.done) try {observer.settle()} catch {}},
            error => {try {observer.settle(error)} catch {}})
          return pending
        }
        return decoded
      }
    }
    return stream
  }
  Stream.fromSSEResponse = fromSSEResponse
  return () => {if (Stream.fromSSEResponse === fromSSEResponse) Stream.fromSSEResponse = original}
}

export async function installSseDiagnostics(ctx, moduleEntry, config) {
  try {
    const sdk = dirname(createRequire(moduleEntry).resolve('openai'))
    const [stream,lines] = await Promise.all([
      import(pathToFileURL(join(sdk,'core/streaming.mjs'))),
      import(pathToFileURL(join(sdk,'internal/decoders/line.mjs'))),
    ])
    if (typeof stream.Stream?.fromSSEResponse !== 'function' || typeof lines.findDoubleNewlineIndex !== 'function'
        || typeof lines.LineDecoder !== 'function') throw new TypeError('Unsupported SDK decoder')
    ctx.effect(() => observeSseDecoder(stream.Stream,lines,config))
  } catch (error) {
    console.error('DSH SSE diagnostics unavailable: ' + safeText(error?.name ?? 'Error'))
  }
}
