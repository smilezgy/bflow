// 任务中心 —— 把「偏差」列为一等公民。
// 设计见 spec/05-ui-ia.md §6。

import { api } from './api.js';
import {
  clear, el, fmtAgo, formatLabel, toastError, toastOk,
} from './ui.js';

let filter = '';
let timer = null;

const STATUS = {
  pending: { label: '排队中', badge: 'info' },
  running: { label: '进行中', badge: 'info' },
  succeeded: { label: '成功', badge: 'ok' },
  failed: { label: '失败', badge: 'err' },
  canceled: { label: '已取消', badge: 'mute' },
};

export async function renderTasks(root) {
  clear(root);
  const wrap = el('div', { class: 'panel', style: { margin: '16px' } });
  const title = el('div', { class: 'panel-title' }, '任务中心');

  const tabs = el('div', { class: 'row' });
  for (const [key, label] of [['', '全部'], ['running', '进行中'], ['succeeded', '成功'], ['failed', '失败']]) {
    const b = el('button', {
      class: `btn sm ${filter === key ? 'primary' : ''}`, text: label,
      onClick: () => { filter = key; renderTasks(root); },
    });
    tabs.append(b);
  }
  title.append(el('span', { class: 'spacer' }), tabs);
  wrap.append(title);

  const body = el('div');
  wrap.append(body);
  root.append(wrap);

  await loadTasks(body);

  // 有进行中的任务时轮询
  clearInterval(timer);
  timer = setInterval(async () => {
    if (!document.getElementById('view-tasks').classList.contains('active')) return;
    await loadTasks(body, true);
  }, 5000);
}

async function loadTasks(body, quiet = false) {
  try {
    const data = await api.tasks(filter ? `?status=${filter}&limit=100` : '?limit=100');
    const tasks = data.tasks || [];
    if (quiet && !tasks.some((t) => t.status === 'running' || t.status === 'pending')) {
      clearInterval(timer);
      return;
    }
    clear(body);
    if (!tasks.length) {
      body.append(el('div', { class: 'empty', text: '还没有任务记录。' }));
      return;
    }
    body.append(buildTable(tasks));
  } catch (e) {
    if (!quiet) toastError(e);
  }
}

function buildTable(tasks) {
  const table = el('table', { class: 'grid' });
  table.append(el('thead', {}, el('tr', {},
    el('th', { text: '状态' }), el('th', { text: '提示词' }),
    el('th', { text: '渠道' }), el('th', { text: '格式（请求→实际）' }),
    el('th', { text: '说明' }), el('th', { text: '时间' }), el('th', { text: '' }),
  )));
  const tbody = el('tbody');
  for (const t of tasks) {
    const st = STATUS[t.status] || { label: t.status, badge: 'mute' };
    const fmtCell = el('span', {});
    if (t.requested_format) {
      fmtCell.append(el('span', { text: formatLabel(t.requested_format) }));
      if (t.actual_format && t.actual_format !== t.requested_format) {
        fmtCell.append(el('span', { class: 'badge warn', text: `→ ${formatLabel(t.actual_format)}` }));
      } else if (t.actual_format) {
        fmtCell.append(el('span', { class: 'badge ok', text: '→ 一致' }));
      }
    } else {
      fmtCell.append(el('span', { class: 'dim', text: '—' }));
    }

    const note = el('span', {});
    if (t.error) {
      note.append(el('span', { class: 'badge err', text: t.error.code || '失败' }));
      note.append(el('div', { class: 'small dim', text: t.error.message || '' }));
    } else if (t.warnings?.length) {
      note.append(el('span', { class: 'badge warn', text: '偏差' }));
      note.append(el('div', { class: 'small dim', text: t.warnings[0].message }));
    } else if (t.status === 'succeeded') {
      note.append(el('span', { class: 'dim small', text: t.actual_size || '' }));
    }

    tbody.append(el('tr', {},
      el('td', {}, el('span', { class: `badge ${st.badge}`, text: st.label })),
      el('td', {}, el('div', { class: 'small', text: trunc(t.prompt, 40) }),
        t.billed ? el('span', { class: 'hint', text: '已计费' }) : null),
      el('td', { class: 'small mono', text: t.channel_id }),
      el('td', {}, fmtCell),
      el('td', {}, note),
      el('td', { class: 'small dim', text: fmtAgo(t.created_at) }),
      el('td', {}, el('button', {
        class: 'btn sm', text: '详情',
        onClick: () => showDetail(t),
      })),
    ));
  }
  table.append(tbody);
  return table;
}

function trunc(s, n) {
  if (!s) return '';
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

async function showDetail(t) {
  const { modal } = await import('./ui.js');
  const body = el('div', { class: 'col' });
  const add = (k, v) => body.append(el('div', { class: 'row' },
    el('span', { class: 'hint', style: { minWidth: '84px' }, text: k }),
    el('span', { class: 'small', text: v ?? '—' })));
  add('任务号', t.task_id);
  add('状态', t.status);
  add('提示词', t.prompt);
  add('渠道 / 模型', `${t.channel_id} / ${t.model}`);
  add('请求格式', formatLabel(t.requested_format));
  add('实际格式', formatLabel(t.actual_format));
  add('请求尺寸', t.requested_size);
  add('实际尺寸', t.actual_size);
  add('是否计费', t.billed ? '是' : '否');
  add('创建时间', fmtAgo(t.created_at));
  if (t.error) {
    add('错误', `${t.error.code} — ${t.error.message}`);
  }
  for (const w of t.warnings || []) {
    body.append(el('div', { class: 'panel', style: { borderColor: 'var(--warn)' } },
      el('div', { class: 'badge warn', text: w.code }),
      el('div', { class: 'small', text: w.message }),
      w.hint ? el('div', { class: 'hint', text: w.hint }) : null));
  }
  modal({ title: '任务详情', body, width: '560px', actions: [{ label: '关闭' }] });
}
