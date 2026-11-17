// UI 工具：浮层反馈、DOM 助手、格式化。
// 反馈四层级见 spec/05-ui-ia.md §8.1。

export const FORMAT_LABELS = {
  png: 'PNG', jpeg: 'JPEG', webp: 'WebP', svg: 'SVG', gif: 'GIF', bmp: 'BMP', tif: 'TIFF',
};

export const formatLabel = (f) => FORMAT_LABELS[String(f || '').toLowerCase()]
  || String(f || '').toUpperCase();

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

// ---------------------------------------------------------------- 浮层反馈

const toastLayer = () => document.getElementById('toastLayer');
let toastTimer = null;

export function toast(message, kind = 'info', ms = 7000) {
  const layer = toastLayer();
  if (!layer) return;
  clear(layer);
  const box = el('div', { class: `toast ${kind}`, text: message });
  layer.append(box);
  requestAnimationFrame(() => box.classList.add('show'));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove('show'), ms);
  return box;
}

/** 阻断级：生成失败。用红色。 */
export const toastError = (e) => {
  const msg = e instanceof Error ? e.message : String(e);
  const hint = (e && e.hint) ? `\n${e.hint}` : '';
  return toast(`${msg}${hint}`, 'error', 14000);
};

/** 警告级：格式/尺寸偏差。产物有效，用琥珀色。 */
export const toastWarn = (message) => toast(message, 'warn', 12000);

/** 提示级：可安全降级参数。用灰色。 */
export const toastInfo = (message) => toast(message, 'info', 7000);

export function toastOk(message) { return toast(message, 'ok', 4000); }

// ---------------------------------------------------------------- 模态框

export function modal({ title, body, actions = [], width }) {
  const layer = document.getElementById('modalLayer');
  if (!layer) return { close() {} };
  clear(layer);
  const backdrop = el('div', { class: 'modal-backdrop' });
  const box = el('div', { class: 'modal' });
  if (width) box.style.width = width;
  box.append(el('h3', { text: title || '' }));
  const bodyBox = el('div', { class: 'col' });
  if (body) bodyBox.append(body);
  box.append(bodyBox);
  if (actions.length) {
    const bar = el('div', { class: 'modal-actions' });
    for (const a of actions) {
      bar.append(el('button', {
        class: `btn ${a.kind || ''}`,
        text: a.label,
        onClick: () => { const r = a.onClick?.(); if (r !== false) close(); },
      }));
    }
    box.append(bar);
  }
  backdrop.append(box);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
  layer.append(backdrop);
  function close() { clear(layer); }
  return { close, box };
}

export function confirmDialog(title, message, onOk, okLabel = '确定') {
  return modal({
    title,
    body: el('div', { class: 'small dim', text: message }),
    actions: [
      { label: '取消' },
      { label: okLabel, kind: 'primary', onClick: onOk },
    ],
  });
}

// ---------------------------------------------------------------- 格式化

export function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtAgo(ts) {
  if (!ts) return '从未';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return fmtTime(ts);
}

export function debounce(fn, ms = 300) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}
