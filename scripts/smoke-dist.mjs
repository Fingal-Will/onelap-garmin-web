import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { parseHTML } from 'linkedom';

const dist = fileURLToPath(new URL('../dist/', import.meta.url));
const html = await readFile(path.join(dist, 'index.html'), 'utf8');
const { window } = parseHTML(html);
const { document } = window;
assert.ok(document.querySelector('.startup-state'), '生产页面缺少 JavaScript 启动失败占位内容。');

async function importWithTimeout(url) {
  let timeout;
  try {
    await Promise.race([
      import(url),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error('生产页面启动超时。')), 10_000);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

Object.defineProperties(window, {
  self: { configurable: true, value: window },
  top: { configurable: true, value: window },
});
window.setInterval = () => 0;
window.clearInterval = () => {};

Object.defineProperties(globalThis, {
  window: { configurable: true, value: window },
  document: { configurable: true, value: document },
  Event: { configurable: true, value: window.Event },
  MutationObserver: { configurable: true, value: window.MutationObserver },
});

const moduleScripts = [...document.querySelectorAll('script[type="module"]')];
assert.equal(moduleScripts.length, 1, '生产页面必须只有一个模块入口。');
const script = moduleScripts[0].getAttribute('src');
assert.ok(script && !script.startsWith('/'), '生产页面必须使用相对模块路径。');
const entry = path.resolve(dist, script);
assert.ok(entry.startsWith(dist), '生产模块路径超出 dist 目录。');
const entrySource = await readFile(entry, 'utf8');
const dynamicChunks = [...entrySource.matchAll(/import\((["'`])(\.\/[^"'`]+\.js)\1\)/g)];
assert.ok(dynamicChunks.length, '生产入口缺少预期的动态加密模块。');
for (const match of dynamicChunks) {
  const chunk = path.resolve(path.dirname(entry), match[2]);
  assert.ok(chunk.startsWith(dist), '动态模块路径超出 dist 目录。');
  await stat(chunk);
}

await importWithTimeout(`${pathToFileURL(entry).href}?smoke=${Date.now()}`);

assert.equal(document.querySelector('h1')?.textContent, 'OneLap 到 Garmin 同步');
assert.ok(document.querySelector('#connection-form'), '首屏未渲染仓库连接表单。');
assert.ok(document.querySelector('#run-list .empty-state'), '首屏未渲染运行记录空状态。');
assert.equal(document.querySelector('.startup-state'), null, '启动占位内容未被应用替换。');

const embeddedPage = parseHTML(html);
const embeddedWindow = embeddedPage.window;
const embeddedDocument = embeddedWindow.document;
Object.defineProperties(embeddedWindow, {
  self: { configurable: true, value: embeddedWindow },
  top: { configurable: true, value: {} },
});
Object.defineProperties(globalThis, {
  window: { configurable: true, value: embeddedWindow },
  document: { configurable: true, value: embeddedDocument },
  Event: { configurable: true, value: embeddedWindow.Event },
  MutationObserver: { configurable: true, value: embeddedWindow.MutationObserver },
});

await importWithTimeout(`${pathToFileURL(entry).href}?embedded=${Date.now()}`);

assert.equal(embeddedDocument.querySelector('h1')?.textContent, '请在新窗口打开控制台');
assert.equal(embeddedDocument.querySelector('a[target="_blank"]')?.getAttribute('href'), './');

console.log('Pages 生产包首屏启动检查通过。');
