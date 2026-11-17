// 渠道管理视图 —— 展示与维护能力表。
// 设计见 spec/05-ui-ia.md §4。

import { api } from './api.js';
import {
  clear, confirmDialog, el, fmtAgo, formatLabel, modal, toastError, toastInfo, toastOk,
} from './ui.js';

let items = [];

export async function renderChannels(root) {
  clear(root);
  const wrap = el('div', { class: 'panel', style: { margin: '16px' } });
  const title = el('div', { class: 'panel-title' }, '渠道',
    el('span', { class: 'spacer' }));
  const addBtn = el('button', { class: 'btn primary sm', text: '+ 新增渠道' });
  addBtn.addEventListener('click', () => openEditor(null));
  title.append(addBtn);
  wrap.append(title);
  const body = el('div');
  wrap.append(body);
  root.append(wrap);

  try {
    const data = await api.channels();
    items = data.channels || [];
  } catch (e) {
    toastError(e);
    return;
  }

  clear(body);
  if (!items.length) {
    body.append(el('div', { class: 'empty', text: '还没有渠道。点击「新增渠道」开始配置。' }));
    return;
  }

  const grid = el('div', { class: 'ch-grid' });
  for (const ch of items) grid.append(chCard(ch));
  body.append(grid);
}

function chCard(ch) {
  const card = el('div', { class: 'ch-card' });
  const cap = ch.capabilities?.[0]?.capability || {};

  card.append(el('h4', {},
    el('span', { text: ch.display_name || ch.id }),
    ch.enabled ? el('span', { class: 'badge ok', text: '启用' })
      : el('span', { class: 'badge mute', text: '停用' }),
    ch.verified
      ? el('span', { class: 'badge info', text: `实测 ${fmtAgo(ch.last_verified_at)}` })
      : el('span', { class: 'badge mute', text: '未实测' }),
  ));

  card.append(el('div', { class: 'ch-meta' },
    el('div', { class: 'mono', text: maskUrl(ch.base_url) }),
    el('div', {}, `协议 ${ch.protocol_label || ch.protocol} · 模型 ${(ch.models || []).length} 个`),
  ));

  // 输出格式能力
  const caps = el('div', { class: 'cap-list' });
  const formats = cap.output_formats || {};
  for (const [k, info] of Object.entries(formats)) {
    let badge;
    if (info.supported === false) badge = el('span', { class: 'badge err', text: '不支持' });
    else if (info.honors_request === false) badge = el('span', { class: 'badge warn', text: '不保证格式' });
    else badge = el('span', { class: 'badge ok', text: '正常' });
    caps.append(el('div', { class: 'cap-line' },
      el('span', { class: 'k', text: formatLabel(k) }), badge));
  }
  card.append(caps);

  const meta = el('div', { class: 'cap-list', style: { marginTop: '8px' } });
  meta.append(el('div', { class: 'cap-line' },
    el('span', { class: 'k', text: '尺寸策略' }),
    el('span', { text: sizePolicyLabel(cap.size_policy) })));
  const bounds = cap.size_bounds;
  if (bounds) {
    meta.append(el('div', { class: 'cap-line' },
      el('span', { class: 'k', text: '像素区间' }),
      el('span', { class: 'mono small', text: `${bounds.min_pixels} ~ ${bounds.max_pixels}` })));
  }
  const edits = cap.edits;
  if (edits) {
    meta.append(el('div', { class: 'cap-line' },
      el('span', { class: 'k', text: '参考图编辑' }),
      el('span', { text: edits.supported ? `支持（≤${edits.max_reference_images} 张）` : '不支持' })));
  }
  card.append(meta);

  const bar = el('div', { class: 'row', style: { marginTop: '12px' } });
  bar.append(
    el('button', { class: 'btn sm', text: '能力表', onClick: () => openCapabilityEditor(ch) }),
    el('button', {
      class: 'btn sm', text: '验证',
      onClick: () => {
        confirmDialog('验证渠道',
          `将向该渠道发起一次真实请求以刷新能力表，可能产生费用。\n渠道：${ch.display_name || ch.id}`,
          () => doVerify(ch), '发起验证');
      },
    }),
    el('button', { class: 'btn sm', text: '编辑', onClick: () => openEditor(ch) }),
    el('button', {
      class: 'btn sm danger', text: '删除',
      onClick: () => confirmDialog('删除渠道', `确定删除「${ch.display_name || ch.id}」？`,
        () => doDelete(ch)),
    }),
  );
  card.append(bar);
  return card;
}

function maskUrl(url) {
  if (!url) return '';
  try {
    const u = new URL(url);
    const host = u.hostname;
    const masked = host.length > 6
      ? host.slice(0, 3) + '•'.repeat(Math.min(10, host.length - 5)) + host.slice(-2)
      : host;
    return `${u.protocol}//${masked}${u.pathname === '/' ? '' : u.pathname}`;
  } catch { return url; }
}

function sizePolicyLabel(p) {
  return { reject: '越界即报错', normalize_silently: '静默归一化', fixed: '固定输出' }[p] || (p || '未知');
}

async function doVerify(ch) {
  try {
    await api.channelVerify(ch.id);
    toastOk('验证完成，能力表已刷新');
    renderChannels(document.getElementById('view-channels'));
  } catch (e) {
    toastError(e);
  }
}

async function doDelete(ch) {
  try {
    await api.channelDelete(ch.id);
    toastOk('已删除');
    renderChannels(document.getElementById('view-channels'));
  } catch (e) {
    toastError(e);
  }
}

// ---------------------------------------------------------------- 编辑渠道

/** 各协议的额外配置项说明 —— 协议差异很大，配置项也不同。 */
const PROTOCOL_FIELDS = {
  'openai-images': [],
  'gemini': [
    ['aspect_ratio', '宽高比', '1:1 / 3:2 / 2:3 / 16:9 / 9:16', '留空则按所选尺寸推导'],
    ['image_size', '图像尺寸档', '1K / 2K', '部分模型支持'],
  ],
  'ark': [
    ['size', '尺寸', '1024x1024 或 2K / 4K', '留空则用所选尺寸'],
    ['response_format', '响应格式', 'url / b64_json', '默认 url'],
    ['watermark', '加水印', 'true / false', '留空则不设置'],
    ['max_reference_images', '参考图上限', '数字', '默认 10'],
  ],
  'apimart': [
    ['aspect_ratio', '宽高比', '1:1 / 16:9 ...', '部分模型支持'],
    ['num_images', '生成数量', '数字', '留空则用所选数量'],
    ['output_format', '产物格式', 'png / jpeg', '按模型而定'],
  ],
  'comfyui': [
    ['prompt_node', '提示词节点 id', '如 6', 'CLIPTextEncode 节点的编号'],
    ['image_node', '参考图节点 id', '如 10', 'LoadImage 节点的编号'],
    ['size_node', '尺寸节点 id', '如 5', 'EmptyLatentImage 节点的编号'],
    ['seed_node', '种子节点 id', '如 3', 'KSampler 节点的编号'],
  ],
  'runninghub': [
    ['workflow_id', '工作流 id', '数字', '必填'],
    ['prompt_node', '提示词节点 id', '如 12', '必填'],
    ['negative_prompt', '负向提示词', '文本', '可选'],
  ],
};

async function openEditor(ch) {
  const isNew = !ch;
  let protocols = [];
  try {
    const d = await api.protocols();
    protocols = d.protocols || [];
  } catch { /* 兜底用内置列表 */ }
  if (!protocols.length) {
    protocols = [{ id: 'openai-images', label: 'OpenAI Images', hint: '' }];
  }

  const f = {
    display_name: el('input', { type: 'text', value: ch?.display_name || '' }),
    base_url: el('input', { type: 'text', value: ch?.base_url || '', placeholder: 'https://example.com' }),
    api_key: el('input', { type: 'password', value: '', placeholder: isNew ? '必填' : '留空则不修改' }),
    protocol: el('select', {}),
    protocolHint: el('div', { class: 'hint', text: '' }),
    configBox: el('div', { class: 'col' }),
  };

  for (const p of protocols) {
    f.protocol.append(el('option', { value: p.id, text: p.label }));
  }
  f.protocol.value = ch?.protocol || 'openai-images';

  // 协议专属配置
  const cfgInputs = {};
  const renderConfigFields = () => {
    const pid = f.protocol.value;
    const rows = PROTOCOL_FIELDS[pid] || [];
    clear(f.configBox);
    if (!rows.length) {
      f.configBox.append(el('div', { class: 'hint', text: '该协议无需额外配置。' }));
      return;
    }
    f.configBox.append(el('div', { class: 'panel-title', text: '协议参数' }));
    for (const [key, label, placeholder, hint] of rows) {
      const input = el('input', { type: 'text', placeholder });
      const existing = (ch?.config || {})[key];
      if (existing !== undefined && existing !== null) input.value = String(existing);
      cfgInputs[key] = input;
      f.configBox.append(el('label', { class: 'field' }, label, input,
        hint ? el('div', { class: 'hint', text: hint }) : null));
    }
  };

  // ComfyUI 需要工作流模板（大段 JSON），单独给一个多行输入
  const workflowBox = el('textarea', {
    placeholder: '{ "3": {"class_type": "KSampler", ...} }',
    style: { minHeight: '120px', fontFamily: 'var(--mono)' },
  });
  const existingWorkflow = ch?.config?.workflow;
  if (typeof existingWorkflow === 'string') workflowBox.value = existingWorkflow;
  else if (existingWorkflow && typeof existingWorkflow === 'object') {
    workflowBox.value = JSON.stringify(existingWorkflow, null, 2);
  }

  const renderWorkflow = () => {
    const isComfy = f.protocol.value === 'comfyui';
    f.configBox.querySelectorAll('[data-wf]').forEach((n) => n.remove());
    if (!isComfy) return;
    f.configBox.append(el('label', { class: 'field' }, '工作流模板（JSON）',
      workflowBox,
      el('div', { class: 'hint', text: '整份工作流贴进来即可；提示词会写入指定的 CLIPTextEncode 节点。' })));
  };

  f.protocol.addEventListener('change', () => {
    const p = protocols.find((x) => x.id === f.protocol.value);
    f.protocolHint.textContent = p?.hint || '';
    renderConfigFields();
    renderWorkflow();
  });
  const initial = protocols.find((x) => x.id === f.protocol.value);
  f.protocolHint.textContent = initial?.hint || '';
  renderConfigFields();
  renderWorkflow();

  const body = el('div', { class: 'col' },
    el('label', { class: 'field' }, '显示名', f.display_name),
    el('label', { class: 'field' }, '渠道地址', f.base_url),
    el('label', { class: 'field' }, 'API Key', f.api_key),
    el('label', { class: 'field' }, '协议', f.protocol),
    f.protocolHint,
    f.configBox,
    el('div', { class: 'hint', text: '渠道名与地址仅用于你自己识别；公开场合建议用代号。' }),
  );

  modal({
    title: isNew ? '新增渠道' : '编辑渠道',
    body,
    actions: [
      { label: '取消' },
      {
        label: '保存', kind: 'primary',
        onClick: async () => {
          if (!f.base_url.value.trim()) { toastInfo('请填写渠道地址。'); return false; }
          if (isNew && !f.api_key.value.trim()) { toastInfo('请填写 API Key。'); return false; }
          try {
            const config = { ...(ch?.config || {}) };
            for (const [key, input] of Object.entries(cfgInputs)) {
              const v = input.value.trim();
              if (v) config[key] = v;
              else delete config[key];
            }
            if (f.protocol.value === 'comfyui') {
              const raw = workflowBox.value.trim();
              if (raw) {
                try {
                  config.workflow = JSON.parse(raw);
                } catch {
                  toastInfo('工作流模板不是合法 JSON。');
                  return false;
                }
              } else {
                delete config.workflow;
              }
            }
            const payload = {
              display_name: f.display_name.value.trim() || undefined,
              base_url: f.base_url.value.trim(),
              protocol: f.protocol.value,
              config,
            };
            if (f.api_key.value.trim()) payload.api_key = f.api_key.value.trim();
            if (isNew) await api.channelCreate(payload);
            else await api.channelPatch(ch.id, payload);
            toastOk('已保存');
            renderChannels(document.getElementById('view-channels'));
          } catch (e) {
            toastError(e);
            return false;
          }
        },
      },
    ],
  });
}

// ---------------------------------------------------------------- 能力表编辑

function openCapabilityEditor(ch) {
  const cap = ch.capabilities?.[0];
  if (!cap) { toastInfo('该渠道还没有能力表记录。'); return; }

  const fmtState = {};
  const rows = el('div', { class: 'col' });
  for (const [k, info] of Object.entries(cap.capability.output_formats || {})) {
    const supported = el('input', { type: 'checkbox' });
    supported.checked = info.supported !== false;
    const honors = el('input', { type: 'checkbox' });
    honors.checked = info.honors_request !== false;
    fmtState[k] = { supported, honors };
    rows.append(el('div', { class: 'row' },
      el('span', { style: { minWidth: '60px' }, text: formatLabel(k) }),
      el('label', { class: 'row small' }, supported, ' 支持'),
      el('label', { class: 'row small' }, honors, ' 保证按此格式出图'),
    ));
  }

  const policy = el('select', {},
    el('option', { value: 'reject', text: '越界即报错' }),
    el('option', { value: 'normalize_silently', text: '静默归一化' }),
    el('option', { value: 'fixed', text: '固定输出' }));
  policy.value = cap.capability.size_policy || 'reject';

  const minPx = el('input', { type: 'number', value: cap.capability.size_bounds?.min_pixels ?? 655360 });
  const maxPx = el('input', { type: 'number', value: cap.capability.size_bounds?.max_pixels ?? 8294400 });
  const maxRefs = el('input', { type: 'number', value: cap.capability.edits?.max_reference_images ?? 0 });
  const editsSupported = el('input', { type: 'checkbox' });
  editsSupported.checked = !!cap.capability.edits?.supported;

  const body = el('div', { class: 'col' },
    el('div', { class: 'hint', text: '手动覆盖的能力会标记来源为「手动」，并替代实测值参与校验。' }),
    el('div', { class: 'panel-title', text: '输出格式' }),
    rows,
    el('div', { class: 'panel-title', text: '尺寸' }),
    el('label', { class: 'field' }, '策略', policy),
    el('div', { class: 'row' },
      el('label', { class: 'field grow' }, '像素下限', minPx),
      el('label', { class: 'field grow' }, '像素上限', maxPx)),
    el('div', { class: 'panel-title', text: '编辑' }),
    el('label', { class: 'row small' }, editsSupported, ' 支持参考图编辑'),
    el('label', { class: 'field' }, '参考图上限', maxRefs),
  );

  modal({
    title: `能力表 · ${ch.display_name || ch.id}`,
    body,
    width: '620px',
    actions: [
      { label: '取消' },
      {
        label: '保存', kind: 'primary',
        onClick: async () => {
          const formats = {};
          for (const [k, s] of Object.entries(fmtState)) {
            formats[k] = { supported: s.supported.checked, honors_request: s.honors.checked };
          }
          const next = {
            ...cap.capability,
            output_formats: formats,
            size_policy: policy.value,
            size_bounds: {
              ...(cap.capability.size_bounds || {}),
              min_pixels: Number(minPx.value) || 655360,
              max_pixels: Number(maxPx.value) || 8294400,
            },
            edits: {
              ...(cap.capability.edits || {}),
              supported: editsSupported.checked,
              max_reference_images: Number(maxRefs.value) || 0,
            },
          };
          try {
            await api.capabilityPut(ch.id, { model: cap.model, capability: next });
            toastOk('能力表已保存');
            renderChannels(document.getElementById('view-channels'));
          } catch (e) {
            toastError(e);
            return false;
          }
        },
      },
    ],
  });
}
