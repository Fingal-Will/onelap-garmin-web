import assert from 'node:assert/strict';
import test from 'node:test';

import { adaptSettingsForRunner, buildSettings, createSessionKey, parseGarminSession, requireSyncTargets, resolveSyncTargets } from '../web/src/settings.js';

const defaultVariables = { STRAVA_SYNC_ENABLED: 'false', ONELAP_DISCOVERY_PAGES: '10', RECONCILE_RECENT_LIMIT: '20', SYNC_MAX_RETRIES: '8', SYNC_DRY_RUN: 'false' };

const cnSession = JSON.stringify({
  oauth1: { oauth_token: 'cn-oauth1', oauth_token_secret: 'cn-secret' },
  oauth2: { access_token: 'cn-access', refresh_token: 'cn-refresh' },
});
const globalSession = JSON.stringify({
  oauth1: { oauth_token: 'global-oauth1', oauth_token_secret: 'global-secret' },
  oauth2: { access_token: 'global-access', refresh_token: 'global-refresh' },
});

function baseForm(overrides = {}) {
  return {
    onelapToken: 'onelap-token',
    garminCnSession: cnSession,
    garminGlobalSession: globalSession,
    unitId: '1234567890',
    productId: '3122',
    softwareVersion: '975',
    targets: 'CN,GLOBAL',
    coordinateTransform: true,
    ...overrides,
  };
}

test('parses a complete Garmin session without retaining its source text', () => {
  assert.deepEqual(parseGarminSession(cnSession), JSON.parse(cnSession));
  assert.throws(
    () => parseGarminSession('{"oauth1":{"oauth_token":"private-value"}}'),
    /oauth2/,
  );
  assert.throws(() => parseGarminSession('not json'), /有效的 JSON/);
});

test('generates a cryptographically random 32-byte session key as hexadecimal', () => {
  const first = createSessionKey();
  const second = createSessionKey();
  assert.match(first, /^[0-9a-f]{64}$/);
  assert.match(second, /^[0-9a-f]{64}$/);
  assert.notEqual(first, second);
});

test('builds encrypted-secret and variable updates from a complete new form', () => {
  const settings = buildSettings(baseForm());
  assert.deepEqual(JSON.parse(settings.secrets.GARMIN_CN_OAUTH1), JSON.parse(cnSession).oauth1);
  assert.deepEqual(JSON.parse(settings.secrets.GARMIN_GLOBAL_OAUTH2), JSON.parse(globalSession).oauth2);
  assert.equal(settings.secrets.GARMIN_DEVICE_UNIT_ID, '1234567890');
  assert.match(settings.secrets.ONELAP_SESSION_KEY, /^[0-9a-f]{64}$/);
  assert.deepEqual(settings.variables, {
    GARMINIZE_FIT_ENABLED: 'true',
    FIT_COORDINATE_TRANSFORM_ENABLED: 'true',
    GARMIN_DEVICE_PRODUCT_ID: '3122',
    GARMIN_DEVICE_SOFTWARE_VERSION: '975',
    SYNC_TARGETS: 'CN,GLOBAL',
    ...defaultVariables,
  });
});

test('preserves blank credentials when matching secrets are already saved', () => {
  const existing = {
    secrets: [
      'ONELAP_TOKEN', 'GARMIN_GLOBAL_OAUTH1', 'GARMIN_GLOBAL_OAUTH2',
      'GARMIN_DEVICE_UNIT_ID', 'ONELAP_SESSION_KEY',
    ],
    variables: { GARMIN_DEVICE_PRODUCT_ID: '3122', SYNC_TARGETS: 'GLOBAL' },
  };
  const settings = buildSettings(baseForm({
    onelapToken: '', garminCnSession: '', garminGlobalSession: '', unitId: '',
    productId: '', softwareVersion: '', targets: '', sessionKey: '', coordinateTransform: false,
  }), existing);
  assert.deepEqual(settings.secrets, {});
  assert.deepEqual(settings.variables, {
    GARMINIZE_FIT_ENABLED: 'true',
    FIT_COORDINATE_TRANSFORM_ENABLED: 'false',
    GARMIN_DEVICE_PRODUCT_ID: '3122',
    GARMIN_DEVICE_SOFTWARE_VERSION: '',
    SYNC_TARGETS: 'GLOBAL',
    ...defaultVariables,
  });
});

test('rejects incomplete credentials, malformed forms, and unsafe device values', () => {
  assert.throws(() => buildSettings(null), /设置表单格式无效/);
  assert.throws(() => buildSettings(baseForm({ garminGlobalSession: '' }), {
    secrets: [], variables: {},
  }), /国际区/);
  assert.throws(() => buildSettings(baseForm({ unitId: '999999999' })), /Unit ID/);
  assert.throws(() => buildSettings(baseForm({ productId: '65536' })), /Product ID/);
  assert.throws(() => buildSettings(baseForm(), { secrets: 'not-an-array', variables: {} }), /仓库设置格式无效/);
});

const globalNames = ['GARMIN_GLOBAL_OAUTH1', 'GARMIN_GLOBAL_OAUTH2'];
const cnNames = ['GARMIN_CN_OAUTH1', 'GARMIN_CN_OAUTH2'];

test('auto uses only complete configured regions and does not require China credentials', () => {
  assert.equal(resolveSyncTargets('AUTO', globalNames), 'GLOBAL');
  assert.equal(resolveSyncTargets('AUTO', cnNames), 'CN');
  assert.equal(resolveSyncTargets('AUTO', [...globalNames, ...cnNames]), 'CN,GLOBAL');
  assert.equal(resolveSyncTargets('AUTO', [...globalNames, 'GARMIN_CN_OAUTH1']), 'GLOBAL');
  assert.equal(resolveSyncTargets('AUTO', []), '');
  assert.throws(() => requireSyncTargets('AUTO', []), /至少配置一个/);
  assert.throws(() => resolveSyncTargets('unknown', globalNames), /请选择/);
});

test('new forms default to AUTO and save an international-only account without China secrets', () => {
  const settings = buildSettings(baseForm({ targets: '', garminCnSession: '' }));
  assert.equal(settings.variables.SYNC_TARGETS, 'AUTO');
  assert.equal(Object.hasOwn(settings.secrets, 'GARMIN_CN_OAUTH1'), false);
  assert.equal(Object.hasOwn(settings.secrets, 'GARMIN_GLOBAL_OAUTH2'), true);
  assert.equal(buildSettings(baseForm({ targets: 'GLOBAL', garminCnSession: '' })).variables.SYNC_TARGETS, 'GLOBAL');
});

test('changing from both regions to international preserves stored China secrets without requiring them', () => {
  const existing = { secrets: globalNames, variables: { SYNC_TARGETS: 'CN,GLOBAL' } };
  const settings = buildSettings(baseForm({ targets: 'GLOBAL', garminCnSession: '', garminGlobalSession: '' }), existing);
  assert.equal(settings.variables.SYNC_TARGETS, 'GLOBAL');
  assert.equal(Object.hasOwn(settings.secrets, 'GARMIN_CN_OAUTH1'), false);
  assert.throws(() => buildSettings(baseForm({ targets: '', garminCnSession: '', garminGlobalSession: '' }), existing), /中国区/);
  assert.throws(() => requireSyncTargets('CN,GLOBAL', globalNames), /中国区/);
  assert.equal(requireSyncTargets('GLOBAL', [...globalNames, 'GARMIN_CN_OAUTH1']), 'GLOBAL');
});

test('AUTO without any complete account fails instead of saving an unusable default', () => {
  assert.throws(() => buildSettings(baseForm({ targets: 'AUTO', garminCnSession: '', garminGlobalSession: '' })), /至少配置一个/);
});


test('optional Strava needs complete credentials and generates its own encryption key once', () => {
  const form = baseForm({ stravaEnabled: true, stravaClientId: '123', stravaClientSecret: 'client-secret', stravaRefreshToken: 'refresh' });
  const update = buildSettings(form);
  assert.equal(update.variables.STRAVA_SYNC_ENABLED, 'true');
  assert.match(update.secrets.STRAVA_SESSION_KEY, /^[0-9a-f]{64}$/);
  assert.notEqual(update.secrets.STRAVA_SESSION_KEY, update.secrets.ONELAP_SESSION_KEY);
  assert.throws(() => buildSettings({ ...form, stravaRefreshToken: '' }), /STRAVA_REFRESH_TOKEN/);
  assert.throws(() => buildSettings({ ...form, stravaClientId: 'not-a-number' }), /Client ID/);
  assert.throws(() => buildSettings({ ...form, stravaSessionKey: 'short' }), /至少需要 32/);
  assert.equal(Object.hasOwn(buildSettings(baseForm()).secrets, 'STRAVA_SESSION_KEY'), false);
});

test('Strava reauthorization preserves the encryption key and disabling preserves saved tokens', () => {
  const existing = { secrets: ['STRAVA_CLIENT_ID', 'STRAVA_CLIENT_SECRET', 'STRAVA_REFRESH_TOKEN', 'STRAVA_SESSION_KEY'], variables: { STRAVA_SYNC_ENABLED: 'true' } };
  const update = buildSettings(baseForm({ stravaEnabled: true, stravaRefreshToken: 'new-refresh' }), existing);
  assert.equal(update.secrets.STRAVA_REFRESH_TOKEN, 'new-refresh');
  assert.equal(Object.hasOwn(update.secrets, 'STRAVA_SESSION_KEY'), false);
  const disabled = buildSettings(baseForm({ stravaEnabled: false }), existing);
  assert.equal(disabled.variables.STRAVA_SYNC_ENABLED, 'false');
  assert.equal(Object.keys(disabled.secrets).some((key) => key.startsWith('STRAVA_')), false);
});

test('validates saved run settings and retains them when omitted by callers', () => {
  const update = buildSettings(baseForm({ discoveryPages: '0', recentLimit: '200', maxRetries: '16', globalDryRun: true }));
  assert.equal(update.variables.ONELAP_DISCOVERY_PAGES, '0');
  assert.equal(update.variables.RECONCILE_RECENT_LIMIT, '200');
  assert.equal(update.variables.SYNC_MAX_RETRIES, '16');
  assert.equal(update.variables.SYNC_DRY_RUN, 'true');
  const preserved = buildSettings(baseForm(), { secrets: [], variables: update.variables });
  assert.equal(preserved.variables.ONELAP_DISCOVERY_PAGES, '0');
  assert.equal(preserved.variables.SYNC_DRY_RUN, 'true');
  for (const form of [{ discoveryPages: '-1' }, { recentLimit: '201' }, { maxRetries: '0' }, { discoveryPages: '10\nSYNC_DRY_RUN=false' }]) {
    assert.throws(() => buildSettings(baseForm(form)), /必须/);
  }
});

test('legacy runners can save old settings but cannot silently accept unsupported new features', () => {
  const update = buildSettings(baseForm());
  const legacy = adaptSettingsForRunner(update, { features: ['default-sync-targets'] });
  assert.equal(Object.hasOwn(legacy.variables, 'ONELAP_DISCOVERY_PAGES'), false);
  assert.equal(Object.hasOwn(legacy.variables, 'STRAVA_SYNC_ENABLED'), false);
  assert.throws(() => adaptSettingsForRunner(buildSettings(baseForm({ maxRetries: '12' })), {}), /升级/);
  assert.throws(() => adaptSettingsForRunner(buildSettings(baseForm({ stravaClientId: '123' })), {}), /Strava/);
  const modern = { features: ['shared-run-settings', 'strava-sync'] };
  assert.deepEqual(adaptSettingsForRunner(update, modern), update);
});
