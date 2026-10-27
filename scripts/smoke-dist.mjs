import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { parseHTML } from 'linkedom';
import { fixtureReport, fixtureSnapshot, fixtureRuns } from '../tests/dashboard-fixtures.js';

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
// All API responses stay in this test. The production bundle has no fixture or demo mode.
const dispatches = [];
const unexpectedRequests = [];
let apiReport = structuredClone(fixtureReport);
const encodedFile = value => {
  const json = JSON.stringify(value);
  return { encoding: 'base64', size: Buffer.byteLength(json), content: Buffer.from(json).toString('base64') };
};
globalThis.fetch = async (url, options = {}) => {
  const request = new URL(url);
  const route = request.pathname.replace('/repos/alice/private-sync', '');
  const method = options.method || 'GET';
  assert.equal(request.origin, 'https://api.github.com');
  assert.ok(request.pathname.startsWith('/repos/alice/private-sync'));
  let data;
  if (method === 'POST' && route === '/actions/workflows/sync.yml/dispatches') {
    dispatches.push(JSON.parse(options.body));
    return new Response(null, { status: 204 });
  }
  if (method !== 'GET') unexpectedRequests.push(`${method} ${route}`);
  else if (!route) data = { private: true, archived: false, disabled: false, permissions: { push: true }, default_branch: 'main', name: 'private-sync', owner: { login: 'alice' } };
  else if (route === '/actions/secrets') data = { secrets: fixtureSnapshot.secrets.map(name => ({ name })) };
  else if (route === '/actions/variables') data = { variables: Object.entries(fixtureSnapshot.variables).map(([name, value]) => ({ name, value })) };
  else if (route === '/contents/.onelap-web.json') data = encodedFile({ project: 'onelap-garmin-web', schema_version: 1, version: '1.3.0', features: ['default-sync-targets', 'shared-run-settings'] });
  else if (route === '/contents/data/web-status.json') data = encodedFile(apiReport);
  else if (/^\/actions\/workflows\/[^/]+\/runs$/.test(route)) data = { workflow_runs: route.includes('/sync.yml/') ? fixtureRuns : [] };
  else if (/^\/actions\/runs\/(123|124)\/jobs$/.test(route)) data = { jobs: [{ steps: [{ name: '保存台账与汇总', status: 'completed', conclusion: route.includes('/124/') ? 'failure' : 'success' }] }] };
  else unexpectedRequests.push(`${method} ${route}`);
  if (data === undefined) return new Response('{}', { status: 404 });
  return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
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

// Exercise real connection and task handlers with offline API responses, without a browser.
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
assert.equal(document.querySelector('#demo-toggle'), null);
assert.equal(document.querySelector('.topbar-date'), null);
assert.doesNotMatch(document.body.textContent, /体验示例|虚构数据|个人私有实例|欢迎使用/);
assert.doesNotMatch(entrySource, /TEST-1025-|周末耐力骑行|示例模式|demo-toggle|体验示例|退出示例|界面演示|虚构数据|个人部署|PRIVATE AUTOMATION/);
const settle = async (predicate, label) => {
  for (let attempt = 0; attempt < 1000; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  throw new Error(`离线交互未完成：${label}；${document.querySelector('#activity-status')?.textContent}`);
};
const unlocked = () => !document.querySelector('#connection-form button').disabled;
document.querySelector('#github-owner').value = 'alice';
document.querySelector('#github-repo').value = 'private-sync';
document.querySelector('#github-token').value = 'test-offline-token';
document.querySelector('#connection-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
await settle(() => document.querySelector('#connection-pill').classList.contains('connected') && unlocked(), '连接仓库');
assert.equal(document.querySelector('#github-token').value, '', '连接后清空 Token 输入框。');
assert.equal(document.querySelector('#onelap-password').disabled, false);
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
await settle(() => document.querySelector('#force-dialog').open, '强制重传确认');
assert.match(document.querySelector('#force-dialog').textContent, /Garmin 中国区.*Garmin 国际区.*Strava/s);
assert.equal(document.querySelector('#force-submit').disabled, true);
assert.equal(dispatches.length, 0, '确认前不得提交工作流。');
clickText('返回调整');
await settle(unlocked, '取消强制重传');
assert.equal(dispatches.length, 0, '取消不得提交工作流。');
document.querySelector('#manual-submit').click();
await settle(() => document.querySelector('#force-dialog').open, '再次确认强制重传');
const acknowledgement = document.querySelector('#force-ack');
acknowledgement.checked = true; acknowledgement.dispatchEvent(new window.Event('change'));
assert.equal(document.querySelector('#force-submit').disabled, false);
document.querySelector('#force-submit').click();
await settle(() => dispatches.length === 1 && unlocked(), '提交强制重传');
assert.equal(document.querySelector('#force-dialog').open, false);
assert.equal(dispatches[0].inputs.record_id, fixtureReport.activity_items[0].id);
assert.equal(dispatches[0].inputs.force_reupload, 'true');
assert.equal(dispatches[0].inputs.target_regions, 'CN,GLOBAL');
assert.equal(dispatches[0].inputs.dry_run, 'false');
document.querySelector('#manual-drawer').close();
navigate('runs');
const reports = [...document.querySelectorAll('#page-content button')].filter(button => button.textContent.trim() === '查看报告');
assert.equal(reports.length, 2); reports[1].click();
await settle(() => /台账未保存/.test(document.querySelector('#page-content').textContent), '失败台账报告');
assert.ok(document.querySelector('.recovery-list'));
navigate('runs');
clickText('查看报告');
await settle(() => /台账已保存/.test(document.querySelector('#page-content').textContent), '成功台账报告');
assert.equal(document.querySelector('.recovery-list'), null, '成功运行无需显示失败恢复说明。');
navigate('reconcile');
assert.match(document.querySelector('#reconcile-results').textContent, /未核实 \/ 歧义/);
assert.equal(document.querySelector('#reconcile-targets').disabled, false);
navigate('settings');
document.querySelector('#settings-tab-processing').click();
assert.equal(document.querySelector('#settings-panel-processing').hidden, false);
assert.equal(document.querySelector('#settings-tab-processing').getAttribute('aria-selected'), 'true');
assert.equal(document.querySelector('#onelap-password'), credentialInput, '导航不应重建或复制凭据表单。');
clickText('首次配置向导');
assert.equal(document.querySelector('#page-title').textContent, '首次配置');
clickText('确认，继续 →'); clickText('确认，继续 →');
assert.ok(document.querySelector('#setup-activity'));
const setupActivity = document.querySelector('#setup-activity');
setupActivity.value = fixtureReport.activity_items[0].id; setupActivity.dispatchEvent(new window.Event('change'));
assert.match(document.querySelector('#setup-validation').textContent, /周末耐力/);
assert.equal([...document.querySelectorAll('#setup-validation button')].find(button => button.textContent.trim() === '执行单条真实上传').disabled, true);
assert.equal(document.querySelector('#setup-simulation-ack').disabled, true);
clickText('执行单条模拟');
assert.equal(document.querySelector('#sync-dry-run').checked, true);
document.querySelector('#manual-submit').click();
await settle(() => dispatches.length === 2 && unlocked(), '提交单条模拟');
assert.equal(dispatches[1].inputs.dry_run, 'true');
assert.equal(dispatches[1].inputs.force_reupload, 'false');
assert.equal(dispatches[1].inputs.record_id, fixtureReport.activity_items[0].id);
document.querySelector('#manual-drawer').close();
assert.equal(document.querySelector('#setup-simulation-ack').disabled, true, '仅提交任务不能视为模拟成功。');
apiReport.run = { ...apiReport.run, record_id: fixtureReport.activity_items[0].id, dry_run: true };
clickText('刷新');
await settle(() => unlocked() && !document.querySelector('#setup-simulation-ack').disabled, '读取模拟成功证据');
assert.match(document.querySelector('#setup-validation').textContent, /单条模拟成功/);
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
navigate('settings');
clickText('断开并清空本页凭据');
assert.doesNotMatch(document.querySelector('#page-content').textContent, /周末耐力/);
assert.equal(document.querySelector('#connection-pill').classList.contains('connected'), false);
navigate('activities');
assert.match(document.querySelector('#activity-results').textContent, /没有符合条件/);
assert.deepEqual(unexpectedRequests, [], '离线接口必须覆盖所有请求。');
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

console.log('Pages 生产包启动、真实连接、筛选、运行报告、模拟证据、强制确认、断开清理和嵌入保护检查通过（离线 API）。');
