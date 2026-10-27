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
// linkedom models DOM only; supply native location/select/dialog behavior without a browser.
Object.defineProperty(window.HTMLSelectElement.prototype, 'value', {
  configurable: true,
  get() { return this.querySelector('option[selected]')?.value ?? this.querySelector('option')?.value ?? ''; },
  set(value) { for (const option of this.querySelectorAll('option')) { if (option.value === String(value)) option.setAttribute('selected', ''); else option.removeAttribute('selected'); } },
});
let networkCalls = 0;
globalThis.fetch = async () => { networkCalls++; throw new Error('离线交互验收不允许网络请求。'); };
window.location = { hash: '', href: 'https://example.test/onelap/' };
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

assert.equal(document.querySelector('h1')?.textContent, '同步概览');
assert.ok(document.querySelector('#connection-form'), '首屏未渲染仓库连接表单。');
assert.ok(document.querySelector('#settings-targets option[value="AUTO"][selected]'), '初次设置应默认自动判断区域。');
for (const id of ['sync-targets', 'reconcile-targets']) {
  assert.ok(document.querySelector(`#${id} option[value="DEFAULT"][selected]`), '手动任务应默认使用已保存设置。');
}
assert.match(document.querySelector('#garmin-cn-session-help').textContent, /未启用可留空/);
for (const id of ['sync-pages', 'reconcile-recent']) {
  assert.ok(document.querySelector(`#${id} option[value="DEFAULT"][selected]`), '页数和核对数量应默认使用已保存设置。');
}
for (const id of ['strava-client-id', 'strava-client-secret', 'strava-refresh-token', 'strava-session-key']) {
  assert.equal(document.querySelector(`#${id}`)?.type, 'password', 'Strava 凭据应隐藏输入。');
}
assert.equal(document.querySelector('#default-pages')?.value, '10');
assert.equal(document.querySelector('#strava-enabled')?.type, 'checkbox');
assert.equal(Boolean(document.querySelector('#strava-enabled').checked), false);

assert.ok(document.querySelector('#run-list .empty-state'), '首屏未渲染运行记录空状态。');
assert.equal(document.querySelector('.startup-state'), null, '启动占位内容未被应用替换。');

// Exercise the built production entry: navigation, filters, evidence, modal confirmation and demo isolation.
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.showModal = () => { dialog.open = true; dialog.setAttribute('open', ''); };
  dialog.close = () => { dialog.open = false; dialog.removeAttribute('open'); dialog.dispatchEvent(new window.Event('close')); };
}
const clickText = text => {
  const node = [...document.querySelectorAll('button')].find(button => button.textContent.trim() === text);
  assert.ok(node, `缺少交互入口：${text}`);
  assert.equal(Boolean(node.disabled), false, `入口意外禁用：${text}`);
  node.click();
};
const navigate = route => document.querySelector(`a[data-page="${route}"]`).click();
const credentialInput = document.querySelector('#onelap-password');
document.querySelector('#demo-toggle').click();
assert.match(document.querySelector('#data-banner').textContent, /均为虚构/);
assert.equal(document.querySelector('#onelap-password').disabled, true);
assert.ok([...document.querySelectorAll('#connection-form button')].every(button => button.disabled), '示例模式不能连接或断开真实仓库。');
navigate('activities');
assert.equal(document.querySelector('#page-title').textContent, '活动');
assert.equal(document.querySelectorAll('#activity-results tbody tr').length, 10);
const search = document.querySelector('#activity-search');
search.value = '周末耐力'; search.dispatchEvent(new window.Event('input'));
assert.equal(document.querySelectorAll('#activity-results tbody tr').length, 1);
search.value = '不存在的测试活动'; search.dispatchEvent(new window.Event('input'));
assert.match(document.querySelector('#activity-results').textContent, /没有符合条件/);
clickText('清空');
const statusFilter = document.querySelector('#filter-status');
statusFilter.value = 'auth_required'; statusFilter.dispatchEvent(new window.Event('change'));
assert.equal(document.querySelectorAll('#activity-results tbody tr').length, 1);
document.querySelector('#activity-results .status-button').click();
assert.equal(document.querySelector('#page-title').textContent, '周末耐力骑行');
assert.match(document.querySelector('#target-GLOBAL').textContent, /需要授权/);
assert.match(document.querySelector('#target-CN').textContent, /上传成功响应/);
clickText('高级操作：指定活动强制重传');
assert.equal(document.querySelector('#manual-drawer').open, true);
assert.match(document.querySelector('#manual-scope').textContent, /Strava/);
document.querySelector('#manual-submit').click();
assert.equal(document.querySelector('#force-dialog').open, true);
assert.match(document.querySelector('#force-dialog').textContent, /Garmin 中国区.*Garmin 国际区.*Strava/s);
assert.equal(document.querySelector('#force-submit').disabled, true);
const acknowledgement = document.querySelector('#force-ack');
acknowledgement.checked = true; acknowledgement.dispatchEvent(new window.Event('change'));
assert.equal(document.querySelector('#force-submit').disabled, false);
document.querySelector('#force-submit').click();
await Promise.resolve(); await Promise.resolve();
assert.equal(document.querySelector('#force-dialog').open, false);
assert.match(document.querySelector('#manual-status').textContent, /没有提交任务/);
document.querySelector('#manual-drawer').close();
navigate('runs');
const reports = [...document.querySelectorAll('#page-content button')].filter(button => button.textContent.trim() === '查看报告');
assert.equal(reports.length, 2); reports[1].click();
assert.match(document.querySelector('#page-content').textContent, /台账未保存/);
assert.match(document.querySelector('#page-content').textContent, /独立演示/);
navigate('reconcile');
assert.match(document.querySelector('#reconcile-results').textContent, /未核实 \/ 歧义/);
assert.equal(document.querySelector('#reconcile-targets').disabled, true);
navigate('settings');
document.querySelector('#settings-tab-processing').click();
assert.equal(document.querySelector('#settings-panel-processing').hidden, false);
assert.equal(document.querySelector('#settings-tab-processing').getAttribute('aria-selected'), 'true');
assert.equal(document.querySelector('#onelap-password'), credentialInput, '导航不应重建或复制凭据表单。');
clickText('恢复首次配置向导');
assert.equal(document.querySelector('#page-title').textContent, '首次配置');
clickText('确认，继续 →'); clickText('确认，继续 →');
assert.ok(document.querySelector('#setup-activity'));
const setupActivity = document.querySelector('#setup-activity');
setupActivity.value = 'DEMO-1025-0001'; setupActivity.dispatchEvent(new window.Event('change'));
assert.match(document.querySelector('#setup-validation').textContent, /周末耐力/);
assert.equal([...document.querySelectorAll('#setup-validation button')].find(button => button.textContent.trim() === '执行单条真实上传').disabled, true);
assert.equal(document.querySelector('#setup-simulation-ack').disabled, true);
clickText('执行单条模拟');
assert.equal(document.querySelector('#sync-dry-run').checked, true);
document.querySelector('#manual-submit').click();
await Promise.resolve();
document.querySelector('#manual-drawer').close();
assert.match(document.querySelector('#setup-validation').textContent, /示例模拟步骤已体验/);
const reviewed = document.querySelector('#setup-simulation-ack');
assert.equal(Boolean(reviewed.disabled), false);
reviewed.checked = true; reviewed.dispatchEvent(new window.Event('change'));
assert.equal([...document.querySelectorAll('#setup-validation button')].find(button => button.textContent.trim() === '执行单条真实上传').disabled, false);
clickText('执行单条真实上传');
assert.equal(document.querySelector('#sync-dry-run').checked, false);
document.querySelector('#manual-drawer').close();
const visible = document.querySelector('#setup-visible-ack');
visible.checked = true; visible.dispatchEvent(new window.Event('change'));
clickText('确认，继续 →');
assert.ok(document.querySelector('#history-ack'));
assert.equal([...document.querySelectorAll('#page-content button')].find(button => button.textContent.trim() === '确认，继续 →').disabled, true);
const history = document.querySelector('#history-ack');
history.checked = true; history.dispatchEvent(new window.Event('change'));
clickText('确认，继续 →');
assert.match(document.querySelector('#page-content').textContent, /准备开启自动同步/);
clickText('暂时保持手动同步');
document.querySelector('#demo-toggle').click();
assert.doesNotMatch(document.querySelector('#page-content').textContent, /周末耐力/);
assert.equal(document.querySelector('#onelap-password').disabled, false);
assert.equal(networkCalls, 0, '示例浏览、筛选和确认不得触发真实网络请求。');
const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
assert.equal(new Set(ids).size, ids.length, '搬移表单后不能出现重复 id。');

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

console.log('Pages 生产包启动、导航、筛选、详情、示例隔离、强制确认和嵌入保护检查通过。');
