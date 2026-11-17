// 图片预览组件的自测 —— jsdom，无浏览器、零费用。
//
// 验证任务/素材库共用的 lightbox：打开渲染大图、多图切换、关闭清理、
// 缩略图点击触发。运行：node tools/frontend_lightbox_check.mjs

import { JSDOM, VirtualConsole } from 'jsdom';
import { build } from 'esbuild';
import pathMod from 'node:path';
import { fileURLToPath } from 'node:url';

const here = pathMod.dirname(fileURLToPath(import.meta.url));
const root = pathMod.join(here, '..');

let PASS = 0, FAIL = 0;
const FAILURES = [];
function check(name, ok, extra = '') {
  if (ok) { PASS++; console.log(`  OK   ${name}`); }
  else { FAIL++; FAILURES.push(name); console.log(`  FAIL ${name} ${extra}`); }
}

console.log('=== 图片预览组件自测（jsdom）===\n');

// 把 lightbox.js 连同它依赖的 ui.js 打成一个 IIFE，暴露到 window
const out = await build({
  entryPoints: [pathMod.join(root, 'static/js/lightbox.js')],
  bundle: true,
  format: 'iife',
  globalName: 'BflowLightbox',
  write: false,
  logLevel: 'silent',
});
const code = out.outputFiles[0].text;

const vc = new VirtualConsole();
const errors = [];
vc.on('jsdomError', (e) => errors.push(e.message || String(e)));
vc.on('error', (...a) => errors.push(a.map(String).join(' ')));

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  virtualConsole: vc,
});
const { window } = dom;
try {
  window.eval(code);
} catch (e) {
  errors.push(e.message || String(e));
}

const LB = window.BflowLightbox;
check('模块加载成功', !!LB && typeof LB.openLightbox === 'function',
  errors.join(' | '));

const IMGS = [
  { id: 'a1', url: '/api/assets/a1/raw', format: 'png', width: 1024, height: 1024,
    bytes: 1234, prompt: '第一张' },
  { id: 'a2', url: '/api/assets/a2/raw', format: 'jpeg', width: 800, height: 600,
    bytes: 5678, prompt: '第二张' },
];

LB.openLightbox(IMGS, 0);
const layer = window.document.getElementById('lightboxLayer');
check('打开后 lightbox 出现在 DOM', !!layer && !!layer.querySelector('.lightbox'));
const img = layer?.querySelector('.lightbox img');
check('大图 src 指向 /raw 二进制', img?.getAttribute('src') === IMGS[0].url,
  img?.getAttribute('src') || '');
check('显示提示词', (layer?.textContent || '').includes('第一张'));
check('显示分辨率', (layer?.textContent || '').includes('1024×1024'));
check('下载按钮指向 /raw',
  layer?.querySelector('.lightbox-meta a')?.getAttribute('href') === IMGS[0].url);
check('多图时显示计数', (layer?.textContent || '').includes('1 / 2'));
check('有上一张/下一张按钮', layer?.querySelectorAll('.lightbox-nav').length === 2);

// 下一张
layer.querySelector('.lightbox-nav.next').click();
check('切换到第二张', layer.querySelector('img')?.getAttribute('src') === IMGS[1].url,
  layer.querySelector('img')?.getAttribute('src') || '');
check('第二张提示词更新', (layer.textContent || '').includes('第二张'));

// ESC 关闭
window.document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
check('ESC 关闭后 DOM 清空',
  !window.document.getElementById('lightboxLayer')?.querySelector('.lightbox'));

// 缩略图点击触发预览
const thumb = LB.thumbnail(IMGS[0], { items: IMGS });
window.document.body.append(thumb);
thumb.click();
check('点缩略图能重新打开预览',
  !!window.document.getElementById('lightboxLayer')?.querySelector('.lightbox'));

check('全程无未捕获错误', errors.length === 0, errors.join(' | '));

try { window.close(); } catch { /* ignore */ }
console.log('\n' + '='.repeat(56));
console.log(`通过 ${PASS} 项，失败 ${FAIL} 项`);
if (FAILURES.length) {
  console.log('失败项：');
  for (const f of FAILURES) console.log(`  - ${f}`);
}
process.exit(FAIL ? 1 : 0);
