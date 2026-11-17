// 画布视图 —— 无限画布 + 节点 + 连线。
// 设计见 spec/05-ui-ia.md §5。

import { api } from './api.js';
import {
  clear, confirmDialog, el, formatLabel, modal, toastError, toastInfo, toastOk,
} from './ui.js';

const state = {
  canvasId: null,
  nodes: [],
  edges: [],
  title: '',
  selected: null,
  tool: 'select',
  view: { x: 40, y: 40, scale: 1 },
  drag: null,
  dirty: false,
};

let refs = {};

export async function renderCanvas(root) {
  clear(root);
  const wrap = el('div', { class: 'canvas-wrap' });

  // 工具栏
  const toolbar = el('div', { class: 'canvas-toolbar' });
  const tools = [
    ['select', '➤', '选择'], ['pan', '✋', '平移'],
    ['image', '🖼', '图像节点'], ['text', 'T', '文本节点'],
  ];
  for (const [key, icon, title] of tools) {
    toolbar.append(el('button', {
      class: `tool-btn ${state.tool === key ? 'active' : ''}`,
      text: icon, title,
      onClick: () => { state.tool = key; renderCanvas(root); },
    }));
  }
  toolbar.append(el('div', { style: { flex: '1' } }));
  toolbar.append(el('button', { class: 'tool-btn', text: '＋', title: '新建画布',
    onClick: () => newCanvas(root) }));
  toolbar.append(el('button', { class: 'tool-btn', text: '⇩', title: '导出',
    onClick: () => exportCanvas() }));

  // 画布区
  const stage = el('div', { class: 'canvas-stage' });
  const layer = el('div', { class: 'canvas-layer' });
  stage.append(layer);
  const status = el('div', { class: 'canvas-status', text: '—' });

  // 右侧属性面板
  const side = el('div', { class: 'canvas-side' });

  wrap.append(toolbar, el('div', { class: 'grow', style: { position: 'relative' } }, stage, status), side);
  root.append(wrap);

  refs = { stage, layer, side, status, root };

  bindStage(stage);
  await loadCanvases(root);
}

async function loadCanvases(root) {
  try {
    const data = await api.canvases();
    const list = data.canvases || [];
    if (!state.canvasId && list.length) {
      await openCanvas(list[0].id, root);
      return;
    }
    if (!list.length) {
      const c = await api.canvasCreate('我的第一个画布');
      state.canvasId = c.id;
      drawCanvas();
      renderSide();
      return;
    }
    if (state.canvasId && !list.find((x) => x.id === state.canvasId)) {
      state.canvasId = list[0].id;
      await openCanvas(state.canvasId, root);
    }
  } catch (e) {
    toastError(e);
  }
}

async function openCanvas(id, root) {
  try {
    const c = await api.canvas(id);
    state.canvasId = c.id;
    state.nodes = c.nodes || [];
    state.edges = c.edges || [];
    state.title = c.title || '';
    state.selected = null;
    drawCanvas();
    renderSide();
  } catch (e) {
    toastError(e);
  }
}

async function newCanvas(root) {
  const input = el('input', { type: 'text', value: '未命名画布' });
  modal({
    title: '新建画布',
    body: el('label', { class: 'field' }, '标题', input),
    actions: [
      { label: '取消' },
      {
        label: '创建', kind: 'primary',
        onClick: async () => {
          try {
            const c = await api.canvasCreate(input.value.trim() || '未命名画布');
            state.canvasId = c.id;
            state.nodes = []; state.edges = []; state.title = c.title;
            toastOk('已创建');
            drawCanvas(); renderSide();
          } catch (e) { toastError(e); return false; }
        },
      },
    ],
  });
}

// ---------------------------------------------------------------- 绘制

function drawCanvas() {
  const { layer, status } = refs;
  if (!layer) return;
  clear(layer);
  layer.style.transform = `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;

  for (const node of state.nodes) {
    const box = el('div', {
      class: `cnode ${state.selected === node.id ? 'selected' : ''}`,
      style: {
        left: `${node.x}px`, top: `${node.y}px`,
        width: `${node.width || 320}px`, height: `${node.height || 320}px`,
        zIndex: String(node.z_index ?? 0),
      },
    });
    const head = el('div', { class: 'cnode-head' },
      el('span', { text: node.type === 'text' ? '文本' : '图像' }),
      node.data?.warnings?.length
        ? el('span', { class: 'warn-dot', text: '⚠', title: node.data.warnings.map((w) => w.message).join('\n') })
        : null);
    box.append(head);

    if (node.type === 'text') {
      box.append(el('div', { class: 'cnode-text', text: node.data?.text || '' }));
    } else if (node.asset_id) {
      box.append(el('div', { class: 'cnode-body' },
        el('img', { src: `/api/assets/${node.asset_id}/raw`, alt: '', loading: 'lazy' })));
    } else {
      box.append(el('div', { class: 'cnode-body' },
        el('div', { class: 'dim small', text: '（空节点）' })));
    }

    box.addEventListener('mousedown', (e) => startNodeDrag(e, node));
    layer.append(box);
  }

  // 连线（SVG 层）
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  svg.style.position = 'absolute';
  svg.style.inset = '0';
  svg.style.overflow = 'visible';
  svg.style.pointerEvents = 'none';
  for (const edge of state.edges) {
    const a = state.nodes.find((n) => n.id === edge.source_id);
    const b = state.nodes.find((n) => n.id === edge.target_id);
    if (!a || !b) continue;
    const ax = a.x + (a.width || 320) / 2;
    const ay = a.y + (a.height || 320) / 2;
    const bx = b.x + (b.width || 320) / 2;
    const by = b.y + (b.height || 320) / 2;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', ax); line.setAttribute('y1', ay);
    line.setAttribute('x2', bx); line.setAttribute('y2', by);
    line.setAttribute('stroke', 'var(--border-strong)');
    line.setAttribute('stroke-width', '2');
    svg.append(line);
  }
  layer.append(svg);

  status.textContent = `${state.nodes.length} 节点 · ${state.edges.length} 连线 · 缩放 ${Math.round(state.view.scale * 100)}%`;
}

// ---------------------------------------------------------------- 交互

function bindStage(stage) {
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    state.view.scale = Math.min(3, Math.max(0.2, state.view.scale * factor));
    drawCanvas();
  }, { passive: false });

  stage.addEventListener('mousedown', (e) => {
    if (e.target !== stage && !e.target.classList.contains('canvas-layer')) return;
    if (state.tool === 'pan' || e.button === 1) {
      state.drag = { mode: 'pan', sx: e.clientX, sy: e.clientY, vx: state.view.x, vy: state.view.y };
      stage.classList.add('panning');
      return;
    }
    // 在空白处添加节点
    if (state.tool === 'image' || state.tool === 'text') {
      const rect = stage.getBoundingClientRect();
      const x = (e.clientX - rect.left - state.view.x) / state.view.scale;
      const y = (e.clientY - rect.top - state.view.y) / state.view.scale;
      addNode(state.tool === 'text' ? 'text' : 'image', x, y);
      state.tool = 'select';
      return;
    }
    state.selected = null;
    drawCanvas();
    renderSide();
  });

  window.addEventListener('mousemove', onMouseMove);
  window.addEventListener('mouseup', onMouseUp);
}

function startNodeDrag(e, node) {
  e.stopPropagation();
  if (state.tool !== 'select') return;
  state.selected = node.id;
  state.drag = {
    mode: 'node', node,
    sx: e.clientX, sy: e.clientY,
    ox: node.x, oy: node.y,
  };
  drawCanvas();
  renderSide();
}

function onMouseMove(e) {
  if (!state.drag) return;
  const d = state.drag;
  if (d.mode === 'pan') {
    state.view.x = d.vx + (e.clientX - d.sx);
    state.view.y = d.vy + (e.clientY - d.sy);
  } else if (d.mode === 'node') {
    d.node.x = d.ox + (e.clientX - d.sx) / state.view.scale;
    d.node.y = d.oy + (e.clientY - d.sy) / state.view.scale;
  }
  drawCanvas();
}

function onMouseUp() {
  if (state.drag?.mode === 'node') {
    state.dirty = true;
    saveSoon();
  }
  state.drag = null;
  document.querySelector('.canvas-stage')?.classList.remove('panning');
}

let saveTimer = null;
function saveSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveCanvas, 800);
}

async function saveCanvas() {
  if (!state.canvasId || !state.dirty) return;
  state.dirty = false;
  try {
    await api.canvasSave(state.canvasId, {
      title: state.title, nodes: state.nodes, edges: state.edges, meta: {},
    });
  } catch (e) {
    toastError(e);
  }
}

function addNode(type, x, y) {
  const node = {
    id: `nd_${Math.random().toString(36).slice(2, 10)}`,
    type,
    x: Math.round(x - 160), y: Math.round(y - 160),
    width: 320, height: 320, z_index: state.nodes.length,
    data: type === 'text' ? { text: '双击编辑' } : {},
    asset_id: null,
  };
  state.nodes.push(node);
  state.selected = node.id;
  state.dirty = true;
  drawCanvas();
  renderSide();
  saveSoon();
}

// ---------------------------------------------------------------- 属性面板

function renderSide() {
  const { side } = refs;
  if (!side) return;
  clear(side);

  // 画布信息 + 画布切换
  const sel = el('select');
  api.canvases().then((d) => {
    for (const c of d.canvases || []) {
      sel.append(el('option', { value: c.id, text: c.title }));
    }
    sel.value = state.canvasId;
    sel.addEventListener('change', () => openCanvas(sel.value, refs.root));
  }).catch(() => {});
  side.append(el('label', { class: 'field' }, '画布', sel));

  const node = state.nodes.find((n) => n.id === state.selected);
  if (!node) {
    side.append(el('div', { class: 'hint', text: '选中一个节点以编辑其属性。' }));
    side.append(el('div', { class: 'hint', text: '滚轮缩放 · 空白处拖拽平移 · 用工具栏添加节点' }));
    return;
  }

  side.append(el('div', { class: 'panel-title', text: node.type === 'text' ? '文本节点' : '图像节点' }));

  if (node.type === 'text') {
    const ta = el('textarea', { value: node.data?.text || '' });
    ta.addEventListener('input', () => {
      node.data = { ...(node.data || {}), text: ta.value };
      state.dirty = true; drawCanvas(); saveSoon();
    });
    side.append(el('label', { class: 'field' }, '文本', ta));
  } else {
    const prompt = el('textarea', { value: node.data?.prompt || '', placeholder: '提示词' });
    prompt.addEventListener('input', () => {
      node.data = { ...(node.data || {}), prompt: prompt.value };
      state.dirty = true; saveSoon();
    });
    side.append(el('label', { class: 'field' }, '提示词', prompt));

    const url = el('input', { type: 'text', value: node.data?.asset_url || '', placeholder: '/api/assets/xxx 或粘贴图片地址' });
    side.append(el('label', { class: 'field' }, '资源', url));

    side.append(el('button', {
      class: 'btn primary', text: '绑定资源',
      onClick: async () => {
        const v = url.value.trim();
        if (!v) { toastInfo('请填写资源地址或上传图片。'); return; }
        try {
          // 支持 /api/assets/<id> 形式
          const m = v.match(/\/api\/assets\/([A-Za-z0-9_-]+)/);
          if (m) {
            node.asset_id = m[1];
            node.data = { ...(node.data || {}), asset_url: v };
          } else {
            toastInfo('暂仅支持引用已上传的资源 ID。');
            return;
          }
          state.dirty = true; drawCanvas(); saveSoon();
          toastOk('已绑定');
        } catch (e) { toastError(e); }
      },
    }));

    if (node.data?.warnings?.length) {
      side.append(el('div', { class: 'panel', style: { borderColor: 'var(--warn)' } },
        el('div', { class: 'badge warn', text: '偏差提示' }),
        ...node.data.warnings.map((w) => el('div', { class: 'small', text: w.message }))));
    }
  }

  const del = el('button', {
    class: 'btn danger', text: '删除节点',
    onClick: () => {
      state.nodes = state.nodes.filter((n) => n.id !== node.id);
      state.edges = state.edges.filter((e) => e.source_id !== node.id && e.target_id !== node.id);
      state.selected = null;
      state.dirty = true;
      drawCanvas(); renderSide(); saveSoon();
    },
  });
  side.append(del);
}

async function exportCanvas() {
  if (!state.canvasId) { toastInfo('请先打开一个画布。'); return; }
  try {
    const c = await api.canvas(state.canvasId);
    const blob = new Blob([JSON.stringify(c, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${c.title || 'canvas'}.bflow.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toastOk('已导出');
  } catch (e) { toastError(e); }
}
