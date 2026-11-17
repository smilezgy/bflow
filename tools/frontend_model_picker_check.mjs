// 模型选择浮层自测（jsdom）—— 验证「拉取 → 勾选 → 应用」链路。
//
// 背景：曾经发生过 JS 语法错误但页面照常显示、所有点击静默失效的情况。
// 这里不只看语法，而是真的打开浮层、切换分类、勾选、点应用，断言结果。
//
// 用法：node tools/frontend_model_picker_check.mjs

import { build } from 'esbuild';
import { JSDOM, VirtualConsole } from 'jsdom';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
let PASS = 0;
let FAIL = 0;
const FAILURES = [];
const check = (name, ok, extra = '') => {
  if (ok) { PASS++; console.log(`  OK   ${name}`); }
  else { FAIL++; FAILURES.push(name); console.log(`  FAIL ${name} ${extra}`); }
};

const out = await build({
  entryPoints: [path.join(here, '..', 'static', 'js', 'channels.js')],
  bundle: true, format: 'iife', globalName: 'PickerTest',
  write: false, logLevel: 'silent',
});

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(e.message || String(e)));
vc.on('error', (...a) => errors.push(a.map(String).join(' ')));
const dom = new JSDOM('<!doctype html><html><body><div id="modalLayer"></div><div id="toastLayer"></div></body></html>', {
  url: 'http://127.0.0.1/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
});
const { window } = dom;
const doc = window.document;
window.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, data: {} }) });
window.eval(out.outputFiles[0].text);

const fetched = [
  { model: 'gpt-image-2.5', category: 'image', suggested_protocol: 'openai-images' },
  { model: 'gpt-image-2.6', category: 'image', suggested_protocol: 'openai-images' },
  { model: 'nano-banana-2', category: 'image', suggested_protocol: 'openai-images' },
  { model: 'gpt-6.1-sol', category: 'chat', suggested_protocol: 'openai-images' },
  { model: 'veo3.1', category: 'video', suggested_protocol: 'openai-images' },
];
const rows = new Map([
  ['gpt-image-2.5', { protocol: 'openai-images', enabled: true, dirty: false }],
  ['gpt-image-2.6', { protocol: 'openai-images', enabled: false, dirty: false }],
  ['nano-banana-2', { protocol: 'openai-images', enabled: true, dirty: false }],
]);
let applied = null;
const open = () => window.PickerTest.openModelPicker({
  fetched, rows, defaultProtocol: 'openai-images', onApply: (next) => { applied = next; },
});
const buttonByText = (text) => [...doc.querySelectorAll('.picker-foot button, .picker-cat-tab')]
  .find((b) => b.textContent.includes(text));

console.log('=== 模型选择浮层自测（jsdom）===\n');
check('模块加载成功', typeof window.PickerTest?.openModelPicker === 'function', errors.join(' | '));
open();

const overlay = doc.querySelector('.picker-overlay');
check('打开后浮层出现在 DOM', !!overlay);
check('标题与参考项目一致',
  (overlay?.querySelector('.picker-title')?.textContent || '').includes('从上游拉取的模型清单'));
check('总数文案正确',
  (overlay?.querySelector('.picker-count')?.textContent || '').includes('共 5 个模型 · 当前显示 5 个'),
  overlay?.querySelector('.picker-count')?.textContent || '');
check('默认只勾选已启用模型（停用的不勾选）',
  overlay?.querySelectorAll('.picker-row').length === 5 &&
  overlay?.querySelectorAll('.picker-checkbox.checked').length === 2,
  `rows=${overlay?.querySelectorAll('.picker-row').length} checked=${overlay?.querySelectorAll('.picker-checkbox.checked').length}`);

// 用户取消一个已启用但不再需要的模型 —— 验证"应用"会真正移除它。
const unneeded = [...overlay.querySelectorAll('.picker-row')]
  .find((r) => (r.textContent || '').includes('nano-banana-2'));
unneeded?.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

const videoTab = buttonByText('视频');
videoTab?.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('分类筛选只显示视频',
  overlay?.querySelectorAll('.picker-row').length === 1 &&
  (overlay?.querySelector('.picker-model-label')?.textContent || '') === 'veo3.1');

overlay?.querySelector('.picker-row')?.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('勾选后汇总更新',
  (doc.querySelector('.picker-summary')?.textContent || '').includes('视频 1') &&
  (doc.querySelector('.picker-summary')?.textContent || '').includes('未选 3'),
  doc.querySelector('.picker-summary')?.textContent || '');

buttonByText('应用到模型列表')?.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('应用后浮层关闭', !doc.querySelector('.picker-overlay'));
check('只应用勾选的模型（含取消勾选的已登记模型）',
  applied && applied.size === 2 &&
  applied.has('gpt-image-2.5') && applied.has('veo3.1') && !applied.has('gpt-image-2.6'),
  applied ? [...applied.keys()].join(', ') : '未回调');
check('新模型继承建议协议', applied?.get('veo3.1')?.protocol === 'openai-images');

// 重新勾选之前停用的模型 → 应用后应恢复 enabled
rows.clear();
for (const [k, v] of applied) rows.set(k, v);
open();
const disabledRow = [...doc.querySelectorAll('.picker-row')]
  .find((r) => (r.textContent || '').includes('gpt-image-2.6'));
check('停用模型默认不勾选',
  !disabledRow?.querySelector('.picker-checkbox')?.classList.contains('checked'));
disabledRow?.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
buttonByText('应用到模型列表')?.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('重新勾选会启用模型', applied?.get('gpt-image-2.6')?.enabled === true,
  String(applied?.get('gpt-image-2.6')?.enabled));

open();
const search = doc.querySelector('.picker-search');
search.value = 'nano';
search.dispatchEvent(new window.Event('input', { bubbles: true }));
check('搜索按名称过滤',
  doc.querySelectorAll('.picker-row').length === 1 &&
  (doc.querySelector('.picker-model-label')?.textContent || '') === 'nano-banana-2');
doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
check('ESC 关闭浮层', !doc.querySelector('.picker-overlay'));

check('全程无未捕获错误', errors.length === 0, errors.join(' | '));

console.log('\n' + '='.repeat(56));
console.log(`通过 ${PASS} 项，失败 ${FAIL} 项`);
if (FAILURES.length) {
  console.log('失败项：');
  for (const f of FAILURES) console.log(`  - ${f}`);
}
try { dom.window.close(); } catch { /* ignore */ }
process.exit(FAIL ? 1 : 0);
