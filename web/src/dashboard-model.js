import { resolveSyncTargets } from './settings.js';

export const TARGETS = ['CN', 'GLOBAL', 'STRAVA'];
export const TARGET_NAMES = { CN: 'Garmin 中国区', GLOBAL: 'Garmin 国际区', STRAVA: 'Strava' };
export const STATUS = {
  success: ['已完成', 'success', 'check'], duplicate: ['已存在', 'success', 'refresh'],
  auth_required: ['需要授权', 'warning', 'key'], failed: ['等待重试', 'warning', 'refresh'],
  pending: ['待处理', 'neutral', 'clock'], uploading: ['平台处理中', 'purple', 'clock'],
  unverified: ['未能核实', 'purple', 'warning'], ambiguous: ['匹配歧义', 'purple', 'warning'],
  missing: ['明确缺失', 'error', 'warning'], present: ['确认存在', 'success', 'check'],
  disabled: ['未启用', 'neutral', 'close'], unknown: ['暂无证据', 'neutral', 'clock'],
  exhausted: ['已达重试上限', 'error', 'warning'],
};
export function enabledTargets(snapshot) {
  const vars = snapshot.variables || {};
  const regions = resolveSyncTargets(vars.SYNC_TARGETS || 'AUTO', snapshot.secrets || []);
  return [...(regions ? regions.split(',') : []), ...(vars.STRAVA_SYNC_ENABLED === 'true' ? ['STRAVA'] : [])];
}
export function isComplete(upload) {
  return ['success', 'duplicate'].includes(upload?.status);
}
export function activityComplete(activity, targets) {
  return targets.length > 0 && targets.every(target => isComplete(activity.targets?.[target]));
}
export function uploadState(upload, enabled = true, maxRetries = 8) {
  if (!enabled) return 'disabled';
  if (!upload) return 'unknown';
  if (['failed', 'auth_required'].includes(upload.status) && upload.attempts >= maxRetries) return 'exhausted';
  if (upload.status === 'auth_required') return 'auth_required';
  if (['unverified', 'ambiguous'].includes(upload.reconciliation_status) && !isComplete(upload) && upload.status !== 'uploading') return upload.reconciliation_status;
  return Object.hasOwn(STATUS, upload.status) ? upload.status : 'unknown';
}
export function formatTime(value, compact = false) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return '尚无记录';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', ...(compact ? {} : { year: 'numeric' }), month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}
export function filterActivities(rows, { query = '', target = '', status = '', tab = 'all', days = 0, now = Date.now() } = {}, targets = [], maxRetries = 8) {
  const needle = query.trim().toLocaleLowerCase();
  return rows.filter(row => {
    if (needle && !`${row.name} ${row.id}`.toLocaleLowerCase().includes(needle)) return false;
    const complete = activityComplete(row, targets);
    if (tab === 'attention' && complete || tab === 'complete' && !complete) return false;
    if (days && (!row.activity_time || new Date(row.activity_time).getTime() < now - days * 86400000)) return false;
    const scope = target ? [target] : targets;
    if (target && !row.targets?.[target]) return false;
    return !status || scope.some(key => uploadState(row.targets?.[key], targets.includes(key), maxRetries) === status);
  });
}
export function summarize(report, targets) {
  const counts = (report?.ledger?.uploads || []).filter(row => targets.includes(row.target_region));
  const total = counts.reduce((sum, row) => sum + row.count, 0);
  const complete = counts.filter(row => ['success', 'duplicate'].includes(row.status)).reduce((sum, row) => sum + row.count, 0);
  return { activities: report?.ledger?.activities ?? 0, total, complete, pending: total - complete };
}
export function configCompleteness(snapshot) {
  const names = new Set(snapshot.secrets || []);
  const has = name => names.has(name);
  return {
    ONELAP: has('ONELAP_TOKEN') || has('ONELAP_ACCOUNT') && has('ONELAP_PASSWORD'),
    CN: has('GARMIN_CN_OAUTH1') && has('GARMIN_CN_OAUTH2'),
    GLOBAL: has('GARMIN_GLOBAL_OAUTH1') && has('GARMIN_GLOBAL_OAUTH2'),
    STRAVA: ['STRAVA_CLIENT_ID', 'STRAVA_CLIENT_SECRET', 'STRAVA_REFRESH_TOKEN', 'STRAVA_SESSION_KEY'].every(has),
  };
}
export function manualImpact(selection, snapshot, dryRun = false) {
  const vars = snapshot.variables || {};
  const strategy = selection === 'DEFAULT' ? vars.SYNC_TARGETS || 'AUTO' : selection;
  const regions = resolveSyncTargets(strategy, snapshot.secrets || []);
  return { targets: [...(regions ? regions.split(',') : []), ...(vars.STRAVA_SYNC_ENABLED === 'true' ? ['STRAVA'] : [])], dryRun: dryRun || vars.SYNC_DRY_RUN === 'true' };
}
// Reports contain only allowlisted metadata. Never spread source_json or error payloads into UI state.
export function normalizeReport(input) {
  if (!input || ![1, 2].includes(input.schema_version) || !input.ledger || !Array.isArray(input.ledger.uploads)) throw new Error('活动报告格式不受支持，请更新私有仓库同步程序。');
  const text = (value, max = 200) => typeof value === 'string' ? value.slice(0, max) : '';
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
  const timestamp = value => Number.isFinite(Date.parse(value)) ? text(value, 40) : '';
  const identifier = value => /^[\w-]{1,100}$/.test(String(value || '')) ? String(value) : '';
  const items = (Array.isArray(input.activity_items) ? input.activity_items : []).slice(0, 200).filter(row => row && typeof row === 'object').map(row => ({
    id: identifier(row.id), name: text(row.name) || 'OneLap 骑行活动', activity_time: timestamp(row.activity_time),
    download_status: ['pending', 'downloading', 'success', 'failed'].includes(row.download_status) ? row.download_status : 'unknown',
    processing_status: ['pending', 'processing', 'success', 'failed', 'not_requested', 'skipped'].includes(row.processing_status) ? row.processing_status : 'unknown',
    fit_sha256: /^[a-f0-9]{64}$/i.test(row.fit_sha256 || '') ? row.fit_sha256 : '',
    targets: Object.fromEntries(TARGETS.filter(key => row.targets?.[key]).map(key => {
      const upload = row.targets[key];
      return [key, { status: ['pending', 'uploading', 'success', 'duplicate', 'failed', 'auth_required'].includes(upload.status) ? upload.status : 'unknown', attempts: count(upload.attempts),
        remote_activity_id: identifier(upload.remote_activity_id), next_retry_at: timestamp(upload.next_retry_at),
        updated_at: timestamp(upload.updated_at), uploaded_at: timestamp(upload.uploaded_at),
        reconciliation_status: ['present', 'missing', 'ambiguous', 'unverified'].includes(upload.reconciliation_status) ? upload.reconciliation_status : '',
        reconciliation_checked_at: timestamp(upload.reconciliation_checked_at) }];
    })),
  })).filter(row => row.id);
  const groups = rows => (Array.isArray(rows) ? rows : []).filter(row => row && TARGETS.includes(row.target_region) && Object.hasOwn(STATUS, row.status)).map(row => ({ target_region: row.target_region, status: row.status, count: count(row.count) }));
  return { schema_version: input.schema_version, updated_at: timestamp(input.updated_at),
    run: { id: identifier(input.run?.id), record_id: identifier(input.run?.record_id), status: text(input.run?.status), dry_run: input.run?.dry_run === true, targets: (Array.isArray(input.run?.targets) ? input.run.targets : []).filter(key => TARGETS.includes(key)) },
    ledger: { available: input.ledger.available === true, activities: count(input.ledger.activities), uploads: groups(input.ledger.uploads), reconciliation: groups(input.ledger.reconciliation) },
    activity_items: items, activity_limit: 200 };
}
export function ledgerSaveState(jobs) {
  const steps = (jobs || []).flatMap(job => job.steps || []);
  const persist = steps.find(step => step.name === '保存台账与汇总');
  if (!persist) return 'unknown';
  if (persist.conclusion === 'success') return 'saved';
  if (persist.conclusion === 'failure') return 'failed';
  return persist.status === 'completed' ? 'unknown' : 'pending';
}

// A checkbox cannot establish that a simulation ran. Match the saved report to its completed Action.
export function simulationEvidence(report, runs, recordId, targets) {
  if (!recordId || !targets.length || !report?.ledger?.available || report.run?.record_id !== recordId || !report.run.dry_run || report.run.status !== 'success') return '';
  const run = runs.find(item => String(item.id) === String(report.run.id));
  const activity = report.activity_items?.find(item => item.id === recordId);
  if (run?.status !== 'completed' || run.conclusion !== 'success' || activity?.download_status !== 'success') return '';
  if (!['success', 'not_requested', 'skipped'].includes(activity.processing_status)) return '';
  if (!targets.every(target => report.run.targets?.includes(target))) return '';
  return String(run.id);
}
