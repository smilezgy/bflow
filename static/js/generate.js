// 快速生成视图 —— 工作台主界面。
// 设计见 spec/05-ui-ia.md §3。
//
// 核心规则：选项由渠道能力表驱动；产物格式/尺寸与请求不符时，
// 交付产物 + 琥珀色提示（不是错误）。

import { api } from './api.js';
import {
  clear, el, fmtBytes, formatLabel, toast, toastError, toastInfo, toastOk, toastWarn,
} from './ui.js';

const state = {
  channels: [],
  channel: null,       // 当前渠道详情
  refs: [],            // 参考图 [{id, url}]
  masks: [],           // 遮罩 [{id, url}]
  lastResult: null,
};

const refs = new WeakMap();

export function renderWorkbench(root) {
  clear(root);
  const wrap = el('div', { class: 'workbench' });

  // ---------------- 左：表单
  const form = el('div', { class: 'panel col' });
  form.append(el('div', { class: 'panel-title', text: '生成' }));

  const prompt = el('textarea', {
    placeholder: '描述你想要的画面…（Ctrl+Enter 生成）',
    id: 'genPrompt',
  });
  form.append(el('label', { class: 'field' }, '提示词', prompt));

  // 参考图
  const refStrip = el('div', { class: 'ref-strip' });
  const refInput = el('input', { type: 'file', accept: 'image/*', multiple: true, style: { display: 'none' } });
  refInput.addEventListener('change', () => handleUpload(refInput.files, 'reference'));
  form.append(
    el('div', { class: 'row' },
      el('span', { class: 'hint', text: '参考图' }),
      el('button', { class: 'btn sm', text: '+ 添加', onClick: () => refInput.click() }),
      refInput),
    refStrip);

  // 选项
  const opts = el('div', { class: 'opt-grid' });
  const selChannel = el('select');
  const selModel = el('select');
  const selSize = el('select');
  const selFormat = el('select');
  const selQuality = el('select');
  const selComplexity = el('select');
  const selBackground = el('select');
  const selCompression = el('input', { type: 'number', min: '0', max: '100', value: '80' });
  const cnt = el('input', { type: 'number', min: '1', max: '4', value: '1' });

  opts.append(
    field('渠道', selChannel), field('模型', selModel),
    field('尺寸', selSize), field('格式', selFormat),
    field('质量', selQuality), field('复杂度', selComplexity),
    field('背景', selBackground), field('压缩质量', selCompression),
    field('数量', cnt));
  form.append(opts);

  const genBtn = el('button', { class: 'btn primary', text: '生成' });
  form.append(el('div', { class: 'row' }, genBtn,
    el('span', { class: 'hint', id: 'genHint' })));

  // ---------------- 右：预览
  const preview = el('div', { class: 'preview' });
  const stage = el('div', { class: 'preview-stage' });
  const meta = el('div', { class: 'preview-meta' });
  const actions = el('div', { class: 'row' });
  preview.append(stage, meta, actions);

  wrap.append(form, preview);
  root.append(wrap);

  refs.set(form, { prompt, selChannel, selModel, selSize, selFormat, selQuality,
    selComplexity, selBackground, selCompression, cnt, genBtn, stage, meta, actions,
    refStrip, refInput });

  state.preview = { stage, meta, actions };

  // 事件
  selChannel.addEventListener('change', () => selectChannel(selChannel.value));
  selModel.addEventListener('change', () => applyCapability());
  selFormat.addEventListener('change', () => applyFormatRules());
  selBackground.addEventListener('change', () => applyFormatRules());
  selCompression.addEventListener('input', () => applyFormatRules());
  genBtn.addEventListener('click', doGenerate);
  prompt.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') doGenerate();
  });

  loadChannels();
}

function field(label, control) {
  return el('label', { class: 'field' }, label, control);
}

function ctx() { return refs.get(document.getElementById('view-workbench')?.querySelector('.panel')); }

// ---------------------------------------------------------------- 渠道与能力

async function loadChannels() {
  try {
    const data = await api.channels();
    state.channels = data.channels || [];
    const c = ctx();
    clear(c.selChannel);
    if (!state.channels.length) {
      c.selChannel.append(el('option', { value: '', text: '（暂无渠道，请先到「渠道」添加）' }));
      c.selChannel.disabled = true;
      c.genBtn.disabled = true;
      return;
    }
    c.selChannel.disabled = false;
    c.genBtn.disabled = false;
    for (const ch of state.channels) {
      c.selChannel.append(el('option', {
        value: ch.id,
        text: `${ch.display_name || ch.id}${ch.enabled ? '' : '（已停用）'}`,
      }));
    }
    selectChannel(state.channels[0].id);
  } catch (e) {
    toastError(e);
  }
}

function selectChannel(id) {
  const ch = state.channels.find((x) => x.id === id);
  if (!ch) return;
  state.channel = ch;
  const c = ctx();
  c.selChannel.value = id;
  clear(c.selModel);
  const models = ch.models || [];
  for (const m of models) c.selModel.append(el('option', { value: m, text: m }));
  if (!models.length) {
    c.selModel.append(el('option', { value: '', text: '（该渠道尚未配置模型）' }));
  }
  applyCapability();
}

function capability() {
  const c = ctx();
  if (!state.channel || !c.selModel.value) return null;
  const caps = state.channel.capabilities || [];
  const hit = caps.find((x) => x.model === c.selModel.value);
  return hit ? hit.capability : null;
}

function applyCapability() {
  const c = ctx();
  const cap = capability();
  const formats = cap?.output_formats || {};
  const fmtKeys = Object.keys(formats);

  clear(c.selFormat);
  if (!fmtKeys.length) {
    c.selFormat.append(el('option', { value: 'png', text: 'PNG' }));
  } else {
    for (const k of fmtKeys) {
      const info = formats[k] || {};
      const mark = info.supported === false ? '（不支持）'
        : (info.honors_request === false ? '（该渠道不保证按此格式出图）' : '');
      c.selFormat.append(el('option', { value: k, text: formatLabel(k) + mark }));
    }
  }

  // 尺寸
  clear(c.selSize);
  const sizes = ['512x512', '768x768', '896x896', '1024x1024', '1536x1024', '2048x2048'];
  for (const s of sizes) c.selSize.append(el('option', { value: s, text: s }));
  c.selSize.value = '1024x1024';

  fillLevels(c.selQuality, cap?.quality_levels, ['auto']);
  fillLevels(c.selComplexity, cap?.complexity_levels, ['auto']);

  clear(c.selBackground);
  for (const b of (cap?.backgrounds || ['auto', 'transparent', 'opaque'])) {
    c.selBackground.append(el('option', { value: b, text: b === '' ? '默认' : b }));
  }

  applyFormatRules();
}

function fillLevels(sel, levels, always) {
  clear(sel);
  const set = new Set([...(always || []), ...(levels || [])]);
  for (const v of set) {
    sel.append(el('option', { value: v, text: v === 'auto' ? '默认' : v }));
  }
  if (!levels || !levels.length) sel.disabled = true; else sel.disabled = false;
}

/** 格式相关的联动规则（spec/05-ui-ia.md §3.2） */
function applyFormatRules() {
  const c = ctx();
  const fmt = c.selFormat.value;
  const cap = capability();

  // PNG 不接受 <100 压缩率 → 置灰并强制 100
  if (fmt === 'png') {
    c.selCompression.disabled = true;
    c.selCompression.value = '100';
  } else if (fmt === 'jpeg' || fmt === 'webp') {
    c.selCompression.disabled = false;
  } else {
    c.selCompression.disabled = true;
  }

  // 透明背景要求格式支持 alpha
  const allowed = cap?.transparent_formats || ['png', 'webp'];
  if (c.selBackground.value === 'transparent' && !allowed.includes(fmt)) {
    c.selBackground.value = '';
    toastInfo(`${formatLabel(fmt)} 不支持透明背景，已重置背景设置。`);
  }

  // SVG 仅支持单张
  if (fmt === 'svg') {
    c.cnt.value = '1';
    c.cnt.disabled = true;
  } else {
    c.cnt.disabled = false;
  }
}

// ---------------------------------------------------------------- 上传

async function handleUpload(files, purpose) {
  if (!files || !files.length) return;
  const c = ctx();
  try {
    const data = await api.upload(Array.from(files), purpose);
    const added = data.assets || [];
    if (purpose === 'mask') state.masks.push(...added);
    else state.refs.push(...added);
    renderRefs();
    toastOk(`已上传 ${added.length} 个文件`);
  } catch (e) {
    toastError(e);
  }
}

function renderRefs() {
  const c = ctx();
  clear(c.refStrip);
  for (const r of state.refs) {
    const item = el('div', { class: 'ref-item' },
      el('img', { src: r.url, alt: '' }));
    item.append(el('button', {
      class: 'rm', text: '×', title: '移除',
      onClick: () => {
        state.refs = state.refs.filter((x) => x.id !== r.id);
        renderRefs();
      },
    }));
    c.refStrip.append(item);
  }
}

// ---------------------------------------------------------------- 生成

async function doGenerate() {
  const c = ctx();
  const prompt = c.prompt.value.trim();
  if (!prompt) { toastInfo('请先输入提示词。'); return; }
  if (!state.channel || !c.selModel.value) { toastInfo('请先选择渠道与模型。'); return; }

  const body = {
    prompt,
    channel_id: state.channel.id,
    model: c.selModel.value,
    size: c.selSize.value,
    count: Number(c.cnt.value) || 1,
    output_format: c.selFormat.value,
    background: c.selBackground.value,
    quality: c.selQuality.value,
    complexity: c.selComplexity.value,
    reference_images: state.refs.map((r) => r.id),
    masks: state.masks.map((m) => m.id),
  };
  if (!c.selCompression.disabled) {
    body.output_compression = Number(c.selCompression.value);
  }

  c.genBtn.disabled = true;
  c.genBtn.textContent = '生成中…';
  clear(c.stage);
  c.stage.append(el('div', { class: 'skeleton' }));
  clear(c.meta); clear(c.actions);

  const started = Date.now();
  const tick = setInterval(() => {
    const b = c.genBtn;
    if (b) b.textContent = `生成中… ${Math.floor((Date.now() - started) / 1000)}s`;
  }, 1000);

  try {
    const data = await api.generate(body);
    state.lastResult = data;
    renderResult(data);
  } catch (e) {
    clear(c.stage);
    c.stage.append(el('div', { class: 'empty', text: '生成失败' }));
    toastError(e);
  } finally {
    clearInterval(tick);
    c.genBtn.disabled = false;
    c.genBtn.textContent = '生成';
  }
}

function renderResult(data) {
  const c = ctx();
  const images = data.images || [];
  const warnings = data.warnings || [];
  clear(c.stage);
  clear(c.meta);
  clear(c.actions);

  if (!images.length) {
    c.stage.append(el('div', { class: 'empty', text: '没有产物' }));
    return;
  }

  const img = images[0];
  c.stage.append(el('img', { src: img.url, alt: img.prompt || '' }));

  // 实际 vs 请求 的对照 —— 核心差异化
  c.meta.append(
    el('span', {}, '实际格式 ', el('b', { text: formatLabel(img.format) })),
    el('span', {}, '尺寸 ', el('b', { text: `${img.width}x${img.height}` })),
    el('span', {}, '大小 ', el('b', { text: fmtBytes(img.bytes) })),
    img.requested_format
      ? el('span', {}, '请求格式 ', el('b', { text: formatLabel(img.requested_format) }))
      : null,
    img.requested_size
      ? el('span', {}, '请求尺寸 ', el('b', { text: img.requested_size }))
      : null,
  );

  c.actions.append(
    el('a', { class: 'btn', href: img.url, download: `${img.id}.${img.format}`, text: '下载' }),
    el('button', {
      class: 'btn', text: '复制提示词',
      onClick: () => {
        navigator.clipboard?.writeText(c.prompt.value);
        toastOk('提示词已复制');
      },
    }),
  );

  // 偏差提示：琥珀色（产物有效，不是错误）
  for (const w of warnings) {
    toastWarn(`${w.message}\n${w.hint || ''}`.trim());
  }
  if (!warnings.length) {
    toastOk('生成完成');
  }
}
