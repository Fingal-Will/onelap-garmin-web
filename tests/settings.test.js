import assert from 'node:assert/strict';
import test from 'node:test';

import { buildSettings, createSessionKey, parseGarminSession } from '../web/src/settings.js';

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
