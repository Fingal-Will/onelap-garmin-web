import { mountDashboard } from './dashboard.js';
import { GitHubClient } from './github.js';
import { authorizeGarmin, cleanupGarminOAuthSecrets } from './oauth.js';
import { adaptSettingsForRunner, buildSettings, requireSyncTargets, resolveSyncTargets } from './settings.js';
import ArrowLeft from 'lucide/dist/esm/icons/arrow-left.mjs';
import Activity from 'lucide/dist/esm/icons/activity.mjs';
import Check from 'lucide/dist/esm/icons/check.mjs';
import Clock from 'lucide/dist/esm/icons/clock.mjs';
import Cloud from 'lucide/dist/esm/icons/cloud.mjs';
import Download from 'lucide/dist/esm/icons/download.mjs';
import ExternalLink from 'lucide/dist/esm/icons/external-link.mjs';
import Key from 'lucide/dist/esm/icons/key.mjs';
import Lock from 'lucide/dist/esm/icons/lock.mjs';
import Play from 'lucide/dist/esm/icons/play.mjs';
import RefreshCw from 'lucide/dist/esm/icons/refresh-cw.mjs';
import Server from 'lucide/dist/esm/icons/server.mjs';
import Settings from 'lucide/dist/esm/icons/settings.mjs';
import ShieldCheck from 'lucide/dist/esm/icons/shield-check.mjs';
import Trash2 from 'lucide/dist/esm/icons/trash.mjs';
import TriangleAlert from 'lucide/dist/esm/icons/triangle-alert.mjs';
import Upload from 'lucide/dist/esm/icons/upload.mjs';
import X from 'lucide/dist/esm/icons/x.mjs';
import './styles.css';
import './session-help.css';
import './mobile.css';

const app = document.querySelector('#app');
if (!app) throw new Error('页面缺少应用入口。');
const embedded = window.self !== window.top;
let dashboard = null;

const state = {
  client: null,
  repository: null,
  snapshot: { secrets: [], variables: {}, installed: false },
  busy: false,
  polling: false,
  oauthRunning: false,
  oauthRegion: null,
};

function element(name, attributes = {}, children = []) {
  const node = document.createElement(name);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'checked') node.checked = Boolean(value);
    else if (key === 'disabled') node.disabled = Boolean(value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function icon(name, small = false) {
  const icons = {
    activity: Activity,
    back: ArrowLeft,
    check: Check,
    clock: Clock,
    cloud: Cloud,
    download: Download,
    external: ExternalLink,
    key: Key,
    lock: Lock,
    play: Play,
    refresh: RefreshCw,
    server: Server,
    settings: Settings,
    shield: ShieldCheck,
    trash: Trash2,
    upload: Upload,
    warning: TriangleAlert,
    close: X,
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', `icon${small ? ' icon-sm' : ''}`);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const createChild = ([tag, attributes, children]) => {
    const child = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [attribute, value] of Object.entries(attributes)) child.setAttribute(attribute, String(value));
    for (const nested of children || []) child.append(createChild(nested));
    return child;
  };
  for (const node of icons[name] || Activity) {
    svg.append(createChild(node));
  }
  return svg;
}

function button(label, options = {}) {
  return element('button', {
    id: options.id, type: options.type || 'button', class: `button${options.variant ? ` ${options.variant}` : ''}${options.small ? ' small' : ''}${options.block ? ' block' : ''}`,
    disabled: options.disabled, onClick: options.onClick, title: options.title, 'aria-label': options.ariaLabel,
  }, [options.icon ? icon(options.icon, options.small) : null, element('span', { text: label })]);
}

function notice(kind, title, message) {
  return element('div', { class: `notice ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, [
    icon(kind === 'error' ? 'warning' : kind === 'success' ? 'check' : 'shield', true),
    element('div', {}, [element('strong', { text: title }), element('p', { text: message })]),
  ]);
}

function makeField({ id, label, help, type = 'text', value = '', placeholder = '', required = false, options, rows, autocomplete = 'off' }) {
  const labelNode = element('label', { for: id, text: label });
  const descriptionId = help ? `${id}-help` : undefined;
  let input;
  if (options) {
    input = element('select', { id, name: id, 'aria-describedby': descriptionId });
    for (const option of options) input.append(element('option', { value: option.value, text: option.label, selected: option.value === value }));
  } else if (rows) {
    input = element('textarea', { id, name: id, rows, placeholder, 'aria-describedby': descriptionId, autocomplete });
    input.value = value;
  } else {
    input = element('input', { id, name: id, type, value, placeholder, required, autocomplete, 'aria-describedby': descriptionId });
  }
  return element('div', { class: 'field' }, [
    element('div', { class: 'field-heading' }, [labelNode, required ? element('span', { class: 'required-note', text: '必填' }) : null]),
    input,
    help ? element('small', { class: 'helper', id: descriptionId, text: help }) : null,
  ]);
}

function checkField(id, title, copy, checked = false) {
  const input = element('input', { type: 'checkbox', id, name: id, checked });
  return element('label', { class: 'check-field', for: id }, [input, element('span', {}, [element('span', { text: title }), element('small', { text: copy })])]);
}

function card(id, number, title, description, body, badge) {
  return element('section', { class: 'card', id, 'aria-labelledby': `${id}-title` }, [
    element('div', { class: 'card-header' }, [
      element('div', { class: 'card-title' }, [element('span', { class: 'step-number', text: number }), element('div', {}, [
        element('h2', { id: `${id}-title`, text: title }), element('p', { class: 'card-description', text: description }),
      ])]),
      element('span', { class: `badge${badge?.success ? ' success' : ''}`, id: `${id}-badge`, text: badge?.text || '等待连接' }),
    ]),
    element('div', { class: 'card-body' }, body),
  ]);
}

function get(id) { return document.querySelector(`#${id}`); }

function setText(id, value) { const node = get(id); if (node) node.textContent = value; }

function setBadge(id, value, success = false) {
  const node = get(`${id}-badge`);
  if (node) { node.textContent = value; node.classList.toggle('success', success); }
}

function setBusy(active, message = '') {
  state.busy = active;
  document.body.classList.toggle('busy', active);
  for (const node of document.querySelectorAll('[data-requires-connection]')) node.disabled = active || !state.client;
  for (const node of document.querySelectorAll('[data-busy-lock]')) node.disabled = active;
  dashboard?.lock();
  setText('activity-status', message || (state.client ? '已连接，等待操作。' : '连接后可安装、保存并运行任务。'));
}

function toast(message, kind = 'success') {
  const old = document.querySelector('.toast-region');
  if (old) old.remove();
  const node = element('div', { class: `toast-region${kind === 'error' ? ' error' : ''}`, role: 'status', text: message });
  document.body.append(node);
  window.setTimeout(() => node.remove(), 6000);
}

function errorMessage(error) {
  const message = error instanceof Error ? error.message : '操作未完成，请稍后重试。';
  return error?.cleanupError instanceof Error ? `${message} ${error.cleanupError.message}` : message;
}

function requestId(prefix) {
  const bytes = new Uint32Array(1);
  globalThis.crypto?.getRandomValues?.(bytes);
  return `${prefix}-${Date.now().toString(36)}-${(bytes[0] || Math.floor(Math.random() * 2 ** 32)).toString(36)}`;
}

function safeGithubUrl(value) {
  try { const url = new URL(String(value)); return url.protocol === 'https:' && url.hostname === 'github.com' ? url.href : ''; } catch { return ''; }
}

async function runTask(message, task) {
  if (!state.client || state.busy) return;
  try { setBusy(true, message); return await task(); }
  catch (error) { const messageText = errorMessage(error); setText('activity-status', messageText); toast(messageText, 'error'); }
  finally { setBusy(false); }
}

function collectSettings() {
  return {
    onelapAccount: get('onelap-account').value,
    onelapPassword: get('onelap-password').value,
    onelapToken: get('onelap-token').value,
    garminCnSession: get('garmin-cn-session').value,
    garminGlobalSession: get('garmin-global-session').value,
    unitId: get('unit-id').value,
    productId: get('product-id').value,
    softwareVersion: get('software-version').value,
    targets: get('settings-targets').value,
    coordinateTransform: get('coordinate-transform').checked,
    sessionKey: get('session-key').value,
    stravaEnabled: get('strava-enabled').checked,
    stravaClientId: get('strava-client-id').value,
    stravaClientSecret: get('strava-client-secret').value,
    stravaRefreshToken: get('strava-refresh-token').value,
    stravaSessionKey: get('strava-session-key').value,
    discoveryPages: get('default-pages').value,
    recentLimit: get('default-recent').value,
    maxRetries: get('max-retries').value,
    globalDryRun: get('global-dry-run').checked,
  };
}

function credentialSummary() {
  const names = new Set(state.snapshot.secrets || []);
  const expected = ['ONELAP_ACCOUNT', 'ONELAP_PASSWORD', 'ONELAP_TOKEN', 'GARMIN_CN_OAUTH1', 'GARMIN_CN_OAUTH2', 'GARMIN_GLOBAL_OAUTH1', 'GARMIN_GLOBAL_OAUTH2', 'GARMIN_DEVICE_UNIT_ID', 'STRAVA_CLIENT_ID', 'STRAVA_CLIENT_SECRET', 'STRAVA_REFRESH_TOKEN', 'STRAVA_SESSION_KEY'];
  const present = expected.filter((name) => names.has(name));
  setText('credential-status', present.length ? `已保存 ${present.length} 项凭据。` : '尚未保存凭据。');
  const container = get('credential-list');
  container.replaceChildren(...present.map((name) => element('span', { class: 'secret-pill', text: name })));
  const temporary = [...names].filter((name) => /^GARMIN_OAUTH_[A-F0-9]{32}_(?:USERNAME|PASSWORD|RESULT_KEY)$/.test(name));
  setText('oauth-cleanup-status', temporary.length
    ? `检测到 ${temporary.length} 项未清理的临时授权数据。`
    : '没有检测到残留临时授权数据。');
}

function applySnapshot(snapshot) {
  state.snapshot = snapshot;
  state.repository = snapshot.repository;
  const vars = snapshot.variables || {};
  get('settings-targets').value = vars.SYNC_TARGETS || 'AUTO';
  get('default-pages').value = vars.ONELAP_DISCOVERY_PAGES || '10';
  get('default-recent').value = vars.RECONCILE_RECENT_LIMIT || '20';
  get('max-retries').value = vars.SYNC_MAX_RETRIES || '8';
  get('global-dry-run').checked = vars.SYNC_DRY_RUN === 'true';
  get('strava-enabled').checked = vars.STRAVA_SYNC_ENABLED === 'true';
  get('sync-pages').value = 'DEFAULT';
  get('reconcile-recent').value = 'DEFAULT';
  get('sync-targets').value = 'DEFAULT';
  get('reconcile-targets').value = 'DEFAULT';
  updateTargetSummary();
  get('product-id').value = vars.GARMIN_DEVICE_PRODUCT_ID || '';
  get('product-preset').value = ['3122', '4440', '3843', '4062', '4061'].includes(vars.GARMIN_DEVICE_PRODUCT_ID) ? vars.GARMIN_DEVICE_PRODUCT_ID : 'custom';
  get('software-version').value = vars.GARMIN_DEVICE_SOFTWARE_VERSION || '';
  get('coordinate-transform').checked = vars.FIT_COORDINATE_TRANSFORM_ENABLED === 'true';
  get('schedule-enabled').checked = vars.SYNC_ENABLED === 'true';
  setBadge('install', snapshot.installed ? '已安装' : '待安装', snapshot.installed);
  setBadge('settings', snapshot.installed ? '可保存' : '先安装程序', snapshot.installed);
  setBadge('run', snapshot.installed ? '可运行' : '先安装程序', snapshot.installed);
  setText('repository-value', `${snapshot.repository.owner?.login || state.client.owner}/${snapshot.repository.name}`);
  setText('installation-value', snapshot.installed ? `已安装 ${snapshot.manifest?.version || '同步程序'}` : '尚未安装');
  setText('schedule-value', vars.SYNC_ENABLED === 'true' ? '约每 30 分钟自动同步' : '自动同步未开启');
  setText('connection-value', 'GitHub 已验证');
  get('connection-pill').classList.add('connected');
  get('connection-pill').querySelector('.pill-label').textContent = '已连接私有仓库';
  credentialSummary();
  dashboard?.update();
}

async function refreshRepository({ quiet = false } = {}) {
  const snapshot = await state.client.inspectRepository();
  applySnapshot(snapshot);
  if (!quiet) toast('仓库状态已刷新。');
  return snapshot;
}

async function connect() {
  if (state.busy) return;
  const token = get('github-token').value;
  const owner = get('github-owner').value;
  const repo = get('github-repo').value;
  try {
    setBusy(true, '正在验证 GitHub 私有仓库…');
    const client = new GitHubClient({ token, owner, repo });
    const snapshot = await client.inspectRepository();
    if (state.client) state.client.disconnect();
    state.client = client;
    applySnapshot(snapshot);
    get('github-token').value = '';
    toast('已连接到私有仓库。Token 仅保留在本页内存中。');
    try { await loadRuns(); } catch { /* A report read failure does not undo connection. */ }
  } catch (error) {
    const message = errorMessage(error);
    setText('connection-status', message);
    toast(message, 'error');
  } finally { setBusy(false); }
}

function disconnect() {
  state.client?.disconnect();
  state.client = null;
  state.repository = null;
  state.snapshot = { secrets: [], variables: {}, installed: false };
  get('github-token').value = '';
  get('onelap-account').value = '';
  for (const id of ['strava-client-id', 'strava-client-secret', 'strava-refresh-token', 'strava-session-key']) get(id).value = '';
  get('strava-enabled').checked = false;
  get('onelap-password').value = '';
  get('onelap-token').value = '';
  get('session-key').value = '';
  get('garmin-cn-session').value = '';
  get('garmin-global-session').value = '';
  get('unit-id').value = '';
  clearGarminOAuthForm();
  state.oauthRunning = false;
  state.oauthRegion = null;
  const oauthDialog = get('garmin-oauth-dialog');
  if (oauthDialog?.open) oauthDialog.close();
  get('connection-pill').classList.remove('connected');
  get('connection-pill').querySelector('.pill-label').textContent = '未连接';
  setText('connection-value', '等待连接');
  setText('repository-value', '尚未选择仓库');
  setText('installation-value', '尚未安装');
  setText('schedule-value', '自动同步未开启');
  setText('credential-status', '连接已断开；本页输入的凭据已清空。');
  setText('oauth-cleanup-status', '连接仓库后可检查。');
  setText('default-target-status', '默认按已配置账号自动判断；中国区和国际区可以独立使用。');
  get('credential-list').replaceChildren();
  setBadge('install', '等待连接'); setBadge('settings', '等待连接'); setBadge('run', '等待连接');
  renderRuns([]);
  dashboard?.reset();
  setBusy(false);
  toast('已断开连接，并清除了本页中的凭据输入。');
}

async function installRunner() {
  await runTask('正在读取并校验安装包…', async () => {
    if (state.snapshot.installed) throw new Error('同步程序已经安装。为避免覆盖，请在仓库中备份并检查升级内容。');
    const response = await fetch(new URL('./runner-template.json', document.baseURI), { cache: 'no-store', credentials: 'omit' });
    if (!response.ok) throw new Error('无法加载同步程序安装包，请重新构建网页后重试。');
    const bundle = await response.json();
    const url = await state.client.installRunner(bundle, (progress) => setText('activity-status', progress));
    const safeUrl = safeGithubUrl(url);
    setText('install-status', safeUrl ? '同步程序已发布到私有仓库。' : '同步程序已发布到私有仓库。');
    await refreshRepository({ quiet: true });
    toast('同步程序安装完成。下一步请保存运行设置。');
  });
}

async function saveSettings() {
  await runTask('正在验证并加密保存设置…', async () => {
    if (!state.snapshot.installed) throw new Error('请先安装同步程序，再保存设置。');
    const update = adaptSettingsForRunner(buildSettings(collectSettings(), state.snapshot), state.snapshot.manifest);
    const legacyAuto = update.variables.SYNC_TARGETS === 'AUTO'
      && !state.snapshot.manifest?.features?.includes('default-sync-targets');
    if (legacyAuto) {
      update.variables.SYNC_TARGETS = requireSyncTargets('AUTO', [
        ...state.snapshot.secrets, ...Object.keys(update.secrets),
      ]);
    }
    const result = await state.client.saveSettings(update, (progress) => setText('activity-status', progress));
    dashboard?.invalidateValidation();
    get('onelap-account').value = '';
    for (const id of ['strava-client-id', 'strava-client-secret', 'strava-refresh-token', 'strava-session-key']) get(id).value = '';
    get('onelap-password').value = '';
    get('onelap-token').value = '';
    get('session-key').value = '';
    get('garmin-cn-session').value = '';
    get('garmin-global-session').value = '';
    await refreshRepository({ quiet: true });
    setText('settings-status', legacyAuto
      ? `已保存 ${result.saved.length} 项设置。当前私有仓库为旧版同步程序，自动判断已按现有会话保存为固定区域；更新同步程序后才能动态判断。`
      : `已保存 ${result.saved.length} 项设置。凭据输入已从本页清除。`);
    toast('设置已保存到 GitHub Actions。');
  });
}

function updateTargetSummary() {
  const strategy = state.snapshot.variables.SYNC_TARGETS || 'AUTO';
  const targets = resolveSyncTargets(strategy, state.snapshot.secrets);
  const label = { CN: '仅中国区', GLOBAL: '仅国际区', 'CN,GLOBAL': '中国区 + 国际区' }[targets];
  setText('default-target-status', label
    ? `已保存的默认同步：${label}${strategy === 'AUTO' ? '（按已配置账号自动判断）' : ''}。未选择的区域无需配置。`
    : '默认按已配置账号自动判断；请至少配置一个区域的完整会话。');
}

function selectedRunTargets(id) {
  const selection = get(id).value;
  const strategy = selection === 'DEFAULT' ? state.snapshot.variables.SYNC_TARGETS || 'AUTO' : selection;
  return requireSyncTargets(strategy, state.snapshot.secrets);
}

function selectedRunLimit(id, variable, fallback) {
  const value = get(id).value;
  if (value !== 'DEFAULT' || state.snapshot.manifest?.features?.includes('shared-run-settings')) return value;
  return state.snapshot.variables[variable] || fallback;
}

function syncInputs() {
  const recordId = get('sync-record-id').value.trim();
  const force = get('sync-force').checked;
  if (force && !recordId) throw new Error('重新上传必须填写活动编号。');
  return {
    target_regions: selectedRunTargets('sync-targets'),
    discovery_pages: selectedRunLimit('sync-pages', 'ONELAP_DISCOVERY_PAGES', '10'),
    full_scan: get('sync-full-scan').checked,
    retry_auth: get('sync-retry-auth').checked,
    record_id: recordId,
    force_reupload: force,
    dry_run: get('sync-dry-run').checked,
    request_id: requestId('sync'),
  };
}

async function dispatchSync(validation = false) {
  await runTask(validation ? '正在提交配置验证任务…' : '正在提交同步任务…', async () => {
    if (!state.snapshot.installed) throw new Error('请先安装同步程序，再运行任务。');
    const inputs = syncInputs();
    if (validation) { inputs.dry_run = true; inputs.discovery_pages = '1'; inputs.full_scan = false; inputs.force_reupload = false; inputs.record_id = ''; inputs.request_id = requestId('validate'); }
    if (!validation && inputs.force_reupload && !await dashboard.confirmForce(inputs)) {
      setText('run-status', '已取消重新上传，本次没有提交任务。');
      return;
    }
    await state.client.dispatchWorkflow('sync.yml', inputs);
    setText('run-status', validation ? '验证任务已提交：检查一页活动，不上传到目标平台。' : '同步任务已提交，GitHub Actions 将在队列中执行。');
    try { await loadRuns(); } catch { /* Dispatch already succeeded; GitHub may not list the new run immediately. */ }
    toast(validation ? '配置验证任务已提交。' : '同步任务已提交。');
  });
}

async function dispatchReconcile() {
  await runTask('正在提交核对任务…', async () => {
    if (!state.snapshot.installed) throw new Error('请先安装同步程序，再运行任务。');
    if (!get('reconcile-dry-run').checked && state.snapshot.variables.SYNC_DRY_RUN !== 'true'
      && !window.confirm('核对后将对明确缺失的 Garmin 目标补传。未能核实和匹配歧义不会补传。确认继续？')) return;
    await state.client.dispatchWorkflow('reconcile.yml', {
      target_regions: selectedRunTargets('reconcile-targets'),
      recent_activities: selectedRunLimit('reconcile-recent', 'RECONCILE_RECENT_LIMIT', '20'),
      dry_run: get('reconcile-dry-run').checked,
      request_id: requestId('reconcile'),
    });
    setText('run-status', '核对任务已提交，结果会写入私有台账。');
    try { await loadRuns(); } catch { /* Dispatch already succeeded; GitHub may not list the new run immediately. */ }
    toast('核对任务已提交。');
  });
}

async function saveSchedule() {
  await runTask('正在更新自动同步开关…', async () => {
    if (!state.snapshot.installed) throw new Error('请先安装同步程序，再调整自动同步。');
    const enabled = get('schedule-enabled').checked;
    if (enabled) requireSyncTargets(state.snapshot.variables.SYNC_TARGETS || 'AUTO', state.snapshot.secrets);
    await state.client.setScheduleEnabled(enabled);
    await refreshRepository({ quiet: true });
    setText('schedule-status', enabled ? '自动同步已开启：每小时第 7 和 37 分钟触发。' : '自动同步已关闭。');
    toast(enabled ? '自动同步已开启。' : '自动同步已关闭。');
  });
}

function runIcon(run) {
  const status = String(run.status || '').toLowerCase();
  if (status === 'completed' && run.conclusion === 'success') return ['success', 'check'];
  if (status === 'completed') return ['failure', 'warning'];
  return ['running', 'clock'];
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
}

function renderRuns(runs) {
  const list = get('run-list');
  list.replaceChildren();
  if (!runs.length) {
    list.append(element('li', { class: 'empty-state' }, [icon('clock'), element('p', { text: '暂无运行记录' }), element('small', { text: state.client ? '提交任务后可在这里查看状态。' : '连接私有仓库后加载记录。' })]));
    return;
  }
  for (const run of runs) {
    const [style, symbol] = runIcon(run);
    const name = typeof run.name === 'string' && run.name ? run.name : (run.path?.includes('reconcile') ? '核对并补同步' : '顽鹿同步');
    const stateText = String(run.status || '') === 'completed' ? (run.conclusion === 'success' ? '已完成' : run.conclusion === 'cancelled' ? '已取消' : '失败') : '进行中';
    const main = element('div', {}, [element('span', { class: 'run-title', text: name }), element('span', { class: 'run-meta', text: `${formatTime(run.created_at)} · #${String(run.run_number || '—')}` })]);
    const url = safeGithubUrl(run.html_url);
    const content = url ? element('a', { href: url, target: '_blank', rel: 'noopener noreferrer', 'aria-label': `在 GitHub 中打开${name}运行记录` }, [main]) : main;
    list.append(element('li', { class: 'run-item' }, [element('span', { class: `run-icon ${style}` }, icon(symbol, true)), content, element('span', { class: 'run-state', text: stateText })]));
  }
}

async function refreshRuns({ quiet = false } = {}) {
  await runTask('正在读取 Actions 运行记录…', async () => {
    await loadRuns();
    if (!quiet) toast('运行记录已刷新。');
  });
}

async function loadRuns() {
  const client = state.client;
  if (!client) return;
  const results = await Promise.allSettled([client.listRuns(), client.readWebReport()]);
  if (client !== state.client) return;
  if (results[0].status === 'fulfilled') {
    const runs = results[0].value;
    renderRuns(runs);
    dashboard?.setRuns(runs);
    setText('run-status', runs.length ? `已加载最近 ${runs.length} 条运行记录。` : '暂未找到同步工作流运行记录。');
  }
  if (results[1].status === 'fulfilled') dashboard?.setReport(results[1].value);
  else dashboard?.setReport(null, results[1].reason?.message || '读取失败，请稍后重试。');
  if (results[0].status === 'rejected') throw results[0].reason;
}

async function pollRuns() {
  if (!state.client || state.busy || state.polling || document.visibilityState !== 'visible') return;
  state.polling = true;
  try { await loadRuns(); } catch { /* Background polling never interrupts an active workflow. */ }
  finally { state.polling = false; }
}

function importSession(fileInput, textArea) {
  const file = fileInput.files?.[0];
  if (!file) return;
  if (file.size > 128 * 1024) { toast('会话文件超过 128 KB，未导入。', 'error'); fileInput.value = ''; return; }
  const reader = new FileReader();
  reader.addEventListener('load', () => {
    if (typeof reader.result === 'string') { textArea.value = reader.result; toast('会话已导入到当前页面，保存前不会发送。'); }
    fileInput.value = '';
  }, { once: true });
  reader.addEventListener('error', () => { toast('无法读取该会话文件。', 'error'); fileInput.value = ''; }, { once: true });
  reader.readAsText(file, 'utf-8');
}

function garminRegionLabel(region) {
  return region === 'CN' ? '中国区' : '国际区';
}

function clearGarminOAuthForm() {
  const username = get('garmin-oauth-username');
  const password = get('garmin-oauth-password');
  if (username) username.value = '';
  if (password) password.value = '';
}

function setGarminOAuthFormDisabled(disabled) {
  for (const id of ['garmin-oauth-username', 'garmin-oauth-password', 'garmin-oauth-submit']) {
    const field = get(id);
    if (field) field.disabled = disabled;
  }
}

function openGarminOAuthDialog(region) {
  if (!state.client) { toast('请先连接私有仓库。', 'error'); return; }
  if (!state.snapshot.installed) { toast('请先安装同步程序。', 'error'); return; }
  if (state.busy || state.oauthRunning) return;
  state.oauthRegion = region;
  clearGarminOAuthForm();
  setGarminOAuthFormDisabled(false);
  setText('garmin-oauth-title', `获取 Garmin ${garminRegionLabel(region)}授权`);
  setText('garmin-oauth-copy', `凭据将由私有 GitHub Actions 登录 Garmin ${garminRegionLabel(region)}，获取结果会直接加密保存。`);
  setText('garmin-oauth-status', '等待提交。');
  const dialog = get('garmin-oauth-dialog');
  if (typeof dialog?.showModal !== 'function') {
    toast('当前浏览器不支持授权对话框，请更新浏览器或使用会话文件导入。', 'error');
    return;
  }
  dialog.showModal();
  get('garmin-oauth-username').focus();
}

function closeGarminOAuthDialog() {
  if (state.oauthRunning) return;
  clearGarminOAuthForm();
  const dialog = get('garmin-oauth-dialog');
  if (dialog?.open) dialog.close();
}

async function submitGarminOAuth(event) {
  event.preventDefault();
  if (!state.client || state.busy || state.oauthRunning || !state.oauthRegion) return;
  const region = state.oauthRegion;
  let username = get('garmin-oauth-username').value;
  let password = get('garmin-oauth-password').value;
  try {
    state.oauthRunning = true;
    setBusy(true, `正在获取 Garmin ${garminRegionLabel(region)}授权…`);
    get('garmin-oauth-close').disabled = true;
    get('garmin-oauth-cancel').disabled = true;
    setGarminOAuthFormDisabled(true);
    setText('garmin-oauth-status', '正在加密提交临时登录凭据…');
    const authorization = authorizeGarmin({
      client: state.client,
      region,
      username,
      password,
      onProgress: (progress) => {
        setText('garmin-oauth-status', progress);
        setText('activity-status', progress);
      },
    });
    clearGarminOAuthForm();
    username = '';
    password = '';
    const result = await authorization;
    let message = `Garmin ${garminRegionLabel(region)}授权已保存。`;
    let warning = result.cleanupWarning;
    if (warning) message += ' 临时数据清理不完整，请稍后使用清理功能。';
    try {
      await refreshRepository({ quiet: true });
    } catch {
      warning = warning || 'refresh_failed';
      message += ' 仓库状态暂时无法刷新，可稍后重新连接查看。';
    }
    setText(`garmin-${region === 'CN' ? 'cn' : 'global'}-oauth-status`, warning
      ? 'OAuth1 与 OAuth2 已保存；请检查授权临时数据。'
      : 'OAuth1 与 OAuth2 已加密保存。');
    setText('garmin-oauth-status', message);
    toast(message, warning ? 'error' : 'success');
  } catch (error) {
    const message = errorMessage(error);
    setText('garmin-oauth-status', message);
    setText('activity-status', message);
    toast(message, 'error');
  } finally {
    clearGarminOAuthForm();
    state.oauthRunning = false;
    setGarminOAuthFormDisabled(false);
    get('garmin-oauth-close').disabled = false;
    get('garmin-oauth-cancel').disabled = false;
    setBusy(false);
  }
}

async function cleanupGarminOAuth() {
  if (!window.confirm('将清理已结束或超过 30 分钟仍未运行的 Garmin 授权临时数据。正在排队或运行的任务会保留。是否继续？')) return;
  await runTask('正在清理 Garmin 授权临时数据…', async () => {
    const result = await cleanupGarminOAuthSecrets(state.client);
    const removed = Array.isArray(result)
      ? result.length
      : Number(result?.secrets?.length || 0) + Number(result?.refs?.length || 0);
    const protectedCount = Number(result?.protectedRequestIds?.length || 0);
    await refreshRepository({ quiet: true });
    const protectedText = protectedCount ? ` 已保留 ${protectedCount} 个正在处理或新创建的请求。` : '';
    setText('oauth-cleanup-status', `${removed ? `已清理 ${removed} 项临时数据。` : '没有可清理的残留临时数据。'}${protectedText}`);
    toast(removed ? 'Garmin 授权临时数据已清理。' : `没有可清理的残留临时数据。${protectedText}`);
  });
}

function buildInterface() {
  const connectionPill = element('div', { class: 'connection-pill', id: 'connection-pill' }, [element('span', { class: 'dot' }), element('span', { class: 'pill-label', text: '未连接' })]);

  const tokenField = makeField({ id: 'github-token', label: 'GitHub 访问令牌（Fine-grained Token）', type: 'password', placeholder: 'github_pat_…', help: 'Token 仅保留在本页内存。', required: true, autocomplete: 'off' });
  tokenField.querySelector('.helper').replaceChildren(
    document.createTextNode('此仓库 Token 需要 Contents、Actions、Secrets、Variables 与 Workflows 的读写权限。'),
    document.createTextNode(' '),
    element('a', { href: 'https://github.com/settings/personal-access-tokens/new', target: '_blank', rel: 'noopener noreferrer', text: '创建 Fine-grained PAT' }),
  );
  const connectionBody = [
    element('form', { id: 'connection-form', noValidate: true, onSubmit: (event) => { event.preventDefault(); connect(); } }, [
      element('div', { class: 'form-grid' }, [
        makeField({ id: 'github-owner', label: 'GitHub 用户名或组织', placeholder: '例如：your-account', help: '需对目标私有仓库拥有写入权限。', required: true, autocomplete: 'username' }),
        makeField({ id: 'github-repo', label: '专用私有仓库名', placeholder: '例如：onelap-garmin-sync', help: '安装前仅允许空仓库或只含 README、LICENSE、.gitignore。', required: true }),
        tokenField,
        element('div', { class: 'field' }, [element('div', { class: 'field-heading' }, [element('label', { text: '连接状态' })]), element('p', { class: 'status-message', id: 'connection-status', text: '选择自己的专用私有仓库后连接。' })]),
      ]),
      element('div', { class: 'actions-row' }, [button('连接并检查', { type: 'submit', variant: 'primary', icon: 'key' }), button('断开并清空本页凭据', { variant: 'ghost', icon: 'refresh', onClick: disconnect })]),
    ]),
    notice('warning', '请使用专用私有仓库', '安装会提交文件，请使用空仓库或仅包含 README、LICENSE、.gitignore 的仓库。'),
  ];

  const installBody = [
    element('p', { class: 'helper', text: '安装内容包括同步工作流与运行脚本，不含账号或活动数据。' }),
    element('div', { class: 'actions-row' }, [button('安装同步程序', { variant: 'primary', icon: 'download', onClick: installRunner, disabled: true }), element('span', { class: 'status-message', id: 'install-status', text: '连接仓库后可检查安装状态。' })]),
  ];

  const cnInput = makeField({ id: 'garmin-cn-session', label: 'Garmin 中国区会话 JSON', rows: 4, placeholder: '导入 Garmin 中国区 session JSON…', help: '仅启用该区域时需要。未启用可留空；已保存的会话不会被删除。' });
  const cnFile = element('input', { class: 'file-input', type: 'file', accept: 'application/json,.json', 'aria-label': '导入 Garmin 中国区会话文件' });
  cnFile.addEventListener('change', () => importSession(cnFile, cnInput.querySelector('textarea')));
  cnInput.append(cnFile, element('div', { class: 'oauth-field-actions' }, [
    button('自动获取', { variant: 'subtle', icon: 'key', onClick: () => openGarminOAuthDialog('CN'), disabled: true }),
    element('span', { class: 'session-status', id: 'garmin-cn-oauth-status', text: '可使用 Garmin 中国区账号登录。' }),
  ]));
  const globalInput = makeField({ id: 'garmin-global-session', label: 'Garmin 国际区会话 JSON', rows: 4, placeholder: '导入 Garmin 国际区 session JSON…', help: '仅启用该区域时需要。未启用可留空；已保存的会话不会被删除。' });
  const globalFile = element('input', { class: 'file-input', type: 'file', accept: 'application/json,.json', 'aria-label': '导入 Garmin 国际区会话文件' });
  globalFile.addEventListener('change', () => importSession(globalFile, globalInput.querySelector('textarea')));
  globalInput.append(globalFile, element('div', { class: 'oauth-field-actions' }, [
    button('自动获取', { variant: 'subtle', icon: 'key', onClick: () => openGarminOAuthDialog('GLOBAL'), disabled: true }),
    element('span', { class: 'session-status', id: 'garmin-global-oauth-status', text: '可使用 Garmin 国际区账号登录。' }),
  ]));

  const productField = makeField({ id: 'product-id', label: 'Garmin 设备 Product ID', type: 'number', placeholder: '请输入自己的设备 Product ID', help: '需与自己的真实设备相匹配。', required: true });
  const productPreset = makeField({ id: 'product-preset', label: '设备型号预设', value: 'custom', options: [
    { value: 'custom', label: '自定义 Product ID' },
    { value: '3122', label: 'Edge 830 (3122)' },
    { value: '4440', label: 'Edge 1050 (4440)' },
    { value: '3843', label: 'Edge 1040 (3843)' },
    { value: '4062', label: 'Edge 840 (4062)' },
    { value: '4061', label: 'Edge 540 (4061)' },
  ], help: '选择预设会填入下方数值；请只选择自己实际拥有的设备。' });
  productPreset.querySelector('select').addEventListener('change', (event) => {
    if (event.target.value !== 'custom') productField.querySelector('input').value = event.target.value;
  });
  productField.querySelector('input').addEventListener('input', () => {
    const value = productField.querySelector('input').value;
    productPreset.querySelector('select').value = ['3122', '4440', '3843', '4062', '4061'].includes(value) ? value : 'custom';
  });
  const sessionHelp = element('details', { class: 'session-help' }, [
    element('summary', { text: '登录需要验证时使用本机会话' }),
    element('p', { text: '如果 Garmin 要求验证码、双因素验证或设备确认，请在已安装私有仓库的本机副本中复制环境变量示例，只在本机 .env 填写对应区域的 Garmin 用户名和密码，然后执行：' }),
    element('pre', {}, element('code', { text: 'npm ci\nnpm run session:cn\nnpm run session:global' })),
    element('p', { text: '生成文件位于 data/garmin-cn.session.json 和 data/garmin-global.session.json。按所选区域导入；不要提交 .env 或会话文件。' }),
  ]);
  const settingsBody = [
    sessionHelp,
    element('div', { class: 'form-grid' }, [
      makeField({ id: 'onelap-account', label: '顽鹿账号', placeholder: '账号或手机号', help: '可与密码搭配，或改用下方 Token。已保存凭据可留空。' }),
      makeField({ id: 'onelap-password', label: '顽鹿密码', type: 'password', placeholder: '输入顽鹿密码', help: '保存完成后会从本页清除。', autocomplete: 'new-password' }),
      makeField({ id: 'onelap-token', label: '顽鹿 Token（可选）', type: 'password', placeholder: '优先于账号密码', help: '可替代账号密码；不会回显或持久保存到网页。', autocomplete: 'off' }),
      makeField({ id: 'settings-targets', label: '默认同步区域', value: 'AUTO', options: [{ value: 'AUTO', label: '按已配置账号自动判断' }, { value: 'GLOBAL', label: '仅国际区' }, { value: 'CN', label: '仅中国区' }, { value: 'CN,GLOBAL', label: '中国区 + 国际区' }], help: '保存后用于自动同步和默认手动任务。只用国际区时选“仅国际区”，中国区会话可留空；修改后请保存。' }),
      makeField({ id: 'default-pages', label: '默认发现页数', type: 'number', value: '10', help: '0–100；0 表示不限页数。用于自动同步和默认手动任务。' }),
      makeField({ id: 'default-recent', label: '默认核对条数', type: 'number', value: '20', help: '1–200；核对按时间从新到旧选择活动。' }),
      makeField({ id: 'max-retries', label: '最大重试次数', type: 'number', value: '8', help: '1–100；更新授权后可选择“授权重试”。' }),
      element('div', { class: 'field' }, [checkField('global-dry-run', '全局模拟运行', '开启后手动和定时任务都只验证；单次任务无法关闭此设置。')]),
      cnInput, globalInput,
      makeField({ id: 'unit-id', label: 'Garmin 设备 Unit ID', type: 'number', placeholder: '1000000000 - 4294967295', help: '使用自己真实设备的 Unit ID。该值作为 Secret 加密保存。', required: true, autocomplete: 'off' }),
      productPreset, productField,
      makeField({ id: 'software-version', label: '设备固件版本（可选）', type: 'number', placeholder: '0 - 65535', help: '不填时仍可保存。' }),
      element('div', { class: 'field' }, [checkField('coordinate-transform', '启用轨迹坐标纠正', '将中国区 GCJ-02 轨迹转为 WGS84 后写入 FIT 文件。')]),
      makeField({ id: 'session-key', label: '顽鹿会话加密密钥（可选）', type: 'password', placeholder: '留空时首次保存自动生成', help: '至少 32 字节。仅当你需要自备密钥时填写。', autocomplete: 'off' }),
    ]),
    element('details', { class: 'session-help' }, [
      element('summary', { text: 'Strava 同步（可选）' }),
      checkField('strava-enabled', '同时同步到 Strava', '默认关闭。上传前批量核对已有活动；读取授权不足时保留待确认任务，并继续 Garmin 同步。'),
      element('p', { text: '在已安装同步程序的私有仓库本机副本中运行 python -m onelap_garmin_sync.strava_auth，完成 activity:read_all 和 activity:write 授权后填写。' }),
      element('div', { class: 'form-grid' }, [
        makeField({ id: 'strava-client-id', label: 'Strava Client ID', type: 'password', help: '已保存时留空。' }),
        makeField({ id: 'strava-client-secret', label: 'Strava Client Secret', type: 'password', help: '通过 GitHub 公钥加密后保存。' }),
        makeField({ id: 'strava-refresh-token', label: 'Strava Refresh Token', type: 'password', help: '重新授权时仅替换此值，保留原会话密钥和台账。' }),
        makeField({ id: 'strava-session-key', label: 'Strava 会话加密密钥（可选）', type: 'password', help: '至少 32 字节；首次开启自动生成，已保存时留空。' }),
      ]),
      element('p', { text: 'Strava 的核对在同步任务中自动执行。' }),
    ]),
    element('p', { class: 'session-status', id: 'default-target-status' , text: '默认按已配置账号自动判断；中国区和国际区可以独立使用。' }),
    element('div', { class: 'check-panel' }, [element('strong', { text: '已保存凭据状态' }), element('p', { class: 'session-status', id: 'credential-status', text: '连接仓库后读取 Secret 名称，不读取 Secret 内容。' }), element('div', { class: 'secret-list', id: 'credential-list' })]),
    element('div', { class: 'actions-row' }, [button('加密保存设置', { variant: 'primary', icon: 'lock', onClick: saveSettings, disabled: true }), button('验证已保存配置', { variant: 'subtle', icon: 'shield', onClick: () => dispatchSync(true), disabled: true }), button('清理授权临时数据', { variant: 'ghost', icon: 'trash', onClick: cleanupGarminOAuth, disabled: true }), element('span', { class: 'status-message', id: 'settings-status', text: '保存后可验证配置。凭据留空会保留已保存值。' })]),
    element('p', { class: 'session-status', id: 'oauth-cleanup-status', text: '连接仓库后可检查。' }),
  ];

  const runBody = [
    element('div', { class: 'session-grid' }, [
      element('section', { class: 'session-card', 'aria-labelledby': 'sync-title' }, [
        element('div', { class: 'session-title' }, [element('span', { id: 'sync-title', text: '手动同步' }), element('span', { class: 'badge', text: 'sync.yml' })]),
        makeField({ id: 'sync-targets', label: '目标区域', value: 'DEFAULT', options: [{ value: 'DEFAULT', label: '使用已保存的默认设置' }, { value: 'GLOBAL', label: '仅国际区（仅本次）' }, { value: 'CN', label: '仅中国区（仅本次）' }, { value: 'CN,GLOBAL', label: '双区（仅本次）' }] }),
        makeField({ id: 'sync-pages', label: '发现页数', value: 'DEFAULT', options: [{ value: 'DEFAULT', label: '使用已保存的默认页数' }, { value: '1', label: '1 页（最多 20 条）' }, { value: '3', label: '3 页' }, { value: '10', label: '10 页' }, { value: '20', label: '20 页' }, { value: '50', label: '50 页' }, { value: '100', label: '100 页' }, { value: '0', label: '不限页数' }] }),
        makeField({ id: 'sync-record-id', label: '指定活动编号（可选）', placeholder: '只处理此活动', help: '重新上传时必须填写。' }),
        element('div', { class: 'divider' }),
        checkField('sync-dry-run', '仅验证与转换，不上传', '建议首次使用时保持开启。', true),
        checkField('sync-full-scan', '全量扫描历史', '忽略发现页数，从第一页扫描。'),
        checkField('sync-retry-auth', '重试登录失效活动', '让任务重试之前因鉴权问题失败的活动。'),
        checkField('sync-force', '重新上传指定活动', '可能产生 Garmin 重复活动，仅用于明确修复。'),
        element('div', { class: 'actions-row' }, [button('提交同步', { variant: 'primary', icon: 'play', onClick: () => dispatchSync(false), disabled: true })]),
      ]),
      element('section', { class: 'session-card', 'aria-labelledby': 'reconcile-title' }, [
        element('div', { class: 'session-title' }, [element('span', { id: 'reconcile-title', text: '核对并补同步' }), element('span', { class: 'badge', text: 'reconcile.yml' })]),
        element('p', { text: '核对最近活动与 Garmin 台账，并按需要补同步。' }),
        makeField({ id: 'reconcile-targets', label: '核对区域', value: 'DEFAULT', options: [{ value: 'DEFAULT', label: '使用已保存的默认设置' }, { value: 'GLOBAL', label: '仅国际区（仅本次）' }, { value: 'CN', label: '仅中国区（仅本次）' }, { value: 'CN,GLOBAL', label: '双区（仅本次）' }] }),
        makeField({ id: 'reconcile-recent', label: '最近活动数量', value: 'DEFAULT', options: [{ value: 'DEFAULT', label: '使用已保存的默认条数' }, { value: '1', label: '1 条' }, { value: '10', label: '10 条' }, { value: '20', label: '20 条' }, { value: '50', label: '50 条' }, { value: '100', label: '100 条' }, { value: '200', label: '200 条' }] }),
        element('div', { class: 'divider' }),
        checkField('reconcile-dry-run', '仅核对，不补传', '开启时只记录核对结果。', true),
        element('div', { class: 'actions-row' }, [button('提交核对', { variant: 'subtle', icon: 'refresh', onClick: dispatchReconcile, disabled: true })]),
      ]),
    ]),
    element('p', { class: 'status-message', id: 'run-status', text: '提交后可在运行记录中查看进度。' }),
  ];

  const scheduleCard = element('section', { class: 'card aside-card', id: 'schedule', 'aria-labelledby': 'schedule-title' }, [
    element('div', { class: 'aside-heading' }, [element('h2', { id: 'schedule-title', text: '自动同步' }), icon('clock', true)]),
    checkField('schedule-enabled', '开启自动同步', '每小时第 7 和 37 分钟运行，使用已保存的区域设置。'),
    button('保存自动开关', { small: true, block: true, icon: 'clock', onClick: saveSchedule, disabled: true }),
    element('p', { class: 'session-status', id: 'schedule-status', text: '连接仓库后可调整。' }),
  ]);

  const runsCard = element('section', { class: 'card aside-card', 'aria-labelledby': 'runs-title' }, [
    element('div', { class: 'aside-heading' }, [element('h2', { id: 'runs-title', text: '最近运行' }), button('刷新', { small: true, icon: 'refresh', onClick: () => refreshRuns(), disabled: true })]),
    element('ul', { class: 'run-list', id: 'run-list', 'aria-live': 'polite' }),
  ]);

  const oauthDialog = element('dialog', { class: 'oauth-dialog', id: 'garmin-oauth-dialog', 'aria-labelledby': 'garmin-oauth-title' }, [
    element('form', { onSubmit: submitGarminOAuth }, [
      element('div', { class: 'oauth-dialog-header' }, [
        element('div', {}, [
          element('h2', { id: 'garmin-oauth-title', text: '获取 Garmin 授权' }),
          element('p', { id: 'garmin-oauth-copy', text: '凭据将由你的私有 GitHub Actions 临时处理。' }),
        ]),
        element('button', { id: 'garmin-oauth-close', type: 'button', class: 'icon-button', title: '关闭', 'aria-label': '关闭 Garmin 授权窗口', onClick: closeGarminOAuthDialog }, icon('close')),
      ]),
      element('div', { class: 'oauth-dialog-body' }, [
        notice('warning', '私有 Actions 登录', '账号和密码会经 GitHub 公钥加密写入一次性 Secret。授权期间请保持本标签页打开；OAuth 保存后会立即清理临时 Secret 与加密结果。'),
        makeField({ id: 'garmin-oauth-username', label: 'Garmin 账号', placeholder: '邮箱或用户名', required: true, autocomplete: 'username' }),
        makeField({ id: 'garmin-oauth-password', label: 'Garmin 密码', type: 'password', placeholder: '仅用于本次授权', required: true, autocomplete: 'current-password' }),
        element('p', { class: 'oauth-dialog-status', id: 'garmin-oauth-status', role: 'status', 'aria-live': 'polite', text: '等待提交。' }),
      ]),
      element('div', { class: 'oauth-dialog-footer' }, [
        button('取消', { id: 'garmin-oauth-cancel', variant: 'ghost', onClick: closeGarminOAuthDialog }),
        button('开始获取', { id: 'garmin-oauth-submit', type: 'submit', variant: 'primary', icon: 'key' }),
      ]),
    ]),
  ]);
  oauthDialog.addEventListener('cancel', (event) => {
    if (state.oauthRunning) event.preventDefault();
    else clearGarminOAuthForm();
  });
  oauthDialog.addEventListener('close', clearGarminOAuthForm);

  // Create the forms once; mountDashboard places them in the page layout.
  app.replaceChildren(connectionPill,
    element('section', { class: 'status-grid', 'aria-label': '当前状态' }, [
      ...[['connection-value', 'GitHub', '等待连接'], ['installation-value', '同步程序', '尚未安装'], ['schedule-value', '自动同步', '尚未开启']].map(([id, label, text]) => element('div', { class: 'status-card' }, [element('p', { class: 'status-label', text: label }), element('p', { class: 'status-value', id, text })])),
    ]),
    card('connection', '01', '连接私有仓库', '使用 GitHub Token 连接专用私有仓库。', connectionBody, { text: '未连接' }),
    card('install', '02', '安装同步程序', '将同步程序安装到私有仓库。', installBody, { text: '等待连接' }),
    card('settings', '03', '保存运行设置', '凭据加密保存到仓库 Secrets。', settingsBody, { text: '等待连接' }),
    card('run', '04', '运行与核对', '通过 GitHub Actions 执行同步与核对。', runBody, { text: '等待连接' }),
    scheduleCard, runsCard,
    element('p', { class: 'status-message', id: 'activity-status', role: 'status', text: '' }),
    oauthDialog);
  renderRuns([]);

  for (const node of document.querySelectorAll('button')) {
    if (node.closest('#connection-form') || node.textContent === '断开并清空本页凭据') continue;
    if (node.matches('#install button, #settings button, #run button, #schedule button, .aside-card button')) node.dataset.requiresConnection = '';
  }
  for (const node of document.querySelectorAll('#connection-form button')) node.dataset.busyLock = '';
  get('garmin-oauth-submit').dataset.requiresConnection = '';
  dashboard = mountDashboard({ app, state, element, icon, button, notice, get, toast, actions: {
    dispatchSync, dispatchReconcile, refresh: () => runTask('正在刷新仓库与活动报告…', async () => { await refreshRepository({ quiet: true }); await loadRuns(); }),
  } });
  setBusy(false);
}

function renderEmbeddedWarning() {
  app.replaceChildren(element('main', { class: 'startup-state' }, [
    element('section', { class: 'startup-panel', role: 'alert' }, [
      element('p', { class: 'startup-label', text: 'ONELAP GARMIN SYNC' }),
      element('h1', { text: '请在新窗口打开控制台' }),
      element('p', { text: '为保护 GitHub Token 和账号凭据，控制台不在嵌入式页面中运行。' }),
      element('a', { class: 'button primary', href: './', target: '_blank', rel: 'noopener noreferrer', text: '在新窗口打开' }),
    ]),
  ]));
}

if (embedded) {
  renderEmbeddedWarning();
} else {
  buildInterface();
  window.setInterval(() => { void pollRuns(); }, 30_000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void pollRuns();
  });
  window.addEventListener('pagehide', () => {
    disconnect();
  });
  window.addEventListener('beforeunload', (event) => {
    if (!state.oauthRunning) return;
    event.preventDefault();
    event.returnValue = '';
  });
}
