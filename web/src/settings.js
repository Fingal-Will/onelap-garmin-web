const MAX_SECRET_BYTES = 48 * 1024;
const encoder = new TextEncoder();

function text(value) {
  return typeof value === 'string' ? value : String(value ?? '');
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function formValue(form, key) {
  return isObject(form) ? form[key] : undefined;
}

function requireToken(object, key, label) {
  if (typeof object[key] !== 'string' || !object[key].trim()) {
    throw new Error(`Garmin 会话缺少 ${label}。请导入完整的会话文件。`);
  }
}

/** Parse the format emitted by uploader/garmin-session.js; never echo input on errors. */
export function parseGarminSession(value) {
  const source = text(value).trim();
  if (!source || encoder.encode(source).length > 128 * 1024) {
    throw new Error('Garmin 会话文件为空或超过 128 KB。');
  }
  let session;
  try {
    session = JSON.parse(source);
  } catch {
    throw new Error('Garmin 会话必须是有效的 JSON 文件。');
  }
  if (!isObject(session) || !isObject(session.oauth1) || !isObject(session.oauth2)) {
    throw new Error('Garmin 会话必须同时包含 oauth1 和 oauth2 对象。');
  }
  requireToken(session.oauth1, 'oauth_token', 'oauth1.oauth_token');
  requireToken(session.oauth1, 'oauth_token_secret', 'oauth1.oauth_token_secret');
  requireToken(session.oauth2, 'access_token', 'oauth2.access_token');
  requireToken(session.oauth2, 'refresh_token', 'oauth2.refresh_token');
  for (const part of [session.oauth1, session.oauth2]) {
    if (encoder.encode(JSON.stringify(part)).length > MAX_SECRET_BYTES) {
      throw new Error('Garmin 会话超过 GitHub Secret 的 48 KB 限制。');
    }
  }
  return { oauth1: session.oauth1, oauth2: session.oauth2 };
}

export function createSessionKey() {
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error('当前环境不支持安全随机数，请通过 HTTPS 打开控制台。');
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function integer(value, label, min, max, optional = false) {
  const normalized = text(value).trim();
  if (optional && !normalized) return '';
  if (!/^\d+$/.test(normalized)) throw new Error(`${label}必须是整数。`);
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`${label}必须在 ${min} 到 ${max} 之间。`);
  }
  return String(parsed);
}

/** Build a credential update. Blank secret fields mean preserve, never delete. */
export function buildSettings(form, existing = { secrets: [], variables: {} }) {
  if (!isObject(form)) throw new Error('设置表单格式无效，请刷新页面后重试。');
  if (!isObject(existing) || (existing.secrets !== undefined && !Array.isArray(existing.secrets))
    || (existing.variables !== undefined && !isObject(existing.variables))) {
    throw new Error('已保存的仓库设置格式无效，请重新连接 GitHub 后重试。');
  }
  const knownSecrets = new Set((existing.secrets ?? []).filter((name) => typeof name === 'string'));
  const oldVariables = existing.variables ?? {};
  const secrets = {};
  const addSecret = (name, value, trim = true) => {
    const source = text(value);
    if (!source.trim()) return;
    const normalized = trim ? source.trim() : source;
    if (encoder.encode(normalized).length > MAX_SECRET_BYTES) {
      throw new Error(`${name} 超过 GitHub Secret 的 48 KB 限制。`);
    }
    secrets[name] = normalized;
  };
  const hasSecret = (name) => Object.hasOwn(secrets, name) || knownSecrets.has(name);

  addSecret('ONELAP_ACCOUNT', formValue(form, 'onelapAccount'));
  addSecret('ONELAP_PASSWORD', formValue(form, 'onelapPassword'), false);
  addSecret('ONELAP_TOKEN', formValue(form, 'onelapToken'));
  if (!hasSecret('ONELAP_TOKEN') && !(hasSecret('ONELAP_ACCOUNT') && hasSecret('ONELAP_PASSWORD'))) {
    throw new Error('请填写顽鹿账号和密码，或填写有效的顽鹿 Token。已保存的凭据可留空。');
  }

  const targets = text(formValue(form, 'targets') || oldVariables.SYNC_TARGETS || 'CN,GLOBAL').trim();
  if (!['CN', 'GLOBAL', 'CN,GLOBAL'].includes(targets)) {
    throw new Error('请选择中国区、国际区或双区同步。');
  }
  for (const [region, value] of [['CN', formValue(form, 'garminCnSession')], ['GLOBAL', formValue(form, 'garminGlobalSession')]]) {
    if (text(value).trim()) {
      const session = parseGarminSession(value);
      addSecret(`GARMIN_${region}_OAUTH1`, JSON.stringify(session.oauth1));
      addSecret(`GARMIN_${region}_OAUTH2`, JSON.stringify(session.oauth2));
    }
    if (targets.split(',').includes(region)) {
      if (!hasSecret(`GARMIN_${region}_OAUTH1`) || !hasSecret(`GARMIN_${region}_OAUTH2`)) {
        throw new Error(`请导入 Garmin ${region === 'CN' ? '中国区' : '国际区'}的完整会话文件。`);
      }
    }
  }

  if (text(formValue(form, 'unitId')).trim()) {
    secrets.GARMIN_DEVICE_UNIT_ID = integer(formValue(form, 'unitId'), '设备 Unit ID', 1_000_000_000, 4_294_967_295);
  }
  if (!hasSecret('GARMIN_DEVICE_UNIT_ID')) throw new Error('请填写你自己的 Garmin 设备 Unit ID。');

  const variables = {
    GARMINIZE_FIT_ENABLED: 'true',
    FIT_COORDINATE_TRANSFORM_ENABLED: formValue(form, 'coordinateTransform') === true ? 'true' : 'false',
    GARMIN_DEVICE_PRODUCT_ID: integer(
      text(formValue(form, 'productId')).trim() || oldVariables.GARMIN_DEVICE_PRODUCT_ID,
      '设备 Product ID', 1, 65535,
    ),
    GARMIN_DEVICE_SOFTWARE_VERSION: integer(formValue(form, 'softwareVersion'), '设备固件版本', 0, 65535, true),
    SYNC_TARGETS: targets,
  };

  if (text(formValue(form, 'sessionKey')).trim()) {
    if (encoder.encode(text(formValue(form, 'sessionKey')).trim()).length < 32) {
      throw new Error('顽鹿会话加密密钥至少需要 32 字节。');
    }
    addSecret('ONELAP_SESSION_KEY', formValue(form, 'sessionKey'));
  } else if (!knownSecrets.has('ONELAP_SESSION_KEY')) {
    secrets.ONELAP_SESSION_KEY = createSessionKey();
  }
  return { secrets, variables };
}
