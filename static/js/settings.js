// 设置视图 —— 存储、主题、更新。
// 设计见 spec/05-ui-ia.md §6。

import { api } from './api.js';
import { clear, el, toastError, toastOk } from './ui.js';

export async function renderSettings(root) {
  clear(root);
  const wrap = el('div', { class: 'panel', style: { margin: '16px', maxWidth: '720px' } });
  wrap.append(el('div', { class: 'panel-title', text: '设置' }));
  const body = el('div', { class: 'col' });
  wrap.append(body);
  root.append(wrap);

  let info = null, cfg = null;
  try {
    [info, cfg] = await Promise.all([api.appInfo(), api.config()]);
  } catch (e) {
    toastError(e);
    return;
  }

  // 外观
  const theme = el('select', {},
    el('option', { value: 'auto', text: '跟随系统' }),
    el('option', { value: 'light', text: '亮色' }),
    el('option', { value: 'dark', text: '暗色' }));
  theme.value = cfg.settings?.['ui.theme'] || 'auto';
  theme.addEventListener('change', async () => {
    await api.configPatch({ 'ui.theme': theme.value });
    applyTheme(theme.value);
  });
  body.append(el('div', { class: 'panel-title', text: '外观' }));
  body.append(el('label', { class: 'field' }, '主题', theme));

  // 存储
  body.append(el('div', { class: 'panel-title', text: '存储' }));
  const dataRoot = el('input', { type: 'text', value: info.data_root || '' });
  body.append(el('label', { class: 'field' }, '数据目录', dataRoot));
  body.append(el('div', { class: 'row' },
    el('button', {
      class: 'btn', text: '切换数据目录',
      onClick: async () => {
        try {
          await api.patch('/api/storage/settings', { data_root: dataRoot.value.trim() });
          toastOk('已切换，重启后生效');
        } catch (e) { toastError(e); }
      },
    })));

  // 更新
  body.append(el('div', { class: 'panel-title', text: '更新' }));
  body.append(el('div', { class: 'hint', text: '当前版本 ' + (info.version || '—') }));
  const repo = el('input', { type: 'text', value: cfg.settings?.['update.repo'] || '', placeholder: 'https://github.com/<you>/<repo>' });
  body.append(el('label', { class: 'field' }, '更新源仓库', repo));
  body.append(el('button', {
    class: 'btn', text: '保存更新源',
    onClick: async () => {
      try {
        await api.configPatch({ 'update.repo': repo.value.trim(), 'update.channel': repo.value.trim() ? 'github' : 'none' });
        toastOk('已保存');
      } catch (e) { toastError(e); }
    },
  }));

  // 关于
  body.append(el('div', { class: 'panel-title', text: '关于' }));
  body.append(el('div', { class: 'hint' },
    'Bflow —— 面向 AI 多模态创作的工作台。'));
  body.append(el('div', { class: 'hint' },
    '本项目为 clean-room 重写：功能需求来源于公开 API 文档与对渠道服务端的黑盒行为探测，'
    + '未复制任何第三方项目的源代码。'));
}

export function applyTheme(pref) {
  const dark = pref === 'dark'
    || (pref !== 'light' && window.matchMedia?.('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}
