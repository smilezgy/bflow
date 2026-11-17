// 前端运行时巡检 —— 用 jsdom 真实加载页面与模块，捕获运行时错误。
//
// 目的：静态语法检查通过 ≠ 运行时正常。模块顶层抛错会让整个模块图
// 静默失败，页面能显示但所有点击无反应、零 API 请求。
//
// 用法：node tools/frontend_check.mjs [base_url]

import { JSDOM, VirtualConsole } from 'jsdom';
import { readFileSync } from 'node:fs';

const BASE = process.argv[2] || 'http://127.0.0.1:8000';

let PASS = 0;
let FAIL = 0;
const FAILURES = [];

function check(name, ok, extra = '') {
  if (ok) { PASS++; console.log(`  OK   ${name}`); }
  else { FAIL++; FAILURES.push(name); console.log(`  FAIL ${name} ${extra}`); }
}

// ---------------------------------------------------------------- 抓取资源

const html = await (await fetch(`${BASE}/`)).text();
const jsFiles = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);
const cssFiles = [...html.matchAll(/<link[^>]+href="([^"]+)"/g)].map((m) => m[1]);

console.log(`[资源] ${BASE}`);
console.log(`  HTML ${html.length} 字节；JS ${jsFiles.length} 个；CSS ${cssFiles.length} 个\n`);

check('HTML 引用了脚本入口', jsFiles.some((f) => f.includes('app.js')), jsFiles.join(','));

// 用 esbuild 把 ES 模块图打成单文件 IIFE —— jsdom 的 eval 是 CommonJS
// 上下文，不认 import 语句，直接内联会误报。
const { build } = await import('esbuild');
const fsP = await import('node:fs/promises');
const pathMod = await import('node:path');
const here = pathMod.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const jsDir = pathMod.join(here, 'js');
await fsP.mkdir(jsDir, { recursive: true });
// HTML 里只引用了入口，模块图要靠 import 语句递归收集
const downloaded = new Set();
const queue = [...jsFiles.filter((f) => f.includes('/static/js/'))];
while (queue.length) {
  const f = queue.shift();
  if (downloaded.has(f)) continue;
  downloaded.add(f);
  const name = f.split('/').pop();
  const resp = await fetch(`${BASE}${f}`);
  const code = await resp.text();
  await fsP.writeFile(pathMod.join(jsDir, name), code);
  // 从 import 语句里找出同目录的相对依赖
  for (const m of code.matchAll(/from\s+'\.\/([^']+)'/g)) {
    queue.push(`/static/js/${m[1]}`);
  }
}
let inlined = '';
try {
  const out = await build({
    entryPoints: [pathMod.join(jsDir, 'app.js')],
    bundle: true,
    format: 'iife',
    write: false,
    logLevel: 'silent',
  });
  inlined = out.outputFiles[0].text;
} catch (e) {
  // 打包失败 = 语法错误。报告成失败项，别让脚本裸崩。
  const msg = (e.errors || []).map((x) => `${x.location?.file}:${x.location?.line} ${x.text}`)
    .join(' | ') || e.message;
  console.log('[模块求值]');
  check('模块图可打包（语法正确）', false, msg);
  console.log('\n' + '='.repeat(56));
  console.log(`通过 ${PASS} 项，失败 ${FAIL} 项`);
  console.log('失败项：\n  - 模块图可打包（语法正确）');
  process.exit(1);
}

// ---------------------------------------------------------------- jsdom

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(e.message || String(e)));
vc.on('error', (...a) => errors.push(a.map(String).join(' ')));

// 记录 API 调用，验证前端确实发了请求
const apiCalls = [];
const dom = new JSDOM(html, {
  url: BASE,
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  virtualConsole: vc,
});

const { window } = dom;
window.fetch = async (path, init) => {
  const url = String(path);
  if (url.startsWith('/api/')) apiCalls.push(`${init?.method || 'GET'} ${url}`);
  return new Response('{}', {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
};
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.HTMLCanvasElement.prototype.getContext = () => ({
  measureText: () => ({ width: 10 }),
  fillRect() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
  arc() {}, fill() {}, stroke() {}, save() {}, restore() {}, translate() {},
  scale() {}, rotate() {}, drawImage() {}, createLinearGradient: () => ({ addColorStop() {} }),
  setTransform() {}, rect() {}, clip() {},
});
window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
window.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };

try {
  window.eval(inlined);
} catch (e) {
  errors.push(e.message || String(e));
}

// 模块脚本是异步求值的，等它跑完
await new Promise((r) => setTimeout(r, 800));

console.log('[模块求值]');
check('模块图求值无错误', errors.length === 0, errors.join(' | '));
check('init() 触发了 API 调用', apiCalls.length > 0, apiCalls.join(', '));

console.log('\n[视图渲染]');
for (const view of ['workbench', 'canvas', 'library', 'tasks', 'channels', 'settings']) {
  const before = errors.length;
  const btn = window.document.querySelector(`.nav-item[data-view="${view}"]`);
  check(`导航项 ${view} 存在`, !!btn);
  if (btn) {
    btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 350));
    const sec = window.document.getElementById(`view-${view}`);
    const active = sec?.classList.contains('active');
    const hasContent = (sec?.textContent || '').trim().length > 0;
    check(`视图 ${view} 渲染成功`, errors.length === before && active && hasContent,
      `active=${active} content=${hasContent} err=${errors.slice(before).join('|')}`);
  }
}

console.log('\n[最终状态]');
check('全程无未捕获错误', errors.length === 0, errors.join(' | '));
check('发出了 API 请求', apiCalls.length > 0, `${apiCalls.length} 个`);

if (apiCalls.length) {
  console.log(`  API 调用 (${apiCalls.length}):`);
  for (const c of [...new Set(apiCalls)].slice(0, 12)) console.log(`    ${c}`);
}

console.log('\n' + '='.repeat(56));
console.log(`通过 ${PASS} 项，失败 ${FAIL} 项`);
if (FAILURES.length) {
  console.log('失败项：');
  for (const f of FAILURES) console.log(`  - ${f}`);
}
process.exit(FAIL ? 1 : 0);
