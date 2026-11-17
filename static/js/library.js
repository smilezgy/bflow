// 素材库 —— 网格浏览、搜索、标签、批量操作。
// 设计见 spec/05-ui-ia.md §7。

import { api } from './api.js';
import {
  clear, el, fmtBytes, fmtTime, formatLabel, toastError, toastOk,
} from './ui.js';

let query = '';
let tag = '';
let view = 'grid';

export async function renderLibrary(root) {
  clear(root);
  const wrap = el('div', { class: 'panel', style: { margin: '16px' } });

  const title = el('div', { class: 'panel-title' }, '素材库');
  const search = el('input', { type: 'text', placeholder: '搜索资源…', value: query });
  search.addEventListener('input', () => {
    query = search.value.trim();
    refresh();
  });
  const tagSel = el('select', {}, el('option', { value: '', text: '全部标签' }));
  const gridBtn = el('button', { class: `btn sm ${view === 'grid' ? 'primary' : ''}`, text: '网格' });
  const listBtn = el('button', { class: `btn sm ${view === 'list' ? 'primary' : ''}`, text: '列表' });
  gridBtn.addEventListener('click', () => { view = 'grid'; renderLibrary(root); });
  listBtn.addEventListener('click', () => { view = 'list'; renderLibrary(root); });

  const upload = el('input', { type: 'file', accept: 'image/*', multiple: true, style: { display: 'none' } });
  upload.addEventListener('change', async () => {
    try {
      await api.upload(Array.from(upload.files), 'library');
      toastOk('上传完成');
      refresh();
    } catch (e) { toastError(e); }
  });

  title.append(
    el('span', { class: 'spacer' }),
    search, tagSel,
    gridBtn, listBtn,
    el('button', { class: 'btn sm primary', text: '上传', onClick: () => upload.click() }),
    upload);

  wrap.append(title);
  const body = el('div');
  wrap.append(body);
  root.append(wrap);

  // 标签列表
  try {
    const td = await api.assets('/tags');
    for (const t of td.tags || []) {
      tagSel.append(el('option', { value: t.tag, text: `${t.tag} (${t.count})` }));
    }
    tagSel.value = tag;
    tagSel.addEventListener('change', () => { tag = tagSel.value; refresh(); });
  } catch { /* 标签非关键 */ }

  await refresh();
}

async function refresh() {
  const body = document.querySelector('#view-library .panel > div:last-child');
  if (!body) return;
  clear(body);
  body.append(el('div', { class: 'skeleton', style: { height: '180px' } }));
  try {
    const params = new URLSearchParams({ limit: '120' });
    if (query) params.set('q', query);
    if (tag) params.set('tag', tag);
    const data = await api.assets(`?${params}`);
    const assets = data.assets || [];
    clear(body);
    if (!assets.length) {
      body.append(el('div', { class: 'empty', text: '素材库是空的。生成图片或上传文件后会出现在这里。' }));
      return;
    }
    body.append(view === 'grid' ? buildGrid(assets) : buildList(assets));
  } catch (e) {
    clear(body);
    toastError(e);
  }
}

function buildGrid(assets) {
  const grid = el('div', { class: 'lib-grid' });
  for (const a of assets) grid.append(gridItem(a));
  return grid;
}

function gridItem(a) {
  const item = el('div', { class: 'lib-item' });
  item.append(el('div', { class: 'lib-thumb' },
    el('img', { src: a.url, alt: '', loading: 'lazy' })));
  const info = el('div', { class: 'lib-info' },
    el('div', { class: 'row', style: { gap: '6px' } },
      el('span', { class: 'badge mute', text: formatLabel(a.format) }),
      el('span', { class: 'hint', text: `${a.width}×${a.height}` })),
    el('div', { class: 'hint', text: `${fmtBytes(a.bytes)} · ${fmtTime(a.created_at)}` }),
  );
  item.append(info);
  item.append(el('div', { class: 'lib-actions' },
    el('a', { class: 'btn sm', href: a.url, download: `${a.id}.${a.format}`, text: '下载' }),
    el('button', {
      class: 'btn sm danger', text: '删除',
      onClick: async () => {
        try {
          await api.assetDelete(a.id);
          toastOk('已删除');
          refresh();
        } catch (e) { toastError(e); }
      },
    }),
  ));
  return item;
}

function buildList(assets) {
  const table = el('table', { class: 'grid' });
  table.append(el('thead', {}, el('tr', {},
    el('th', { text: '预览' }), el('th', { text: '格式' }), el('th', { text: '尺寸' }),
    el('th', { text: '大小' }), el('th', { text: '来源' }), el('th', { text: '时间' }),
    el('th', { text: '' }),
  )));
  const tbody = el('tbody');
  for (const a of assets) {
    tbody.append(el('tr', {},
      el('td', {}, el('img', {
        src: a.url, alt: '',
        style: { width: '44px', height: '44px', objectFit: 'cover', borderRadius: '4px', display: 'block' },
      })),
      el('td', {}, el('span', { class: 'badge mute', text: formatLabel(a.format) })),
      el('td', { class: 'small', text: `${a.width}×${a.height}` }),
      el('td', { class: 'small dim', text: fmtBytes(a.bytes) }),
      el('td', { class: 'small dim', text: a.origin }),
      el('td', { class: 'small dim', text: fmtTime(a.created_at) }),
      el('td', {}, el('button', {
        class: 'btn sm danger', text: '删除',
        onClick: async () => {
          try {
            await api.assetDelete(a.id);
            toastOk('已删除');
            refresh();
          } catch (e) { toastError(e); }
        },
      })),
    ));
  }
  table.append(tbody);
  return table;
}
