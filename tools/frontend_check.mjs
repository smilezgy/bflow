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
  // 'dangerously' 才会执行 HTML 里的内联脚本 —— index.html 里的加载兜底
  // 脚本必须被执行，才能验证「模块崩了会给出提示」这条链路。
  // 外部 <script src> 不会被加载（未开启 resources: 'usable'），
  // 真正的模块代码由下面的 esbuild 产物注入。
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc,
});

const { window } = dom;

// GET 优先打真实服务（这样才能验证前后端契约对不对得上），
// 写操作一律 mock 掉。
//
// 早先版本对所有请求都返回空 JSON，前端据此判断「还没有画布」，于是
// 每次巡检都真的 POST /api/canvases —— 往用户的真实数据目录里塞一个
// 空白画布。巡检工具不该污染被测系统。
const MOCK_BODIES = {
  'GET /api/canvases': { canvases: [] },
  'GET /api/channels': { channels: [] },
  'GET /api/assets/tags': { tags: [] },
  'GET /api/assets': { assets: [] },
  'GET /api/tasks': { tasks: [] },
  'GET /api/app-info': { version: 'check' },
  'GET /api/config': { settings: {} },
};
const json = (obj) => new Response(JSON.stringify(obj), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
});
const realFetch = globalThis.fetch;

window.fetch = async (path, init) => {
  const url = String(path);
  const method = (init?.method || 'GET').toUpperCase();
  if (!url.startsWith('/api/')) return json({});
  apiCalls.push(`${method} ${url}`);
  if (method !== 'GET') {
    // 写操作：不落到真实服务
    return json({ id: 'cv_mock', canvases: [] });
  }
  try {
    const resp = await realFetch(`${BASE}${url}`, init);
    if (resp.ok) return resp;
  } catch { /* 服务不可达，降级到 mock */ }
  const key = `${method} ${url.split('?')[0]}`;
  return json(MOCK_BODIES[key] ?? {});
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

// 加载兜底：index.html 的内联脚本独立于模块图运行，负责把「模块崩了」
// 这类沉默的失败变成可见提示。这里验证它没被误触发，且前端确实就绪。
const bootState = window.__bflowBootState || null;
console.log('\n[加载兜底]');
check('加载兜底脚本已注入', !!bootState);
check('加载兜底未误报', !bootState?.reported, bootState?.detail || '');
check('前端已标记就绪', bootState?.booted === true);
check('未出现加载失败提示层', !window.document.getElementById('bflowBootError'));

// 反向验证：单独起一个「模块没加载」的壳，确认兜底真能弹出提示。
// 用户的界面就靠它把沉默的失败变成看得见的原因，所以这条链路本身
// 也必须被验证 —— 否则它可能一直坏着没人知道。
{
  const shell = new JSDOM(html, {
    url: BASE,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: new VirtualConsole(),
  });
  const w = shell.window;
  w.__bflowBootFail?.('巡检注入的模拟失败');
  const st = w.__bflowBootState;
  check('兜底能触发（模拟模块加载失败）', st?.reported === true);
  check('失败详情可读', !!(st?.detail && st.detail.includes('巡检注入')), st?.detail || '');
  const layer = w.document.getElementById('bflowBootError');
  check('提示层已渲染', !!layer);
  check('提示层含强刷指引',
    !!(layer && (layer.textContent || '').includes('Ctrl+Shift+R')));
  try { w.close(); } catch { /* ignore */ }
}

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
// 关掉 jsdom：清掉兜底脚本里那个 6 秒定时器，避免进程空等
try { dom.window.close(); } catch { /* ignore */ }
process.exit(FAIL ? 1 : 0);
