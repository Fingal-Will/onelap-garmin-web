// Explicit opt-in fixtures, never used as a fallback for a real account.
export const demoSnapshot = {
  installed: true, manifest: { version: '1.3.0' },
  variables: { SYNC_TARGETS: 'CN,GLOBAL', STRAVA_SYNC_ENABLED: 'true', SYNC_ENABLED: 'true', ONELAP_DISCOVERY_PAGES: '10', SYNC_MAX_RETRIES: '8' },
  secrets: ['ONELAP_ACCOUNT', 'ONELAP_PASSWORD', 'ONELAP_SESSION_KEY', 'GARMIN_CN_OAUTH1', 'GARMIN_CN_OAUTH2', 'GARMIN_GLOBAL_OAUTH1', 'GARMIN_GLOBAL_OAUTH2', 'STRAVA_CLIENT_ID', 'STRAVA_CLIENT_SECRET', 'STRAVA_REFRESH_TOKEN', 'STRAVA_SESSION_KEY'],
};
const names = ['周末耐力骑行', '午间恢复骑', '夜间训练', '晨间轻松骑', '节奏训练', '河畔骑行', '周末长距离', '城市晨骑', '室内间歇训练', '山地骑行', '下班恢复骑', '清晨有氧骑'];
const items = names.map((name, index) => ({
  id: `DEMO-1025-${String(index + 1).padStart(4, '0')}`, name,
  activity_time: `2026-10-${String(25 - index).padStart(2, '0')}T00:00:00Z`,
  download_status: 'success', processing_status: 'not_requested',
  targets: Object.fromEntries(['CN', 'GLOBAL', 'STRAVA'].map((target, t) => [target, {
    status: index === 0 && target === 'GLOBAL' ? 'auth_required' : index === 1 && target === 'GLOBAL' ? 'failed' : index === 2 && target === 'STRAVA' ? 'pending' : index === 0 && target === 'STRAVA' ? 'duplicate' : 'success',
    attempts: 1, remote_activity_id: index < 2 && target === 'GLOBAL' || index === 2 && target === 'STRAVA' ? '' : `${900000 + index * 3 + t}`,
    reconciliation_status: target === 'STRAVA' || index >= 10 ? '' : index === 0 && target === 'GLOBAL' ? 'unverified' : index === 1 && target === 'CN' ? 'missing' : 'present',
    reconciliation_checked_at: target === 'STRAVA' || index >= 10 ? '' : '2026-10-26T02:22:00Z', updated_at: '2026-10-26T02:22:00Z',
    next_retry_at: index === 1 && target === 'GLOBAL' ? '2026-10-26T03:22:00Z' : '',
  }])) ,
}));
export const demoReport = {
  schema_version: 2, updated_at: '2026-10-26T02:30:00Z',
  run: { id: 'demo-run', status: 'success', dry_run: false, targets: ['CN', 'GLOBAL', 'STRAVA'] },
  ledger: { available: true, activities: 12,
    uploads: ['CN', 'GLOBAL', 'STRAVA'].flatMap(target => [...new Set(items.map(row => row.targets[target].status))].map(status => ({ target_region: target, status, count: items.filter(row => row.targets[target].status === status).length }))),
    reconciliation: [{ target_region: 'CN', status: 'present', count: 9 }, { target_region: 'CN', status: 'missing', count: 1 }, { target_region: 'GLOBAL', status: 'present', count: 9 }, { target_region: 'GLOBAL', status: 'unverified', count: 1 }],
  }, activity_items: items,
};
export const demoRuns = [
  { id: 'demo-run', name: 'OneLap 同步', event: 'schedule', status: 'completed', conclusion: 'success', created_at: '2026-10-26T02:22:00Z', updated_at: '2026-10-26T02:23:00Z' },
  { id: 'demo-ledger-failure', name: '台账保存异常 · 独立演示场景', event: 'workflow_dispatch', status: 'completed', conclusion: 'failure', created_at: '2026-10-25T02:22:00Z', updated_at: '2026-10-25T02:23:00Z' },
];
