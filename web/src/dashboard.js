import { TARGETS, TARGET_NAMES, STATUS, enabledTargets, uploadState, activityComplete, summarize, formatTime, filterActivities, configCompleteness, manualImpact, ledgerSaveState, simulationEvidence } from './dashboard-model.js';

const PAGES = {
  overview: ['同步概览', '查看同步状态与待处理活动。', 'activity'],
  activities: ['活动', '查看各平台的同步结果。', 'download'],
  runs: ['运行记录', '查看任务进度、日志与台账保存结果。', 'clock'],
  reconcile: ['Garmin 核对与补传', '先确认远端是否缺失，再决定补传。', 'shield'],
  settings: ['配置与授权', '管理账号、目标平台与同步设置。', 'settings'],
  setup: ['首次配置', '先验证一条，再开启自动化。', 'check'],
};

export function mountDashboard({ app, state, element: el, icon, button, notice, get, toast, actions }) {
  const old = {
    connection: get('connection'), install: get('install'), settings: get('settings'), run: get('run'),
    schedule: get('schedule'), runs: get('run-list').closest('section'), oauth: get('garmin-oauth-dialog'),
    pill: get('connection-pill'), status: get('activity-status'), stats: document.querySelector('.status-grid'),
  };
  let report = null;
  let runs = [];
  let reportError = '';
  let route = 'overview';
  let detailId = '';
  let selectedRun = '';
  let page = 1;
  let setupStep = 0;
  let setupRecordId = '';
  let settingsTab = 'accounts';
  let previousClient = null;
  let requestGeneration = 0;
  const filters = { query: '', target: '', status: '', tab: 'all', days: 0 };
  const jobs = new Map();
  const acknowledgements = new Map();
  const validations = new Map();
  const invalidatedRuns = new Set();
  const validationKey = id => `${id}:${targets().join(',')}`;
  const targets = () => enabledTargets(state.snapshot);
  const retryLimit = () => Number(state.snapshot.variables?.SYNC_MAX_RETRIES || 8);
  const rows = () => report?.activity_items || [];
  const replace = (node, ...children) => node.replaceChildren(...children.filter(child => child != null));
  const muted = text => el('p', { class: 'muted', text });
  const badge = (status, label) => {
    const [copy, color, symbol] = STATUS[status] || STATUS.unknown;
    return el('span', { class: `result-badge ${color}` }, [icon(symbol, true), el('span', { text: label || copy })]);
  };
  const panel = (title, body, extra) => el('section', { class: 'panel' }, [el('div', { class: 'panel-heading' }, [el('h2', { text: title }), extra]), ...body]);
  const linkButton = (text, onClick, symbol = 'external') => button(text, { variant: 'ghost', small: true, icon: symbol, onClick });
  const action = (text, onClick, options = {}) => {
    const node = button(text, { ...options, onClick });
    node.dataset.requiresConnection = '';
    return node;
  };
  const empty = (title, copy, controls = []) => el('div', { class: 'empty-state' }, [el('span', { class: 'empty-icon' }, icon('activity')), el('h3', { text: title }), muted(copy), el('div', { class: 'actions-row' }, controls)]);
  const select = (id, label, options, change, value = '') => {
    const input = el('select', { id, 'aria-label': label, onChange: change }, options.map(([key, name]) => el('option', { value: key, text: name, selected: key === String(value) })));
    return el('label', { class: 'filter-field' }, [el('span', { class: 'sr-only', text: label }), input]);
  };
  const go = (name, id = '') => { window.location.hash = `/${name}${id ? `/${encodeURIComponent(id)}` : ''}`; navigate(name, id); };
  const closeButton = (label, close) => button('', { icon: 'close', variant: 'icon-button', ariaLabel: label, onClick: close });

  // One navigation tree: sidebar on desktop, bottom tabs on touch and narrow screens.
  const shortLabels = { overview: '概览', activities: '活动', runs: '运行', reconcile: '核对', settings: '设置' };
  const nav = el('nav', { class: 'nav-list', 'aria-label': '主导航' }, Object.entries(PAGES).filter(([key]) => key !== 'setup').map(([key, [title, , symbol]]) => el('a', { href: `#/${key}`, class: 'nav-item', 'aria-label': title, 'data-page': key, onClick: event => { event.preventDefault(); go(key); } }, [icon(symbol), el('span', { class: 'nav-label-full', 'aria-hidden': 'true', text: title }), el('span', { class: 'nav-label-short', 'aria-hidden': 'true', text: shortLabels[key] })])));
  const modeLabel = el('span', { class: 'instance-label' });
  const repositoryButton = button('', { id: 'mobile-repository', icon: 'server', ariaLabel: '管理连接仓库', variant: 'mobile-repository icon-button', onClick: () => { settingsTab = 'deployment'; go('settings'); } });
  const title = el('h1', { id: 'page-title', tabIndex: '-1', text: '同步概览' });
  const subtitle = muted(PAGES.overview[1]);
  const content = el('div', { class: 'page-stack', id: 'page-content' });
  const titleActions = el('div', { class: 'actions-row' });
  const banner = el('div', { id: 'data-banner', 'aria-live': 'polite' });
  const sidebar = el('aside', { class: 'sidebar', id: 'dashboard-sidebar' }, [
    el('a', { class: 'brand', href: '#/overview', onClick: event => { event.preventDefault(); go('overview'); } }, [el('span', { class: 'brand-mark', text: '»' }), el('span', {}, [el('strong', { text: 'OneLap Sync' }), el('small', { text: '个人运动同步' })])]),
    nav, el('div', { class: 'sidebar-bottom' }, [linkButton('首次配置向导', () => go('setup'), 'check')]),
  ]);
  const main = el('main', { class: 'main', id: 'main-content' }, [el('div', { class: 'page-heading' }, [el('div', {}, [title, subtitle]), titleActions]), banner, content]);
  const settingsPage = el('div', { class: 'page-stack' });
  const reconcilePage = el('div', { class: 'page-stack' });
  const setupPage = el('div', { class: 'page-stack' });
  const storage = el('div', { hidden: true, id: 'component-storage' });
  app.append(storage); // Keep moved forms discoverable while assembling the shell.
  // Move existing forms with their secure handlers intact; no second copy of credential inputs.
  const syncSection = get('sync-title').closest('.session-card');
  const reconcileSection = get('reconcile-title').closest('.session-card');
  const runStatus = get('run-status');
  const runBadge = get('run-badge');
  old.runs.hidden = true;
  storage.append(old.runs, old.run, old.stats);
  const drawerStatus = el('p', { id: 'manual-status', class: 'status-message', role: 'status' });
  const drawerSummary = el('div', { class: 'scope-summary', id: 'manual-scope', 'aria-live': 'polite' });
  const drawerSubmit = button('开始同步', { id: 'manual-submit', icon: 'play', variant: 'primary block', onClick: () => submitManual() });
  const drawer = el('dialog', { class: 'manual-drawer', id: 'manual-drawer', 'aria-labelledby': 'manual-title' }, [
    el('div', { class: 'drawer-heading' }, [el('div', {}, [el('h2', { id: 'manual-title', text: '手动同步' })]), closeButton('关闭手动同步', () => closeDrawer())]),
    el('div', { class: 'drawer-body' }, [syncSection, drawerSummary, notice('info', '同步范围', '指定编号仅筛选已发现活动。查不到时请先扩大扫描范围。'), drawerStatus, runStatus]),
    el('div', { class: 'drawer-footer' }, [button('取消', { variant: 'subtle', onClick: () => closeDrawer() }), drawerSubmit]),
  ]);
  app.append(drawer);
  syncSection.querySelector('.actions-row')?.remove();
  get('sync-record-id-help').textContent = '筛选已发现或台账中的活动，不会按链接直接抓取未知记录。';
  const mode = select('manual-mode', '操作模式', [['normal', '普通同步'], ['auth', '授权重试'], ['full', '全量重新发现'], ['force', '指定活动强制重传']], event => setManualMode(event.target.value), 'normal');
  mode.className = 'field';
  mode.querySelector('span').className = '';
  syncSection.prepend(mode);
  for (const id of ['sync-full-scan', 'sync-retry-auth', 'sync-force']) get(id).closest('.check-field').hidden = true;
  get('sync-retry-auth').closest('.check-field').querySelector('small').textContent = '保留其他目标已完成结果；达到上限的任务需排查后显式重新排队。';
  for (const id of ['sync-targets', 'sync-pages', 'sync-record-id', 'sync-dry-run']) get(id).addEventListener('input', refreshManualScope);
  for (const id of ['sync-targets', 'sync-pages']) get(id).addEventListener('change', refreshManualScope);
  let drawerTrigger;
  function openDrawer({ id = '', mode: operation = 'normal', dryRun } = {}) {
    drawerTrigger = document.activeElement;
    get('sync-record-id').value = id;
    if (typeof dryRun === 'boolean') get('sync-dry-run').checked = dryRun;
    setManualMode(operation);
    drawerStatus.textContent = '';
    refreshManualScope();
    if (!drawer.open) drawer.showModal();
  }
  function closeDrawer() { if (drawer.open) drawer.close(); }
  drawer.addEventListener('close', () => drawerTrigger?.focus?.());
  function setManualMode(operation) {
    get('manual-mode').value = operation;
    get('sync-full-scan').checked = operation === 'full';
    get('sync-retry-auth').checked = operation === 'auth';
    get('sync-force').checked = operation === 'force';
    get('sync-record-id').required = operation === 'force';
    refreshManualScope();
  }
  function refreshManualScope() {
    const impact = manualImpact(get('sync-targets').value, state.snapshot, Boolean(get('sync-dry-run').checked));
    const pages = get('sync-pages').value === 'DEFAULT' ? state.snapshot.variables?.ONELAP_DISCOVERY_PAGES || '10' : get('sync-pages').value;
    const activity = rows().find(row => row.id === get('sync-record-id').value.trim());
    replace(drawerSummary, el('h3', { text: '本次实际影响' }),
      el('strong', { text: impact.targets.map(key => TARGET_NAMES[key]).join(' + ') || '尚未配置完整目标' }),
      muted(`运行方式：${impact.dryRun ? '模拟运行，不上传' : '真实上传'}${state.snapshot.variables?.SYNC_DRY_RUN === 'true' ? '（仓库级强制模拟）' : ''}`),
      muted(`发现范围：${get('sync-full-scan').checked || pages === '0' ? '不限页数' : `${pages} 页，每页最多 20 条`}；已有历史待处理任务也可能执行。`),
      activity ? el('p', { text: `已发现活动：${activity.name}` }) : get('sync-record-id').value.trim() ? muted('该编号不在当前已加载报告中；不代表来源不存在。') : null,
      impact.targets.includes('STRAVA') ? muted('Strava 由独立开关启用；选择 Garmin 区域不会关闭 Strava。') : null);
    drawerSubmit.textContent = impact.dryRun ? '提交模拟任务' : '开始同步';
    drawerSubmit.disabled = state.busy || (!state.client || !state.snapshot.installed);
  }
  async function submitManual() {
    if (get('sync-force').checked && !get('sync-record-id').value.trim()) { drawerStatus.textContent = '强制重传必须填写指定活动编号。'; get('sync-record-id').focus(); return; }
    await actions.dispatchSync(false);
  }
  let forceResolver;
  let forceTrigger;
  const forceContent = el('div', { class: 'force-content' });
  const forceAcknowledge = el('input', { type: 'checkbox', id: 'force-ack', onChange: () => { forceSubmit.disabled = !forceAcknowledge.checked; } });
  const forceSubmit = button('确认重新排队', { id: 'force-submit', variant: 'danger', disabled: true, onClick: () => { forceResolver?.(true); forceResolver = null; forceDialog.close(); } });
  const forceDialog = el('dialog', { class: 'confirm-dialog', id: 'force-dialog', 'aria-labelledby': 'force-title' }, [
    el('div', { class: 'drawer-heading' }, [el('h2', { id: 'force-title', text: '确认指定活动强制重传' }), closeButton('取消强制重传', () => forceDialog.close())]),
    forceContent, el('label', { class: 'check-field force-check', for: 'force-ack' }, [forceAcknowledge, el('span', { text: '我已确认实际目标与重复风险。' })]),
    el('div', { class: 'drawer-footer' }, [button('返回调整', { variant: 'subtle', onClick: () => forceDialog.close() }), forceSubmit]),
  ]);
  forceDialog.addEventListener('close', () => { forceResolver?.(false); forceResolver = null; forceTrigger?.focus?.(); });
  function confirmForce(inputs) {
    const impact = manualImpact(inputs.target_regions, state.snapshot, inputs.dry_run);
    const activity = rows().find(row => row.id === inputs.record_id);
    forceTrigger = document.activeElement;
    forceAcknowledge.checked = false;
    forceSubmit.disabled = true;
    forceSubmit.textContent = impact.dryRun ? '确认模拟重新排队' : '确认重新排队该活动';
    replace(forceContent, muted('此操作会重排指定活动在本次所有启用目标的任务，包括已经完成的目标。'),
      el('div', { class: 'scope-summary' }, [el('strong', { text: activity?.name || '指定 OneLap 活动' }), el('code', { text: inputs.record_id }), muted(`实际影响：${impact.targets.map(key => TARGET_NAMES[key]).join(' + ') || '无完整目标'}`)]),
      notice(impact.dryRun ? 'info' : 'warning', impact.dryRun ? '本次为模拟运行' : '可能产生重复活动', impact.dryRun ? '不会上传，但运行仍可能修改本地台账。' : '请先确认目标平台是否已存在活动。优先更新授权并重试未完成任务。'),
      table(['实际目标', '当前台账结果', '本次操作'], impact.targets.map(key => [TARGET_NAMES[key], badge(uploadState(activity?.targets?.[key], true, retryLimit())), '重新排队'])),
      impact.targets.includes('STRAVA') ? notice('warning', 'Strava 仍然启用', '只选中国区或国际区不会关闭 Strava；如需排除，请先返回配置页关闭独立开关并保存。') : null);
    forceDialog.showModal();
    return new Promise(resolve => { forceResolver = resolve; });
  }

  function table(headers, data, kind = 'records') {
    // Keep a single set of controls and table semantics when rows become mobile cards.
    return el('div', { class: 'table-scroll' }, el('table', { class: `data-table ${kind}-table`, role: 'table' }, [
      el('thead', { role: 'rowgroup' }, el('tr', { role: 'row' }, headers.map(text => el('th', { scope: 'col', role: 'columnheader', text })))),
      el('tbody', { role: 'rowgroup' }, data.map(row => el('tr', { role: 'row' }, row.map((value, index) => el('td', { role: 'cell' }, [
        el('span', { class: 'cell-label', 'aria-hidden': 'true', text: headers[index] }),
        el('div', { class: 'cell-value' }, typeof value === 'string' || typeof value === 'number' ? document.createTextNode(String(value)) : value),
      ]))))),
    ]));
  }
  function activityTable(items, compact = false) {
    return table(['活动', '开始时间 · 北京时间', ...TARGETS.map(key => TARGET_NAMES[key]), '操作'], items.map(row => [
      el('div', { class: 'activity-name' }, [button(row.name, { variant: 'text-button', onClick: () => go('activities', row.id) }), el('small', { class: 'mono muted', text: row.id })]),
      el('span', { class: 'numeric', text: formatTime(row.activity_time, compact) }),
      ...TARGETS.map(key => button(STATUS[uploadState(row.targets?.[key], targets().includes(key), retryLimit())][0], { variant: `status-button ${STATUS[uploadState(row.targets?.[key], targets().includes(key), retryLimit())][1]}`, icon: STATUS[uploadState(row.targets?.[key], targets().includes(key), retryLimit())][2], small: true, ariaLabel: `${row.name}，${TARGET_NAMES[key]}，查看结果`, onClick: () => { go('activities', row.id); get(`target-${key}`)?.focus(); } })),
      linkButton('查看详情', () => go('activities', row.id)),
    ]), 'activity');
  }
  function sourceGraph() {
    const complete = configCompleteness(state.snapshot);
    return panel('同步链路', [el('div', { class: 'sync-graph' }, [
      el('div', { class: 'source-column' }, [muted('来源'), el('div', { class: 'source-tile' }, [el('span', { class: 'platform-logo onelap', text: '»' }), el('div', {}, [el('h3', { text: 'OneLap' }), muted('运动记录'), el('small', { text: report ? `报告更新 ${formatTime(report.updated_at, true)}` : '等待首次发现活动' })])])]),
      el('div', { class: 'graph-arrow', 'aria-hidden': true, text: '→' }),
      el('div', { class: 'target-column' }, [muted('目标平台'), ...TARGETS.map(key => {
        const auth = report?.ledger.uploads.some(row => row.target_region === key && row.status === 'auth_required' && row.count > 0);
        return el('button', { class: 'target-tile', type: 'button', onClick: () => { settingsTab = 'accounts'; go('settings'); } }, [el('span', { class: `platform-logo ${key.toLowerCase()}`, text: key === 'STRAVA' ? 'S' : 'G' }), el('span', { class: 'target-copy' }, [el('strong', { text: TARGET_NAMES[key] }), key === 'STRAVA' && targets().includes(key) ? el('small', { text: '上传前自动核对' }) : null]), badge(!targets().includes(key) ? 'disabled' : auth ? 'auth_required' : complete[key] ? 'success' : 'pending', targets().includes(key) && !auth ? complete[key] ? '待验证' : '需要配置' : undefined), el('span', { text: '›', class: 'chevron' })]);
      })]),
    ])]);
  }
  function stats(items) {
    return el('div', { class: 'metrics-grid' }, items.map(([name, value, symbol, color = 'teal']) => el('div', { class: 'metric' }, [el('span', { class: `metric-icon ${color}` }, icon(symbol)), el('div', {}, [el('strong', { class: 'metric-value', text: String(value) }), muted(name)])])));
  }
  function overview() {
    const data = report;
    const summary = summarize(data, targets());
    const auth = (data?.ledger.uploads || []).filter(row => targets().includes(row.target_region) && row.status === 'auth_required' && row.count);
    content.append(sourceGraph());
    if (auth.length) content.append(notice('warning', `${auth.map(row => TARGET_NAMES[row.target_region]).join('、')}授权需要更新`, '已完成目标保留。更新凭据后可执行授权重试；达到尝试上限的活动需先排查。'), el('div', { class: 'banner-actions' }, [linkButton('更新授权', () => { settingsTab = 'accounts'; go('settings'); }, 'key'), linkButton('授权重试', () => openDrawer({ mode: 'auth' }), 'refresh')]));
    if (!targets().length) content.append(notice('info', '请先配置目标平台', '连接私有仓库并保存目标授权后，即可开始同步。'));
    content.append(panel('同步统计', [stats([['来源活动', data?.ledger.available ? summary.activities : '—', 'download'], ['目标任务', data?.ledger.available ? summary.total : '—', 'activity'], ['已完成', data?.ledger.available ? summary.complete : '—', 'check', 'green'], ['待处理', data?.ledger.available ? summary.pending : '—', 'clock', 'amber']])], el('span', { class: 'caption', text: '按当前启用目标计数' })));
    content.append(panel('最近活动', rows().length ? [activityTable(rows().slice(0, 4), true)] : [empty('还没有活动报告', state.client ? '完成一次同步或配置验证后，刷新读取私有台账。旧版同步程序需先升级。' : '连接私有仓库后开始同步。', [linkButton('开始配置', () => go('setup'), 'play')])], linkButton('查看全部活动', () => go('activities'))));
    const recent = runs[0];
    content.append(el('div', { class: 'two-columns' }, [panel('最近运行', [recent ? button(`${formatTime(recent.created_at, true)} · ${recent.conclusion === 'success' ? '执行成功' : recent.status === 'completed' ? '需要查看报告' : '执行中'}`, { variant: 'text-button', onClick: () => go('runs', String(recent.id)) }) : muted('还没有读取到运行记录')], icon('play')), panel('自动化', [el('h3', { text: state.snapshot.variables?.SYNC_ENABLED === 'true' ? '自动同步已开启' : '自动同步尚未开启' }), muted('每小时第 7、37 分钟触发，GitHub Actions 可能延迟。')], linkButton('管理', () => { settingsTab = 'behavior'; go('settings'); }))]));
  }

  function activities() {
    if (detailId) { activityDetail(); return; }
    const tabs = el('div', { class: 'segmented', role: 'group', 'aria-label': '活动完成情况' }, [['all', '全部'], ['attention', '需要处理'], ['complete', '全部完成']].map(([key, name]) => button(`${name}  ${rows().filter(row => key === 'all' || (key === 'complete' ? activityComplete(row, targets()) : !activityComplete(row, targets()))).length}`, { variant: filters.tab === key ? 'selected' : '', onClick: () => { filters.tab = key; page = 1; render(); } })));
    const search = el('input', { id: 'activity-search', type: 'search', placeholder: '搜索活动名称或编号', 'aria-label': '搜索活动名称或编号', value: filters.query, onInput: event => { filters.query = event.target.value; page = 1; renderActivityResults(); } });
    const toolbar = el('div', { class: 'filter-bar' }, [el('label', { class: 'search-field' }, [icon('activity', true), search]),
      select('filter-days', '时间范围', [['0', '全部时间'], ['7', '最近 7 天'], ['30', '最近 30 天']], event => { filters.days = Number(event.target.value); page = 1; renderActivityResults(); }, filters.days),
      select('filter-target', '目标平台', [['', '全部目标'], ...TARGETS.map(key => [key, TARGET_NAMES[key]])], event => { filters.target = event.target.value; page = 1; renderActivityResults(); }, filters.target),
      select('filter-status', '目标状态', [['', '全部状态'], ...Object.entries(STATUS).filter(([key]) => key !== 'disabled').map(([key, [name]]) => [key, name])], event => { filters.status = event.target.value; page = 1; renderActivityResults(); }, filters.status),
      linkButton('清空', () => { Object.assign(filters, { query: '', days: 0, target: '', status: '', tab: 'all' }); page = 1; render(); }, 'close')]);
    content.append(tabs, toolbar, el('div', { id: 'activity-results', class: 'panel table-panel' }));
    renderActivityResults();
  }
  function renderActivityResults() {
    const list = filterActivities(rows(), { ...filters, now: Date.now() }, targets(), retryLimit());
    const totalPages = Math.max(1, Math.ceil(list.length / 10));
    page = Math.min(page, totalPages);
    get('activity-results')?.replaceChildren(list.length ? activityTable(list.slice((page - 1) * 10, page * 10)) : empty('没有符合条件的活动', rows().length ? '调整筛选条件，或清空搜索重试。' : '报告暂未包含活动明细。请完成一次运行并刷新，或升级旧版同步程序。'),
      el('div', { class: 'pagination' }, [muted(`已加载 ${rows().length} 条 · 符合条件 ${list.length} 条（最多显示最近 200 条）`), el('div', { class: 'actions-row' }, [button('上一页', { small: true, disabled: page <= 1, onClick: () => { page--; renderActivityResults(); } }), el('span', { class: 'numeric', text: `第 ${page} / ${totalPages} 页` }), button('下一页', { small: true, disabled: page >= totalPages, onClick: () => { page++; renderActivityResults(); } })])]));
  }
  function activityDetail() {
    const row = rows().find(item => item.id === detailId);
    content.append(linkButton('返回活动列表', () => go('activities'), 'back'));
    if (!row) { content.append(empty('当前报告中未找到该活动', '这里只加载最近 200 条。请确认编号，或查看私有仓库的历史台账。')); return; }
    title.textContent = row.name;
    subtitle.textContent = activityComplete(row, targets()) ? '当前启用目标均已完成。' : '查看目标状态与处理记录。';
    content.append(el('div', { class: 'metadata-strip' }, [el('span', { class: 'mono', text: `OneLap · ${row.id}` }), el('span', { text: `${formatTime(row.activity_time)} 北京时间` }), badge(activityComplete(row, targets()) ? 'success' : 'pending', activityComplete(row, targets()) ? '全部完成' : '仍需关注')]));
    content.append(el('div', { class: 'target-detail-grid' }, TARGETS.map(key => {
      const upload = row.targets[key];
      const status = uploadState(upload, targets().includes(key), retryLimit());
      const remote = /^\d+$/.test(upload?.remote_activity_id || '') ? `https://${key === 'STRAVA' ? 'www.strava.com/activities/' : key === 'CN' ? 'connect.garmin.cn/modern/activity/' : 'connect.garmin.com/modern/activity/'}${upload.remote_activity_id}` : '';
      const evidence = upload?.status === 'success' ? '上传成功响应' : upload?.status === 'duplicate' ? '重复响应或远端匹配（以运行日志为准）' : upload?.status === 'uploading' ? '平台异步处理中，尚无完成证据' : '尚无完成证据';
      return el('section', { class: 'panel target-detail', id: `target-${key}`, tabIndex: '-1' }, [el('div', { class: 'panel-heading' }, [el('h3', { text: TARGET_NAMES[key] }), badge(status)]),
        el('dl', { class: 'evidence-list' }, [['完成证据', evidence], ['远端编号', upload?.remote_activity_id || '暂无'], ['尝试次数', `${upload?.attempts || 0} / ${retryLimit()}`], ['最近结果', formatTime(upload?.updated_at)], ['核对结论', upload?.reconciliation_status ? STATUS[upload.reconciliation_status]?.[0] : '未执行或无报告'], ['核对时间', formatTime(upload?.reconciliation_checked_at)], ...(upload?.next_retry_at ? [['计划重试', formatTime(upload.next_retry_at)]] : [])].flatMap(([name, value]) => [el('dt', { text: name }), el('dd', { text: value })])),
        !targets().includes(key) && upload ? muted('目标已关闭，以上历史证据仍保留。') : null,
        status === 'auth_required' ? linkButton('更新授权', () => { settingsTab = 'accounts'; go('settings'); }, 'key') : null,
        status === 'exhausted' ? notice('warning', '先排查，再重新排队', '授权重试仍受最大尝试次数限制。不要直接批量强制重传。') : null,
        remote ? el('a', { class: 'button subtle', href: remote, target: '_blank', rel: 'noopener noreferrer', text: '查看远端活动 ↗' }) : null,
      ]);
    })));
    content.append(panel('恢复建议', [el('ol', { class: 'recovery-steps' }, ['更新对应区域授权', '保存配置并执行验证', '重试未完成的授权任务'].map(text => el('li', { text }))), muted('普通授权重试保留其他目标的完成结果。未能核实和匹配歧义需要先核对，不能认定为远端缺失。'), button('打开授权重试', { icon: 'refresh', variant: 'subtle', onClick: () => openDrawer({ id: row.id, mode: 'auth' }) })]));
    content.append(panel('文件与处理报告', [table(['检查项', '已保存结果'], [['下载状态', { success: '已下载', downloading: '下载中', pending: '待下载', failed: '下载失败' }[row.download_status] || '尚无报告'], ['处理状态', { success: '处理完成', processing: '处理中', pending: '待处理', not_requested: '未请求处理', skipped: '已跳过', failed: '处理失败' }[row.processing_status] || row.processing_status || '尚无报告'], ['原始文件 SHA-256', el('code', { class: 'hash', text: row.fit_sha256 || '报告未提供' })]]), muted('当前报告不包含逐事件日志与 FIT 详细校验结果，请到对应运行记录查看实际检查。')]));
    content.append(linkButton('高级操作：指定活动强制重传', () => openDrawer({ id: row.id, mode: 'force', dryRun: false }), 'warning'));
  }

  function runList() {
    if (selectedRun) { runDetail(); return; }
    const list = runs;
    content.append(panel('最近 20 次工作流', list.length ? [table(['运行', '触发方式', '开始时间 · 北京时间', '执行结果', '报告'], list.map(run => [
      el('div', { class: 'activity-name' }, [el('strong', { text: run.name || '同步工作流' }), el('small', { class: 'mono muted', text: `#${run.run_number || run.id}` })]),
      run.event === 'schedule' ? '自动计划' : run.event === 'workflow_dispatch' ? '手动触发' : run.event || '其他',
      formatTime(run.created_at), badge(run.status !== 'completed' ? 'uploading' : run.conclusion === 'success' ? 'success' : 'failed', run.status !== 'completed' ? '执行中' : run.conclusion === 'success' ? '执行成功' : run.conclusion === 'cancelled' ? '已取消' : '查看失败阶段'),
      linkButton('查看报告', () => go('runs', String(run.id))),
    ]), 'run')] : [empty('暂时没有运行记录', '连接仓库后刷新，或先提交一次配置验证。')], button('刷新', { small: true, icon: 'refresh', onClick: () => refresh() })));
  }
  function runDetail() {
    const run = runs.find(item => String(item.id) === selectedRun);
    content.append(linkButton('返回运行记录', () => go('runs'), 'back'));
    if (!run) { content.append(empty('当前列表中没有该运行', '请刷新运行记录后重试。')); return; }
    title.textContent = '运行报告';
    subtitle.textContent = `${run.name || '同步工作流'} · ${formatTime(run.created_at)} · 北京时间`;
    const entry = jobs.get(selectedRun);
    const save = entry?.data ? ledgerSaveState(entry.data) : 'unknown';
    const matching = String(report?.run.id) === String(run.id);
    if (save === 'failed') content.append(notice('error', '远端上传可能完成，但台账未成功保存', '先保留本次运行日志与恢复附件，核对远端活动，再决定恢复方式。退出失败不代表所有目标上传失败。'));
    else if (save === 'unknown') content.append(notice('info', '台账保存结果尚未核实', entry?.error || '正在读取运行步骤。'));
    const runUrl = safeRunUrl(run.html_url);
    if (runUrl) content.append(el('a', { class: 'button subtle align-start', href: runUrl, target: '_blank', rel: 'noopener noreferrer', text: '查看 GitHub Actions 日志 ↗' }));
    content.append(el('div', { class: 'two-columns' }, [panel('执行阶段', entry?.data ? [el('ol', { class: 'timeline' }, entry.data.flatMap(job => (job.steps || []).map(step => el('li', { class: step.conclusion === 'failure' ? 'error' : step.conclusion === 'success' ? 'success' : 'neutral' }, [icon(step.conclusion === 'failure' ? 'warning' : step.conclusion === 'success' ? 'check' : 'clock', true), el('div', {}, [el('strong', { text: step.name }), muted(`${formatTime(step.started_at, true)} · ${{ success: '成功', failure: '失败', cancelled: '已取消', skipped: '已跳过', timed_out: '已超时', action_required: '需要处理', neutral: '无结果', stale: '已过期', startup_failure: '启动失败', queued: '排队中', waiting: '等待中', pending: '待执行', in_progress: '执行中', completed: '已结束' }[step.conclusion || step.status] || '未知状态'}`)])]))))] : [muted(entry?.error || '正在读取运行步骤…')]),
      panel('台账保存', [badge(save === 'saved' ? 'success' : save === 'failed' ? 'missing' : 'unknown', { saved: '台账已保存', failed: '台账未保存', pending: '等待保存', unknown: '尚未核实' }[save]),
        muted(save === 'saved' ? '已确认 Actions 中“保存台账与汇总”阶段成功。' : '以“保存台账与汇总”步骤的结果为准。'),
        save === 'failed' ? el('ol', { class: 'recovery-list' }, ['保留本次日志与远端活动证据。', '若推送失败，在私有 Actions 下载 private-ledger-recovery 附件（保留 7 天）。', '人工恢复最新台账并核对远端，只补明确缺失的目标。'].map(text => el('li', { text }))) : null,
        button('进入 Garmin 核对', { icon: 'shield', variant: 'subtle', onClick: () => go('reconcile') }),
      ])]));
    if (matching) content.append(panel('本次运行范围', [muted(`目标：${report.run.targets.map(key => TARGET_NAMES[key]).join('、') || '报告未提供'} · ${report.run.dry_run ? '模拟运行' : '真实运行'}`), muted('以下为运行结束时的累计台账，不是本次新增上传数量。'), aggregateTable(report.ledger.uploads)]));
    else content.append(notice('info', '暂无该次运行的活动报告', '请在 GitHub Actions 日志中查看本次结果。'));
    if (!entry) loadJobs(run.id);
  }
  function safeRunUrl(value) { try { const url = new URL(value); return url.protocol === 'https:' && url.hostname === 'github.com' ? url.href : ''; } catch { return ''; } }
  function aggregateTable(groups) { return table(['目标', '台账状态', '数量'], groups.map(row => [TARGET_NAMES[row.target_region], badge(row.status), row.count])); }
  async function loadJobs(id) {
    const client = state.client;
    const generation = requestGeneration;
    if (!client) return;
    jobs.set(String(id), { loading: true });
    try { const data = await client.getRunJobs(id); if (client === state.client && generation === requestGeneration) jobs.set(String(id), { data }); }
    catch (error) { if (client === state.client && generation === requestGeneration) jobs.set(String(id), { error: error.message }); }
    if (route === 'runs' && selectedRun === String(id)) render();
  }

  const reconcileData = el('div', { class: 'page-stack', id: 'reconcile-results' });
  reconcileSection.querySelector('.session-title')?.remove();
  reconcileSection.className = 'panel reconcile-form';
  reconcileSection.append(muted('核对范围按 OneLap 活动条数计算。“只核对”会保存结果，不上传活动。'));
  reconcilePage.append(notice('info', '仅核对 Garmin', 'Strava 在主同步中执行独立预核对。读取不完整或匹配歧义不能推断为远端缺失。'), reconcileSection, reconcileData);
  function renderReconcileData() {
    const data = report;
    const groups = data?.ledger.reconciliation || [];
    const count = key => groups.filter(row => row.status === key && row.target_region !== 'STRAVA').reduce((sum, row) => sum + row.count, 0);
    const total = ['present', 'missing', 'unverified', 'ambiguous'].reduce((sum, key) => sum + count(key), 0);
    reconcileData.replaceChildren(panel('核对结果', [stats([['目标结论', data?.ledger.available ? total : '—', 'activity'], ['确认存在', data?.ledger.available ? count('present') : '—', 'check', 'green'], ['明确缺失', data?.ledger.available ? count('missing') : '—', 'warning', 'red'], ['未核实 / 歧义', data?.ledger.available ? count('unverified') + count('ambiguous') : '—', 'clock', 'purple']]), muted('核对时间见各活动详情。')]));
    const candidates = rows().filter(row => ['CN', 'GLOBAL'].some(key => row.targets?.[key]?.reconciliation_status));
    reconcileData.append(panel('活动核对明细', candidates.length ? [table(['活动', '中国区结论', '国际区结论', '操作'], candidates.slice(0, 20).map(row => [row.name, ...['CN', 'GLOBAL'].map(key => el('div', { class: 'evidence-cell' }, [badge(row.targets[key]?.reconciliation_status || 'unknown'), el('small', { class: 'muted', text: formatTime(row.targets[key]?.reconciliation_checked_at, true) })])), linkButton('查看详情', () => go('activities', row.id))]), 'reconcile')] : [empty('暂无核对结论', '提交只核对任务，完成后刷新报告。')]));
    if (count('unverified') + count('ambiguous') > 0) reconcileData.append(notice('warning', '部分活动尚未核实', '请查看活动详情。补传仅处理明确缺失的 Garmin 活动。'));
  }

  const accountFields = el('div', { class: 'page-stack' });
  const behaviorFields = el('div', { class: 'page-stack' });
  const processingFields = el('div', { class: 'page-stack' });
  const deploymentFields = el('div', { class: 'page-stack' });
  const settingsSummary = el('div', { class: 'page-stack', id: 'config-report' });
  const settingsPanels = { accounts: accountFields, behavior: behaviorFields, processing: processingFields, deployment: deploymentFields };
  const takeField = id => get(id).closest('.field') || get(id).closest('.check-field');
  accountFields.append(panel('OneLap 账号', [el('div', { class: 'form-grid' }, ['onelap-account', 'onelap-password', 'onelap-token', 'session-key'].map(takeField))]));
  accountFields.append(panel('目标平台', [takeField('settings-targets'), el('div', { class: 'two-columns' }, [takeField('garmin-cn-session'), takeField('garmin-global-session')])]));
  const stravaDetails = get('strava-enabled').closest('details');
  stravaDetails.open = true;
  accountFields.append(panel('Strava · 可选目标', [stravaDetails]), old.settings.querySelector('.session-help'));
  behaviorFields.append(panel('默认运行范围', [el('div', { class: 'form-grid' }, ['default-pages', 'default-recent', 'max-retries', 'global-dry-run'].map(takeField)), notice('info', '页数控制发现范围', '0 表示不限发现页数；这不是上传上限。关闭 Strava 会保留历史，并停止执行该目标存量失败任务。')]), old.schedule);
  processingFields.append(panel('文件处理与设备标记', [notice('info', '设备标记', '同步文件会写入 Garmin 设备标记，请填写实际设备信息。'), el('div', { class: 'form-grid' }, ['coordinate-transform', 'unit-id', 'product-preset', 'product-id', 'software-version'].map(takeField))]));
  deploymentFields.append(old.connection, old.install, old.stats, panel('台账与部署', [muted('活动报告从私有仓库读取，最多显示最近 200 条。'), muted('已安装的同步程序需手动升级，更新前请备份台账。')]));
  const shared = el('div', { class: 'settings-save panel' });
  for (const selector of ['#default-target-status', '.check-panel', '.actions-row', '#oauth-cleanup-status']) { const node = old.settings.querySelector(selector); if (node) shared.append(node); }
  const tabBar = el('div', { class: 'tab-bar', role: 'tablist', 'aria-label': '配置分类' }, [['accounts', '账号与目标'], ['behavior', '同步行为'], ['processing', '文件处理'], ['deployment', '部署与台账']].map(([key, name]) => button(name, { id: `settings-tab-${key}`, variant: 'tab-button', onClick: () => { settingsTab = key; renderSettings(); } })));
  for (const [key, node] of Object.entries(settingsPanels)) {
    node.id = `settings-panel-${key}`; node.setAttribute('role', 'tabpanel'); node.setAttribute('aria-labelledby', `settings-tab-${key}`);
    const tab = tabBar.querySelector(`#settings-tab-${key}`); tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', node.id);
    tab.addEventListener('keydown', event => {
      const keys = Object.keys(settingsPanels); let next = keys.indexOf(settingsTab);
      if (event.key === 'ArrowRight') next = (next + 1) % keys.length; else if (event.key === 'ArrowLeft') next = (next + keys.length - 1) % keys.length; else return;
      event.preventDefault(); settingsTab = keys[next]; renderSettings(); get(`settings-tab-${settingsTab}`).focus();
    });
  }
  const settingsForms = el('div', { class: 'page-stack' }, Object.values(settingsPanels));
  settingsPage.append(tabBar, el('div', { class: 'settings-layout' }, [settingsForms, settingsSummary]), shared);
  function renderSettings() {
    for (const [key, node] of Object.entries(settingsPanels)) node.hidden = key !== settingsTab;
    for (const tab of tabBar.querySelectorAll('button')) {
      const active = tab.id === `settings-tab-${settingsTab}`;
      tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1;
    }
    const snapshot = state.snapshot;
    const complete = configCompleteness(snapshot);
    const values = snapshot.variables || {};
    replace(settingsSummary, panel('凭据状态', ['ONELAP', ...TARGETS].map(key => el('div', { class: 'config-line' }, [el('strong', { text: key === 'ONELAP' ? 'OneLap' : TARGET_NAMES[key] }), badge(!state.client ? 'unknown' : key !== 'ONELAP' && !targets().includes(key) ? 'disabled' : complete[key] ? 'success' : 'pending', !state.client ? '未读取' : key !== 'ONELAP' && !targets().includes(key) ? '未启用' : complete[key] ? '已保存' : '待配置')]))),
      panel('已保存配置', [table(['设置', '已保存值', '来源'], [['Garmin 区域', ({ CN: '中国区', GLOBAL: '国际区', 'CN,GLOBAL': '双区' })[values.SYNC_TARGETS] || '自动判断', values.SYNC_TARGETS ? '仓库设置' : '默认值'], ['Strava', values.STRAVA_SYNC_ENABLED === 'true' ? '开启' : '关闭', values.STRAVA_SYNC_ENABLED ? '仓库设置' : '默认值'], ['模拟运行', values.SYNC_DRY_RUN === 'true' ? '开启' : '关闭', values.SYNC_DRY_RUN ? '仓库设置' : '默认值'], ['发现页数', values.ONELAP_DISCOVERY_PAGES || '10', values.ONELAP_DISCOVERY_PAGES ? '仓库设置' : '默认值']]), muted('运行参数以已保存设置和本次输入为准。凭据仅显示是否保存；未保存的表单不会生效。')]),
      notice('warning', '授权状态需通过运行验证', '已保存凭据不代表授权有效，请执行配置验证。'),
      values.SYNC_DRY_RUN === 'true' ? notice('info', '仓库级模拟已开启', '即使单次取消模拟，Runner 仍不会上传。需关闭此设置并保存后再执行真实同步。') : null,
      button('首次配置向导', { variant: 'subtle', icon: 'check', onClick: () => go('setup') }));
  }

  function setup() {
    const labels = ['选择平台', '准备授权', '单条验证', '确认历史', '开启自动化'];
    setupPage.replaceChildren(el('ol', { class: 'setup-stepper', 'aria-label': '首次配置进度' }, labels.map((label, index) => el('li', { class: index === setupStep ? 'current' : index < setupStep ? 'done' : '' }, button(`${index < setupStep ? '✓' : index + 1}  ${label}`, { variant: 'text-button', onClick: () => { if (index <= setupStep) { setupStep = index; render(); } } })))));
    const body = el('div', { class: 'page-stack' });
    const complete = configCompleteness(state.snapshot);
    const ready = complete.ONELAP && targets().length > 0 && targets().every(key => complete[key]);
    const next = (enabled = true) => button('确认，继续 →', { variant: 'primary align-start', disabled: !enabled, onClick: () => { setupStep++; render(); } });
    if (setupStep === 0) body.append(panel('选择你的接收平台', [muted('选择需要接收 OneLap 活动的平台。'), sourceGraph(), button('前往选择目标并保存', { variant: 'subtle', icon: 'settings', onClick: () => { settingsTab = 'accounts'; go('settings'); } }), muted('保存至少一个目标的授权后继续。'), next(targets().length > 0)]));
    if (setupStep === 1) body.append(panel('准备对应目标授权', [muted('先连接专用私有仓库并安装同步程序，再保存来源与目标凭据。'), button('连接仓库 / 安装程序', { variant: 'subtle', icon: 'server', onClick: () => { settingsTab = 'deployment'; go('settings'); } }), button('配置与授权', { variant: 'subtle', icon: 'key', onClick: () => { settingsTab = 'accounts'; go('settings'); } }), notice('info', ready ? '所需凭据已保存' : '尚有必需凭据未准备', '保存凭据后，请执行配置验证。'), next(ready)]));
    if (setupStep === 2) {
      const chooser = select('setup-activity', '选择已发现活动', [['', '选择一条已发现活动'], ...rows().map(row => [row.id, `${row.name} · ${row.id}`])], event => { setupRecordId = event.target.value; updateValidation(); }, setupRecordId);
      chooser.className = 'field'; chooser.querySelector('span').className = '';
      const validation = el('div', { class: 'page-stack', id: 'setup-validation' });
      body.append(panel('验证一条 OneLap 活动', [chooser, rows().length ? muted('先模拟并查看报告，再上传到目标平台验证。') : muted('先执行配置验证发现来源活动，完成后刷新报告再选择。'), action('发现活动 / 验证配置', () => actions.dispatchSync(true), { variant: 'subtle', icon: 'shield' }), validation]));
      function updateValidation() {
        const id = chooser.querySelector('select').value;
        const row = rows().find(item => item.id === id);
        const simulation = validations.get(validationKey(id));
        const ack = acknowledgements.get(validationKey(id)) || { simulation: false, visible: false };
        const simulationAck = el('input', { type: 'checkbox', id: 'setup-simulation-ack', checked: ack.simulation, disabled: !simulation, onChange: event => { ack.simulation = Boolean(event.target.checked); acknowledgements.set(validationKey(id), ack); updateValidation(); } });
        const visibleAck = el('input', { type: 'checkbox', id: 'setup-visible-ack', checked: ack.visible, disabled: !simulation || !ack.simulation, onChange: event => { ack.visible = Boolean(event.target.checked); acknowledgements.set(validationKey(id), ack); updateValidation(); } });
        validation.replaceChildren(row ? el('div', { class: 'scope-summary' }, [el('h3', { text: row.name }), muted(`${row.id} · ${formatTime(row.activity_time)}`)]) : muted('请先选择已发现的活动。'),
          notice(simulation ? 'success' : 'info', simulation ? '单条模拟成功' : '等待对应活动的成功模拟报告', simulation ? `运行 #${simulation} 已成功。修改配置后需重新模拟。` : '请先执行单条模拟，成功后刷新报告。'),
          button('执行单条模拟', { variant: 'subtle', icon: 'play', disabled: !row, onClick: () => openDrawer({ id, dryRun: true }) }),
          el('label', { class: 'check-field', for: 'setup-simulation-ack' }, [simulationAck, el('span', { text: '我已查看该活动模拟运行报告，确认读取与处理结果符合预期。' })]),
          muted(state.snapshot.variables?.SYNC_DRY_RUN === 'true' ? '仓库级模拟已开启：需要先在配置页关闭并保存，再重新模拟，才能执行真实上传。' : '模拟成功后，确认已阅读报告才可真实上传；完成后再人工确认远端可见。'),
          button('执行单条真实上传', { variant: 'primary', icon: 'upload', disabled: !row || !simulation || !ack.simulation || state.snapshot.variables?.SYNC_DRY_RUN === 'true', onClick: () => openDrawer({ id, dryRun: false }) }),
          el('label', { class: 'check-field', for: 'setup-visible-ack' }, [visibleAck, el('span', { text: '真实上传后，我已在启用的目标平台确认活动可见、内容正确。' })]),
          button('查看运行报告', { variant: 'ghost', icon: 'clock', onClick: () => go('runs') }), next(Boolean(row && simulation && ack.simulation && ack.visible)));
      }
      updateValidation();
    }
    if (setupStep === 3) {
      const historyAck = el('input', { id: 'history-ack', type: 'checkbox', onChange: event => { proceed.disabled = !event.target.checked; } });
      const proceed = next(false);
      body.append(panel('确认本次历史处理范围', [notice('warning', '后续可能处理历史待办', `当前累计发现 ${report?.ledger.activities || 0} 条活动。默认发现 ${state.snapshot.variables?.ONELAP_DISCOVERY_PAGES || '10'} 页，每页最多 20 条；发现数量不等于上传数量。`), muted('自动同步可能继续处理历史待办，暂不支持按日期限制上传范围。'), el('label', { class: 'check-field', for: 'history-ack' }, [historyAck, el('span', { text: '我已确认历史活动的处理范围。' })]), proceed]));
    }
    if (setupStep === 4) body.append(panel('准备开启自动同步', [notice('success', '首次验证流程已确认', '请保存自动同步开关。每小时第 7、37 分钟计划运行，实际执行可能延迟。'), button('前往开启自动化', { variant: 'primary', icon: 'clock', onClick: () => { settingsTab = 'behavior'; go('settings'); } }), button('暂时保持手动同步', { variant: 'subtle', onClick: () => go('overview') })]));
    setupPage.append(el('div', { class: 'setup-layout' }, [body, panel('本次有效配置', ['ONELAP', ...TARGETS].map(key => el('div', { class: 'config-line' }, [el('strong', { text: key === 'ONELAP' ? 'OneLap 账号' : TARGET_NAMES[key] }), badge(key !== 'ONELAP' && !targets().includes(key) ? 'disabled' : complete[key] ? 'success' : 'pending', key !== 'ONELAP' && !targets().includes(key) ? '未启用' : complete[key] ? '配置完整' : '需要配置')])))]));
    content.append(setupPage);
  }

  function render() {
    title.textContent = PAGES[route]?.[0] || '同步概览';
    subtitle.textContent = PAGES[route]?.[1] || '';
    const refreshControl = button('刷新', { small: true, icon: 'refresh', ariaLabel: '刷新数据', onClick: () => refresh() });
    refreshControl.querySelector('span').className = 'refresh-label';
    replace(titleActions, refreshControl,
      route === 'overview' ? button('核对 Garmin', { icon: 'shield', variant: 'subtle', onClick: () => go('reconcile') }) : null,
      button('手动同步', { id: 'open-manual', icon: 'refresh', variant: 'primary', onClick: () => openDrawer() }));
    for (const node of nav.querySelectorAll('a')) { const active = node.dataset.page === route || node.dataset.page === 'settings' && route === 'setup'; node.classList.toggle('active', active); if (active) node.setAttribute('aria-current', 'page'); else node.removeAttribute('aria-current'); }
    // Keep static forms mounted even when their view isn't selected.
    storage.append(settingsPage, reconcilePage);
    content.replaceChildren();
    if (route === 'overview') overview();
    if (route === 'activities') activities();
    if (route === 'runs') runList();
    if (route === 'reconcile') { renderReconcileData(); content.append(reconcilePage); }
    if (route === 'settings') { renderSettings(); content.append(settingsPage); }
    if (route === 'setup') setup();
    lock();
  }
  function navigate(name, id = '') {
    route = PAGES[name] ? name : 'overview'; detailId = route === 'activities' ? id : ''; selectedRun = route === 'runs' ? id : '';
    render(); title.focus?.({ preventScroll: true });
    window.scrollTo?.({ top: 0, behavior: 'instant' });
  }
  function routeFromHash() {
    const parts = window.location.hash.replace(/^#\/?/, '').split('/');
    let id = ''; try { id = decodeURIComponent(parts[1] || ''); } catch { /* Malformed URL falls back to empty detail. */ }
    navigate(parts[0], id);
  }
  function lock() {
    for (const node of document.querySelectorAll('[data-busy-lock]')) node.disabled = state.busy;
    for (const node of document.querySelectorAll('[data-requires-connection]')) node.disabled = state.busy || !state.client;
    for (const node of settingsPage.querySelectorAll('input, textarea, select')) node.disabled = state.busy;
    for (const node of reconcileSection.querySelectorAll('input, select, button')) node.disabled = state.busy || !state.client;
    drawerSubmit.disabled = state.busy || (!state.client || !state.snapshot.installed);
  }
  function update() {
    if (previousClient !== state.client) { previousClient = state.client; requestGeneration++; report = null; runs = []; jobs.clear(); acknowledgements.clear(); validations.clear(); invalidatedRuns.clear(); setupRecordId = ''; setupStep = 0; reportError = ''; }
    modeLabel.textContent = state.client ? `${state.client.owner}/${state.client.repo}` : '尚未连接仓库';
    replace(banner, reportError ? notice('warning', '未能读取活动报告', reportError) : report && !report.ledger.available ? notice('warning', '同步台账不可用', '请查看运行日志，确认台账是否读取成功。') : state.client && !report ? notice('info', '尚无活动报告', '完成一次运行后刷新报告。使用旧版同步程序的仓库需先升级。') : null);
    render(); refreshManualScope();
  }
  async function refresh() {
    if (!state.client) { go('settings'); settingsTab = 'deployment'; render(); toast('请先连接专用私有仓库。', 'error'); return; }
    await actions.refresh();
  }
  function captureValidation() {
    if (!report?.run.record_id) return;
    const evidence = simulationEvidence(report, runs, report.run.record_id, enabledTargets(state.snapshot));
    if (evidence && !invalidatedRuns.has(evidence)) validations.set(validationKey(report.run.record_id), evidence);
  }
  function setRuns(value) { runs = value; requestGeneration++; jobs.clear(); captureValidation(); render(); }
  function setReport(value, error = '') { if (!error) report = value; captureValidation(); reportError = error ? `${error}${report ? ' 当前保留上次成功读取的报告，请按报告时间判断。' : ''}` : ''; update(); }
  const shell = el('div', { class: 'shell' }, [sidebar, el('div', { class: 'workspace' }, [el('header', { class: 'topbar' }, [el('div', { class: 'topbar-instance' }, [el('span', { class: 'mobile-brand', 'aria-hidden': 'true', text: '»' }), el('div', { class: 'instance-copy' }, [el('strong', { class: 'mobile-brand-name', text: 'OneLap Sync' }), modeLabel])]), el('div', { class: 'topbar-actions' }, [old.pill, repositoryButton])]), main, el('footer', { class: 'footer' }, [old.status])])]);
  app.replaceChildren(el('a', { class: 'skip-link', href: '#main-content', text: '跳到主要内容' }), shell, storage, old.oauth, drawer, forceDialog);
  window.addEventListener('hashchange', routeFromHash);
  routeFromHash(); update();
  return { update, setRuns, setReport, invalidateValidation: () => { if (report?.run.id) invalidatedRuns.add(String(report.run.id)); for (const evidence of validations.values()) invalidatedRuns.add(evidence); validations.clear(); acknowledgements.clear(); }, confirmForce, lock, reset: () => { requestGeneration++; report = null; runs = []; jobs.clear(); acknowledgements.clear(); validations.clear(); invalidatedRuns.clear(); setupRecordId = ''; setupStep = 0; if (forceDialog.open) forceDialog.close(); closeDrawer(); update(); } };
}
