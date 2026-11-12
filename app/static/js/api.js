// 与后端打交道的唯一入口。
// 所有请求都从这里过，好处是"带不带 API Key""错误信封长什么样"只有一处需要记住。
// 之前每个 fetch 各写各的，结果 SSE 端点返回 400 时前端还在按流解析，页面一片安静。

const KEY_STORE = 'documind.apiKey';

export class ApiError extends Error {
  constructor(message, { code = '', status = 0 } = {}) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export const apiKey = () => sessionStorage.getItem(KEY_STORE) || '';

export function setApiKey(k) {
  if (k) sessionStorage.setItem(KEY_STORE, k);
  else sessionStorage.removeItem(KEY_STORE);
}

export function authHeaders(extra = {}) {
  const k = apiKey();
  return k ? { ...extra, 'X-API-Key': k } : extra;
}

async function asJson(r) {
  if (!r.ok) {
    let message = `HTTP ${r.status}`;
    let code = '';
    try {
      const j = await r.json();
      const e = j.error || j.detail || {};
      if (e.message) message = e.message;
      if (e.code) code = e.code;
    } catch (_) { /* 非 JSON 错误体就用状态码 */ }
    throw new ApiError(message, { code, status: r.status });
  }
  return r.json();
}

export const config = () => fetch('/v1/config').then(asJson);
export const ready = () => fetch('/readyz', { headers: authHeaders() }).then(asJson);
export const evalRows = () => fetch('/v1/eval', { headers: authHeaders() }).then(asJson);
export const documents = () => fetch('/v1/documents', { headers: authHeaders() }).then(asJson);

export async function upload(file) {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/v1/documents', { method: 'POST', headers: authHeaders(), body: fd });
  return asJson(r);
}

export async function removeDocument(docId) {
  const r = await fetch(`/v1/documents/${encodeURIComponent(docId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  return asJson(r);
}

/**
 * 把 SSE 字节流解析成 {event, data}。
 *
 * 两个细节都是踩过的坑：
 * 1. 帧以空行结束，保活用的是 `: ping` 注释——它是**行**不是帧。
 *    把整帧丢弃会连带吞掉紧跟其后的第一个 token，表现为答案里少半句话。
 * 2. 400 错误返回的是 JSON 不是流，必须先判 r.ok（由 asJson 之外单独处理）。
 */
export async function* askStream(body, signal) {
  const r = await fetch('/v1/ask/stream', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    signal,
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let message = `HTTP ${r.status}`;
    let code = '';
    try {
      const j = await r.json();
      const e = j.error || {};
      if (e.message) message = e.message;
      if (e.code) code = e.code;
    } catch (_) { /* ignore */ }
    throw new ApiError(message, { code, status: r.status });
  }

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, i).replace(/^(:\s*[^\n]*\n?)+/, '').trim();
      buf = buf.slice(i + 2);
      if (!raw) continue;
      const ev = (raw.match(/^event: (.+)$/m) || [])[1];
      const dataLine = (raw.match(/^data: (.*)$/m) || [])[1];
      if (!ev || !dataLine) continue;
      let data;
      try { data = JSON.parse(dataLine); } catch (_) { continue; }
      yield { event: ev, data };
    }
  }
}
