// Bflow 前端入口 —— 视图路由与全局初始化。

import { api } from './api.js';
import { toastError } from './ui.js';
import { renderWorkbench } from './generate.js';
import { renderCanvas } from './canvas.js';
import { renderLibrary } from './library.js';
import { renderTasks } from './tasks.js';
import { renderChannels } from './channels.js';
import { renderSettings, applyTheme } from './settings.js';

const RENDERERS = {
  workbench: renderWorkbench,
  canvas: renderCanvas,
  library: renderLibrary,
  tasks: renderTasks,
  channels: renderChannels,
  settings: renderSettings,
};

let current = 'workbench';

function show(view) {
  current = view;
  for (const btn of document.querySelectorAll('.nav-item')) {
    btn.classList.toggle('active', btn.dataset.view === view);
  }
  for (const sec of document.querySelectorAll('.view')) {
    sec.classList.toggle('active', sec.id === `view-${view}`);
  }
  const root = document.getElementById(`view-${view}`);
  try {
    RENDERERS[view]?.(root);
  } catch (e) {
    console.error(e);
    toastError(e);
  }
}

function initNav() {
  for (const btn of document.querySelectorAll('.nav-item')) {
    btn.addEventListener('click', () => show(btn.dataset.view));
  }
}

function initTheme() {
  const btn = document.getElementById('themeBtn');
  btn?.addEventListener('click', async () => {
    const cur = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = cur;
    try {
      await api.configPatch({ 'ui.theme': cur });
    } catch { /* 主题偏好持久化失败不阻塞 */ }
  });
}

async function init() {
  initNav();
  initTheme();
  try {
    const info = await api.appInfo();
    document.getElementById('appVersion').textContent = info.version || '';
  } catch { /* 版本号非关键 */ }
  try {
    const cfg = await api.config();
    applyTheme(cfg.settings?.['ui.theme'] || 'auto');
  } catch { applyTheme('auto'); }
  show('workbench');
  // 通知 index.html 里的兜底脚本：前端已就绪，无需显示加载失败提示
  window.__bflowBooted?.();
}

init();
