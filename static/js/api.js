// Bflow API 客户端 —— 统一处理包络与错误。
// 契约见 spec/03-api-contract.md §0。

export class ApiError extends Error {
  constructor(code, message, hint, retryable, detail) {
    super(message || code || '请求失败');
    this.code = code || 'UNKNOWN';
    this.hint = hint || '';
    this.retryable = !!retryable;
    this.detail = detail || {};
  }
}

async function request(method, path, body, opts = {}) {
  const init = { method, headers: { Accept: 'application/json' } };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (opts.signal) init.signal = opts.signal;

  let resp;
  try {
    resp = await fetch(path, init);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('NETWORK', '无法连接服务。', '请确认 Bflow 服务正在运行。', true);
  }

  const text = await resp.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { /* 非 JSON */ }

  if (!resp.ok || !payload || payload.ok === false) {
    const err = (payload && payload.error) || {};
    throw new ApiError(err.code, err.message || `请求失败（HTTP ${resp.status}）`,
      err.hint, err.retryable, err.detail);
  }
  return payload.data ?? {};
}

export const api = {
  get: (p, o) => request('GET', p, undefined, o),
  post: (p, b, o) => request('POST', p, b, o),
  put: (p, b, o) => request('PUT', p, b, o),
  patch: (p, b, o) => request('PATCH', p, b, o),
  del: (p, o) => request('DELETE', p, undefined, o),

  // --- 渠道 ---
  channels: () => request('GET', '/api/channels'),
  channelCreate: (b) => request('POST', '/api/channels', b),
  channelPatch: (id, b) => request('PATCH', `/api/channels/${id}`, b),
  channelDelete: (id) => request('DELETE', `/api/channels/${id}`),
  capabilityPut: (id, b) => request('PUT', `/api/channels/${id}/capability`, b),
  channelVerify: (id) => request('POST', `/api/channels/${id}/verify`),
  protocols: () => request('GET', '/api/protocols'),

  // --- 生图 ---
  generate: (b, o) => request('POST', '/api/images/generate', b, o),

  // --- 任务 ---
  tasks: (params = '') => request('GET', `/api/tasks${params}`),
  task: (id) => request('GET', `/api/tasks/${id}`),

  // --- 资源 ---
  assets: (params = '') => request('GET', `/api/assets${params}`),
  assetDelete: (id) => request('DELETE', `/api/assets/${id}`),
  upload: (files, purpose = 'reference') => {
    const fd = new FormData();
    fd.append('purpose', purpose);
    for (const f of files) fd.append('files', f);
    return request('POST', '/api/assets/upload', fd);
  },

  // --- 画布 ---
  canvases: () => request('GET', '/api/canvases'),
  canvasCreate: (title) => request('POST', '/api/canvases', { title }),
  canvas: (id) => request('GET', `/api/canvases/${id}`),
  canvasSave: (id, body) => request('PUT', `/api/canvases/${id}`, body),
  canvasDelete: (id) => request('DELETE', `/api/canvases/${id}`),

  // --- 配置 ---
  config: () => request('GET', '/api/config'),
  configPatch: (b) => request('PATCH', '/api/config', b),
  appInfo: () => request('GET', '/api/app-info'),
};
