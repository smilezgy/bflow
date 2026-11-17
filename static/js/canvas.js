// 画布视图 —— 无限画布 + 节点 + 连线。
// 设计见 spec/05-ui-ia.md §5。
//
// P3 增强：
//   1. 连线交互 —— 从节点右侧端口拖到目标节点建边；点击连线可选中并删除；
//   2. 节点内直接生成 —— 在画布上就地出图，结果直接落进该节点；
//   3. 局部编辑 —— 在节点上涂绘遮罩，只改指定区域。

import { api } from './api.js';
import {
  clear, confirmDialog, el, formatLabel, modal, toastError, toastInfo, toastOk, toastWarn,
} from './ui.js';

const state = {
  canvasId: null,
  nodes: [],
  edges: [],
  title: '',
  selected: null,
  selectedEdge: null,
  tool: 'select',
  view: { x: 40, y: 40, scale: 1 },
  drag: null,
  link: null,          // 正在拖拽的连线
  dirty: false,
  channels: [],
  gen: {               // 画布级生成设置
    channel_id: '', model: '', size: '1024x1024', output_format: 'png',
  },
  busy: {},            // nodeId → true（生成中）
};

let refs = {};

export async function renderCanvas(root) {
  clear(root);
  const wrap = el('div', { class: 'canvas-wrap' });

  // 工具栏
  const toolbar = el('div', { class: 'canvas-toolbar' });
  const tools = [
    ['select', '➤', '选择 / 拖线建边'],
    ['pan', '✋', '平移'],
    ['image', '🖼', '图像节点'],
    ['text', 'T', '文本节点'],
    ['mask', '✎', '局部编辑（涂绘遮罩）'],
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

  refs = { stage, layer, side, status, root, nodeEls: new Map() };

  bindStage(stage);
  applyTransform();
  await loadChannels();
  await loadCanvases(root);
}

async function loadChannels() {
  try {
    const data = await api.channels();
    state.channels = data.channels || [];
    if (!state.gen.channel_id && state.channels.length) {
      const ch = state.channels[0];
      state.gen.channel_id = ch.id;
      state.gen.model = (ch.models || [])[0] || '';
    }
  } catch (e) {
    toastError(e);
  }
}

async function loadCanvases(root) {
  try {
    const data = await api.canvases();
    const list = data.canvases || [];
    if (!list.length) {
      const c = await api.canvasCreate('我的第一个画布');
      state.canvasId = c.id;
      state.nodes = []; state.edges = [];
      drawCanvas();
      renderSide();
      return;
    }
    if (!state.canvasId || !list.find((x) => x.id === state.canvasId)) {
      state.canvasId = list[0].id;
      await openCanvas(list[0].id, root);
      return;
    }
    // 当前画布已在列表中 —— 仍然要重绘。
    // 漏掉这一步会导致「切换工具后画布空白」：视图重建了，
    // 但节点层没有重新渲染。
    drawCanvas();
    renderSide();
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
    state.selectedEdge = null;
    // 画布级生成设置
    const meta = c.meta || {};
    if (meta.gen) Object.assign(state.gen, meta.gen);
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

function nodeWidth(n) { return n.width || 320; }
function nodeHeight(n) { return n.height || 320; }

/** 端口坐标：输出在右，输入在左。 */
function outPort(n) {
  return { x: n.x + nodeWidth(n), y: n.y + nodeHeight(n) / 2 };
}
function inPort(n) {
  return { x: n.x, y: n.y + nodeHeight(n) / 2 };
}

function edgePath(a, b) {
  const dx = Math.max(48, Math.abs(b.x - a.x) / 2);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}

/**
 * 画布分两层渲染：**节点层**（DOM）与**连线层**（SVG）。
 *
 * 分层是必要的：拖拽节点或拖线时只需更新对应层，
 * 如果整体重建 DOM，正在操作的事件目标会被销毁 —— 拖拽会中断。
 */
function drawCanvas() {
  drawNodes();
  drawEdges();
  updateStatus();
}

function updateStatus() {
  const { status } = refs;
  if (!status) return;
  status.textContent = `${state.nodes.length} 节点 · ${state.edges.length} 连线 · 缩放 ${Math.round(state.view.scale * 100)}%`;
}

function drawNodes() {
  const { layer } = refs;
  if (!layer) return;
  clear(layer);
  refs.nodeEls = new Map();

  for (const node of state.nodes) {
    const box = el('div', {
      class: `cnode ${state.selected === node.id ? 'selected' : ''}`,
      style: {
        left: `${node.x}px`, top: `${node.y}px`,
        width: `${nodeWidth(node)}px`, height: `${nodeHeight(node)}px`,
        zIndex: String(node.z_index ?? 0),
      },
    });
    const head = el('div', { class: 'cnode-head' },
      el('span', { text: node.type === 'text' ? '文本' : '图像' }),
      node.data?.warnings?.length
        ? el('span', { class: 'warn-dot', text: '⚠', title: node.data.warnings.map((w) => w.message).join('\n') })
        : null);
    head.append(el('span', { style: { flex: '1' } }));
    if (node.type !== 'text') {
      head.append(el('button', {
        class: `cnode-act${state.busy[node.id] ? ' busy' : ''}`,
        text: state.busy[node.id] ? '◌' : '⚡',
        title: state.busy[node.id] ? '生成中…' : '就地生成',
        onClick: (e) => { e.stopPropagation(); generateIntoNode(node); },
      }));
    }
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

    // 端口
    const portIn = el('div', { class: 'cnode-port in', title: '输入：拖到上游节点' });
    const portOut = el('div', { class: 'cnode-port out', title: '输出：拖到下游节点' });
    portOut.addEventListener('mousedown', (e) => startLink(e, node));
    portIn.addEventListener('mousedown', (e) => startLink(e, node, true));
    box.append(portIn, portOut);

    box.addEventListener('mousedown', (e) => startNodeDrag(e, node));
    refs.nodeEls.set(node.id, box);
    layer.append(box);
  }
}

function drawEdges() {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'canvas-edges');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  refs.edgeLayer = svg;

  for (const edge of state.edges) {
    const a = state.nodes.find((n) => n.id === edge.source_id);
    const b = state.nodes.find((n) => n.id === edge.target_id);
    if (!a || !b) continue;
    const d = edgePath(outPort(a), inPort(b));
    const isSel = state.selectedEdge === edge.id;

    const line = document.createElementNS(NS, 'path');
    line.setAttribute('d', d);
    line.setAttribute('class', `edge-line${isSel ? ' selected' : ''}`);
    svg.append(line);

    // 命中区（宽透明描边）—— 放在可见线之上才能点到
    const hit = document.createElementNS(NS, 'path');
    hit.setAttribute('d', d);
    hit.setAttribute('class', 'edge-hit');
    hit.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      state.selectedEdge = edge.id;
      state.selected = null;
      drawCanvas();
      renderSide();
    });
    svg.append(hit);
  }

  // 正在拖拽的连线
  const linkPath = document.createElementNS(NS, 'path');
  linkPath.setAttribute('class', 'edge-link');
  svg.append(linkPath);
  refs.linkPath = linkPath;
  updateLinkPath();

  // 挂回图层（在节点层之下）
  // 旧的连线层必须先摘掉 —— 否则旧图里的连线会残留在 DOM 里，
  // 表现为「删了线却还在」。
  const { layer } = refs;
  if (layer) {
    const old = refs.edgeLayer;
    if (old && old.parentNode === layer) layer.removeChild(old);
    layer.insertBefore(svg, layer.firstChild);
  }
}

/** 只更新拖拽中的连线（不动节点层）。 */
function updateLinkPath() {
  const path = refs.linkPath;
  if (!path) return;
  if (!state.link) { path.setAttribute('d', ''); return; }
  const a = state.nodes.find((n) => n.id === state.link.from);
  if (!a) { path.setAttribute('d', ''); return; }
  const from = state.link.fromInput ? inPort(a) : outPort(a);
  path.setAttribute('d', edgePath(from, state.link.to));
}

// ---------------------------------------------------------------- 交互

function bindStage(stage) {
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    state.view.scale = Math.min(3, Math.max(0.2, state.view.scale * factor));
    applyTransform();
    drawEdges();
    updateStatus();
  }, { passive: false });

  stage.addEventListener('mousedown', (e) => {
    if (e.target !== stage && !e.target.classList.contains('canvas-layer')
        && !e.target.classList.contains('canvas-edges')) return;
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
    state.selectedEdge = null;
    drawCanvas();
    renderSide();
  });

  // window 级监听只绑一次 —— renderCanvas 可能被反复调用
  if (!state.bound) {
    state.bound = true;
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
  }
}

/** 视图变换（缩放 / 平移）。 */
function applyTransform() {
  const { layer } = refs;
  if (!layer) return;
  layer.style.transform = `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;
}

function startNodeDrag(e, node) {
  e.stopPropagation();
  if (state.tool === 'mask') {
    openMaskEditor(node);
    return;
  }
  if (state.tool !== 'select') return;
  state.selected = node.id;
  state.selectedEdge = null;
  state.drag = {
    mode: 'node', node,
    sx: e.clientX, sy: e.clientY,
    ox: node.x, oy: node.y,
  };
  // 只更新选中样式，不重建 DOM（重建会销毁正在操作的事件目标）
  for (const [id, box] of (refs.nodeEls || new Map())) {
    box.classList.toggle('selected', id === node.id);
  }
  renderSide();
}

/** 从端口拖出连线。 */
function startLink(e, node, fromInput = false) {
  e.stopPropagation();
  e.preventDefault();
  const p = fromInput ? inPort(node) : outPort(node);
  state.link = {
    from: node.id,
    fromInput,
    to: p,
    valid: false,
    target: null,
  };
  state.tool = 'select';
  updateLinkPath();
}

function onMouseMove(e) {
  if (state.link) {
    const rect = refs.stage?.getBoundingClientRect();
    if (rect) {
      state.link.to = {
        x: (e.clientX - rect.left - state.view.x) / state.view.scale,
        y: (e.clientY - rect.top - state.view.y) / state.view.scale,
      };
    }
    // 悬停在某个节点上 → 记录可连接目标
    const hover = nodeAt(e.clientX, e.clientY);
    state.link.valid = !!hover && hover.id !== state.link.from;
    state.link.target = state.link.valid ? hover.id : null;
    updateLinkPath();
    return;
  }
  if (!state.drag) return;
  const d = state.drag;
  if (d.mode === 'pan') {
    state.view.x = d.vx + (e.clientX - d.sx);
    state.view.y = d.vy + (e.clientY - d.sy);
    applyTransform();
  } else if (d.mode === 'node') {
    d.node.x = d.ox + (e.clientX - d.sx) / state.view.scale;
    d.node.y = d.oy + (e.clientY - d.sy) / state.view.scale;
    // 增量更新位置与连线，不重建节点层
    const box = refs.nodeEls?.get(d.node.id);
    if (box) {
      box.style.left = `${d.node.x}px`;
      box.style.top = `${d.node.y}px`;
    }
    drawEdges();
  }
}

function onMouseUp() {
  if (state.link) {
    const link = state.link;
    state.link = null;
    if (link.valid && link.target) {
      const from = link.fromInput ? link.target : link.from;
      const to = link.fromInput ? link.from : link.target;
      const exists = state.edges.find(
        (e) => e.source_id === from && e.target_id === to);
      if (exists) {
        toastInfo('这两个节点之间已经有连线了。');
      } else {
        state.edges.push({
          id: `eg_${Math.random().toString(36).slice(2, 10)}`,
          source_id: from, target_id: to, label: '',
        });
        state.dirty = true;
        saveSoon();
      }
    }
    updateLinkPath();
    drawEdges();
    renderSide();
    return;
  }
  if (state.drag?.mode === 'node') {
    state.dirty = true;
    saveSoon();
  }
  state.drag = null;
  document.querySelector('.canvas-stage')?.classList.remove('panning');
}

/** 屏幕坐标 → 命中的节点。 */
function nodeAt(clientX, clientY) {
  const rect = refs.stage?.getBoundingClientRect();
  if (!rect) return null;
  const x = (clientX - rect.left - state.view.x) / state.view.scale;
  const y = (clientY - rect.top - state.view.y) / state.view.scale;
  // 端口在节点边界外侧 7px，因此命中范围要留出容差，
  // 否则「拖到输入端口上松手」会落在节点之外而被判为无效。
  const PAD = 12;
  for (let i = state.nodes.length - 1; i >= 0; i--) {
    const n = state.nodes[i];
    if (x >= n.x - PAD && x <= n.x + nodeWidth(n) + PAD
        && y >= n.y - PAD && y <= n.y + nodeHeight(n) + PAD) {
      return n;
    }
  }
  return null;
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
      title: state.title, nodes: state.nodes, edges: state.edges,
      meta: { gen: state.gen },
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

// ---------------------------------------------------------------- 节点内生成

async function generateIntoNode(node) {
  if (state.busy[node.id]) return;
  const prompt = (node.data?.prompt || '').trim();
  if (!prompt) {
    toastInfo('这个节点还没有提示词 —— 先在右侧属性面板填写。');
    state.selected = node.id;
    drawCanvas(); renderSide();
    return;
  }
  if (!state.gen.channel_id || !state.gen.model) {
    toastInfo('请先在右侧面板选择渠道与模型。');
    return;
  }
  state.busy[node.id] = true;
  drawCanvas();
  try {
    const data = await api.generate({
      prompt,
      channel_id: state.gen.channel_id,
      model: state.gen.model,
      size: state.gen.size,
      count: 1,
      output_format: state.gen.output_format,
    });
    const img = (data.images || [])[0];
    if (!img) throw new Error('没有产物');
    node.asset_id = img.id;
    node.data = {
      ...(node.data || {}),
      warnings: data.warnings || [],
      actual_format: img.format,
      actual_size: `${img.width}x${img.height}`,
      task_id: data.task_id,
    };
    state.dirty = true;
    saveSoon();
    drawCanvas();
    renderSide();
    for (const w of (data.warnings || [])) {
      toastWarn(`${w.message}\n${w.hint || ''}`.trim());
    }
    if (!(data.warnings || []).length) toastOk('已生成');
  } catch (e) {
    toastError(e);
  } finally {
    state.busy[node.id] = false;
    drawCanvas();
  }
}

// ---------------------------------------------------------------- 局部编辑（遮罩）

/**
 * 打开遮罩编辑器。
 *
 * 遮罩约定（与 OpenAI 一致）：**被涂区域为透明**（alpha=0），
 * 上游只改动这些区域。因此画布先铺满不透明，再用 destination-out 擦出笔迹。
 */
function openMaskEditor(node) {
  if (!node.asset_id) {
    toastInfo('这个节点还没有图片，无法局部编辑。');
    return;
  }
  state.tool = 'select';

  const img = new Image();
  img.crossOrigin = 'anonymous';
  const overlay = el('div', { class: 'mask-editor' });
  const box = el('div', { class: 'mask-box' });
  const stageBox = el('div', { class: 'mask-stage' });
  const canvas = el('canvas');
  stageBox.append(canvas);
  const brush = el('input', { type: 'range', min: '4', max: '160', value: '36', style: { width: '140px' } });
  const brushLabel = el('span', { class: 'hint', text: '笔刷 36' });
  brush.addEventListener('input', () => { brushLabel.textContent = `笔刷 ${brush.value}`; });

  const promptInput = el('textarea', {
    value: node.data?.prompt || '',
    placeholder: '描述要修改的区域…',
  });

  const bar = el('div', { class: 'mask-bar' },
    el('label', {}, '笔刷', brush, brushLabel),
    el('button', { class: 'btn sm', text: '擦除全部', onClick: () => clearMask() }),
    el('span', { style: { flex: '1' } }),
    el('button', { class: 'btn sm', text: '取消', onClick: () => close() }),
    el('button', { class: 'btn sm primary', text: '应用编辑', onClick: () => apply() }),
  );

  box.append(el('div', { class: 'panel-title', text: '局部编辑 —— 涂掉要改的区域' }),
    el('div', { class: 'hint', text: '被涂掉的透明区域即为要修改的部分；其余区域保持原样。' }),
    stageBox, promptInput, bar);
  overlay.append(box);
  document.body.append(overlay);

  let ctx = null;
  let painting = false;
  let last = null;

  // strokeCanvas：笔迹画布（涂过的地方为不透明黑）
  // maskCanvas：导出用（涂过的地方为**透明** alpha=0，符合 OpenAI 遮罩约定）
  const strokeCanvas = document.createElement('canvas');
  let strokeCtx = null;
  let maskCtx = null;

  function clearMask() {
    if (!strokeCtx) return;
    strokeCtx.globalCompositeOperation = 'source-over';
    strokeCtx.clearRect(0, 0, strokeCanvas.width, strokeCanvas.height);
    redraw();
  }

  function redraw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    // 把笔迹染成红色半透明，标出「将被修改」的区域
    const tint = document.createElement('canvas');
    tint.width = canvas.width;
    tint.height = canvas.height;
    const tctx = tint.getContext('2d');
    tctx.drawImage(strokeCanvas, 0, 0);
    tctx.globalCompositeOperation = 'source-in';
    tctx.fillStyle = 'rgba(239,68,68,.45)';
    tctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(tint, 0, 0);
  }

  /** 生成导出用的遮罩：未涂区域不透明，涂过的区域透明。 */
  function buildMaskCanvas() {
    const out = document.createElement('canvas');
    out.width = strokeCanvas.width;
    out.height = strokeCanvas.height;
    const octx = out.getContext('2d');
    octx.fillStyle = 'rgba(0,0,0,1)';
    octx.fillRect(0, 0, out.width, out.height);
    octx.globalCompositeOperation = 'destination-out';
    octx.drawImage(strokeCanvas, 0, 0);
    octx.globalCompositeOperation = 'source-over';
    return out;
  }

  function pointOf(e) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) * (canvas.width / rect.width),
      y: (e.clientY - rect.top) * (canvas.height / rect.height),
    };
  }

  function strokeTo(from, to) {
    strokeCtx.globalCompositeOperation = 'source-over';
    strokeCtx.lineWidth = Number(brush.value);
    strokeCtx.lineCap = 'round';
    strokeCtx.lineJoin = 'round';
    strokeCtx.strokeStyle = 'rgba(0,0,0,1)';
    strokeCtx.beginPath();
    strokeCtx.moveTo(from.x, from.y);
    strokeCtx.lineTo(to.x, to.y);
    strokeCtx.stroke();
    redraw();
  }

  img.onload = () => {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    strokeCanvas.width = canvas.width;
    strokeCanvas.height = canvas.height;
    ctx = canvas.getContext('2d');
    strokeCtx = strokeCanvas.getContext('2d');
    maskCtx = null;
    clearMask();
  };
  img.src = `/api/assets/${node.asset_id}/raw`;

  canvas.addEventListener('mousedown', (e) => {
    painting = true;
    last = pointOf(e);
    e.preventDefault();
  });
  canvas.addEventListener('mousemove', (e) => {
    if (!painting) return;
    const p = pointOf(e);
    strokeTo(last, p);
    last = p;
  });
  window.addEventListener('mouseup', () => { painting = false; });

  function close() {
    overlay.remove();
  }

  async function apply() {
    const prompt = promptInput.value.trim();
    if (!prompt) { toastInfo('请描述要修改的内容。'); return; }
    if (!state.gen.channel_id || !state.gen.model) {
      toastInfo('请先在右侧面板选择渠道与模型。');
      return;
    }
    const blob = await new Promise((resolve) => buildMaskCanvas().toBlob(resolve, 'image/png'));
    if (!blob) { toastInfo('遮罩导出失败。'); return; }
    const file = new File([blob], `mask-${node.id}.png`, { type: 'image/png' });

    const btn = bar.querySelector('.primary');
    btn.disabled = true;
    btn.textContent = '提交中…';
    try {
      const up = await api.upload([file], 'mask');
      const maskId = (up.assets || [])[0]?.id;
      if (!maskId) throw new Error('遮罩上传失败');

      const data = await api.generate({
        prompt,
        channel_id: state.gen.channel_id,
        model: state.gen.model,
        size: state.gen.size,
        count: 1,
        output_format: state.gen.output_format,
        reference_images: [node.asset_id],
        masks: [maskId],
      });
      const img2 = (data.images || [])[0];
      if (!img2) throw new Error('没有产物');
      node.asset_id = img2.id;
      node.data = {
        ...(node.data || {}),
        prompt,
        warnings: data.warnings || [],
        actual_format: img2.format,
        actual_size: `${img2.width}x${img2.height}`,
        task_id: data.task_id,
      };
      state.dirty = true;
      saveSoon();
      drawCanvas();
      renderSide();
      close();
      for (const w of (data.warnings || [])) toastWarn(`${w.message}\n${w.hint || ''}`.trim());
      toastOk('局部编辑完成');
    } catch (e) {
      toastError(e);
      btn.disabled = false;
      btn.textContent = '应用编辑';
    }
  }
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

  // ---- 画布级生成设置
  side.append(el('div', { class: 'panel-title', text: '生成设置' }));
  const selChannel = el('select');
  for (const ch of state.channels) {
    selChannel.append(el('option', {
      value: ch.id, text: ch.display_name || ch.id,
    }));
  }
  if (state.channels.length) selChannel.value = state.gen.channel_id;

  const selModel = el('select');
  const ch = state.channels.find((x) => x.id === state.gen.channel_id);
  for (const m of (ch?.models || [])) selModel.append(el('option', { value: m, text: m }));
  selModel.value = state.gen.model;

  const selSize = el('select');
  for (const s of ['512x512', '768x768', '896x896', '1024x1024', '1536x1024', '2048x2048']) {
    selSize.append(el('option', { value: s, text: s }));
  }
  selSize.value = state.gen.size;

  const selFormat = el('select');
  const cap = (ch?.capabilities || []).find((c) => c.model === state.gen.model)?.capability;
  const formats = Object.keys(cap?.output_formats || { png: 1 });
  for (const f of formats) {
    selFormat.append(el('option', { value: f, text: formatLabel(f) }));
  }
  if (formats.includes(state.gen.output_format)) selFormat.value = state.gen.output_format;

  selChannel.addEventListener('change', () => {
    state.gen.channel_id = selChannel.value;
    const c2 = state.channels.find((x) => x.id === selChannel.value);
    clear(selModel);
    for (const m of (c2?.models || [])) selModel.append(el('option', { value: m, text: m }));
    state.gen.model = (c2?.models || [])[0] || '';
    selModel.value = state.gen.model;
    state.dirty = true; saveSoon();
    renderSide();
  });
  selModel.addEventListener('change', () => {
    state.gen.model = selModel.value;
    state.dirty = true; saveSoon();
    renderSide();
  });
  selSize.addEventListener('change', () => {
    state.gen.size = selSize.value; state.dirty = true; saveSoon();
  });
  selFormat.addEventListener('change', () => {
    state.gen.output_format = selFormat.value; state.dirty = true; saveSoon();
  });

  side.append(el('label', { class: 'field' }, '渠道', selChannel));
  side.append(el('label', { class: 'field' }, '模型', selModel));
  side.append(el('label', { class: 'field' }, '尺寸', selSize));
  side.append(el('label', { class: 'field' }, '输出格式', selFormat));

  // ---- 选中连线
  if (state.selectedEdge) {
    const edge = state.edges.find((e) => e.id === state.selectedEdge);
    if (edge) {
      side.append(el('div', { class: 'panel-title', text: '连线' }));
      const a = state.nodes.find((n) => n.id === edge.source_id);
      const b = state.nodes.find((n) => n.id === edge.target_id);
      side.append(el('div', { class: 'hint',
        text: `${a?.data?.prompt?.slice(0, 18) || a?.id} → ${b?.data?.prompt?.slice(0, 18) || b?.id}` }));
      side.append(el('button', {
        class: 'btn danger', text: '删除连线',
        onClick: () => {
          state.edges = state.edges.filter((e) => e.id !== edge.id);
          state.selectedEdge = null;
          state.dirty = true; drawCanvas(); renderSide(); saveSoon();
        },
      }));
      return;
    }
  }

  const node = state.nodes.find((n) => n.id === state.selected);
  if (!node) {
    side.append(el('div', { class: 'hint', text: '选中一个节点以编辑其属性。' }));
    side.append(el('div', { class: 'hint',
      text: '从节点右侧圆点拖到另一个节点即可连线；点击连线可选中删除。' }));
    side.append(el('div', { class: 'hint',
      text: '节点标题栏的 ⚡ 可就地生成；工具栏 ✎ 进入局部编辑模式后点节点。' }));
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

    side.append(el('div', { class: 'row' },
      el('button', {
        class: 'btn primary sm', text: state.busy[node.id] ? '生成中…' : '在此生成',
        onClick: () => generateIntoNode(node),
      }),
      el('button', {
        class: 'btn sm', text: '局部编辑',
        title: '涂绘遮罩后只改指定区域',
        onClick: () => openMaskEditor(node),
      }),
    ));

    // 绑定已有资源
    const url = el('input', { type: 'text', value: node.data?.asset_url || '',
      placeholder: '/api/assets/xxx 或粘贴图片地址' });
    side.append(el('label', { class: 'field' }, '资源', url));
    side.append(el('button', {
      class: 'btn sm', text: '绑定资源',
      onClick: async () => {
        const v = url.value.trim();
        if (!v) { toastInfo('请填写资源地址或上传图片。'); return; }
        const m = v.match(/\/api\/assets\/([A-Za-z0-9_-]+)/);
        if (!m) {
          toastInfo('暂仅支持引用已上传的资源 ID。');
          return;
        }
        node.asset_id = m[1];
        node.data = { ...(node.data || {}), asset_url: v };
        state.dirty = true;
        drawCanvas(); renderSide(); saveSoon();
        toastOk('已绑定');
      },
    }));

    if (node.asset_id) {
      side.append(el('div', { class: 'hint',
        text: `当前产物 ${node.data?.actual_format || '?'} ${node.data?.actual_size || ''}` }));
    }

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
