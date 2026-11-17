// 图片预览（lightbox）—— 任务与素材库共用。
//
// 参照 Infinite-Canvas 的交互：点缩略图放大，显示尺寸/格式/提示词，
// 可直接下载，多图可左右切换，ESC 或点背景关闭。
// 独立挂在 #lightboxLayer，不与 ui.js 的 modalLayer 冲突。

import { el, clear, formatLabel, fmtBytes } from './ui.js';

let current = [];
let index = 0;
let onKey = null;

function layer() {
  let node = document.getElementById('lightboxLayer');
  if (!node) {
    node = el('div', { id: 'lightboxLayer' });
    document.body.append(node);
  }
  return node;
}

/**
 * 打开预览。
 * items: [{ url, format, width, height, bytes, prompt, name }]
 */
export function openLightbox(items, start = 0) {
  const list = (items || []).filter((x) => x && x.url);
  if (!list.length) return;
  current = list;
  index = Math.max(0, Math.min(start, list.length - 1));

  if (onKey) document.removeEventListener('keydown', onKey);
  onKey = (e) => {
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'ArrowLeft') step(-1);
  };
  document.addEventListener('keydown', onKey);
  draw();
}

export function closeLightbox() {
  clear(layer());
  if (onKey) {
    document.removeEventListener('keydown', onKey);
    onKey = null;
  }
  current = [];
}

function step(delta) {
  if (current.length < 2) return;
  index = (index + delta + current.length) % current.length;
  draw();
}

function draw() {
  const item = current[index];
  const box = el('div', { class: 'lightbox' });

  // 点背景关闭（点图片/按钮不关）
  box.addEventListener('click', (e) => { if (e.target === box) closeLightbox(); });

  const stage = el('div', { class: 'lightbox-stage' });
  const img = el('img', { src: item.url, alt: item.prompt || '预览' });
  stage.append(img);

  if (current.length > 1) {
    stage.append(
      el('button', {
        class: 'lightbox-nav prev', text: '‹',
        title: '上一张（←）', onClick: (e) => { e.stopPropagation(); step(-1); },
      }),
      el('button', {
        class: 'lightbox-nav next', text: '›',
        title: '下一张（→）', onClick: (e) => { e.stopPropagation(); step(1); },
      }));
  }

  stage.append(el('button', {
    class: 'lightbox-close', text: '×', title: '关闭（ESC）',
    onClick: (e) => { e.stopPropagation(); closeLightbox(); },
  }));

  const meta = el('div', { class: 'lightbox-meta' });
  const bits = [];
  if (item.format) bits.push(formatLabel(item.format));
  if (item.width && item.height) bits.push(`${item.width}×${item.height}`);
  if (item.bytes) bits.push(fmtBytes(item.bytes));
  if (current.length > 1) bits.push(`${index + 1} / ${current.length}`);
  if (bits.length) meta.append(el('span', { class: 'badge mute', text: bits.join(' · ') }));

  meta.append(el('span', { class: 'spacer' }));
  meta.append(el('a', {
    class: 'btn sm primary',
    href: item.url,
    download: item.name || `${item.id || 'image'}.${item.format || 'png'}`,
    text: '下载',
  }));

  box.append(stage);
  if (item.prompt) {
    box.append(el('div', { class: 'lightbox-prompt', text: item.prompt }));
  }
  box.append(meta);

  const host = layer();
  clear(host);
  host.append(box);
}

/** 生成可点击的缩略图元素（任务/素材库共用）。 */
export function thumbnail(item, { items, style } = {}) {
  const thumb = el('div', { class: 'lib-thumb', style });
  thumb.append(el('img', { src: item.url, alt: item.prompt || '', loading: 'lazy' }));
  thumb.addEventListener('click', () => {
    const list = items || [item];
    const at = Math.max(0, list.indexOf(item));
    openLightbox(list, at);
  });
  return thumb;
}
