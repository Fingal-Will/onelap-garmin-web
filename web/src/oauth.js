import { parseGarminSession } from './settings.js';

const REQUEST_ID_BYTES = 16;
const RESULT_KEY_BYTES = 32;
const AES_GCM_IV_BYTES = 12;
const DEFAULT_TIMEOUT_MS = 25 * 60 * 1000;
const DEFAULT_POLL_INTERVAL_MS = 5_000;

function asObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function requireCrypto() {
  if (!globalThis.crypto?.getRandomValues || !globalThis.crypto?.subtle) {
    throw new Error('当前环境不支持安全的 Garmin OAuth 获取，请通过 HTTPS 打开控制台。');
  }
  return globalThis.crypto;
}

function bytesToBase64(bytes) {
  let text = '';
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

function base64ToBytes(value, label) {
  if (typeof value !== 'string' || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
    throw new Error(`${label}不是有效的 Base64 数据。`);
  }
  try {
    return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
  } catch {
    throw new Error(`${label}不是有效的 Base64 数据。`);
  }
}

function safeRegion(value) {
  const region = String(value ?? '').toUpperCase();
  if (!['CN', 'GLOBAL'].includes(region)) throw new Error('请选择 Garmin 中国区或国际区。');
  return region;
}

function validateRequestId(value) {
  if (typeof value !== 'string' || !/^[A-F0-9]{32}$/.test(value)) {
    throw new Error('Garmin OAuth 请求编号无效。');
  }
  return value;
}

function validateClient(client) {
  const methods = [
    'beginGarminOAuth', 'getGarminOAuthRun', 'readGarminOAuthResult',
    'saveGarminOAuthSession', 'deleteGarminOAuthResult', 'deleteGarminOAuthTemporarySecrets',
  ];
  if (!client || methods.some((name) => typeof client[name] !== 'function')) {
    throw new Error('GitHub OAuth 客户端不可用，请重新连接私有仓库。');
  }
}

function validatePolling(timeoutMs, pollIntervalMs, sleepImpl) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 30 * 60 * 1_000
    || !Number.isSafeInteger(pollIntervalMs) || pollIntervalMs < 100 || pollIntervalMs > 30_000
    || typeof sleepImpl !== 'function') {
    throw new Error('Garmin OAuth 轮询参数无效。');
  }
}

function resultPayload(value, requestId, region) {
  let payload;
  try {
    payload = JSON.parse(value);
  } catch {
    throw new Error('Garmin OAuth 结果文件不是有效的 JSON。');
  }
  if (!asObject(payload) || payload.schema_version !== 1 || payload.request_id !== requestId
    || payload.region !== region || !asObject(payload.encryption)
    || payload.encryption.algorithm !== 'AES-256-GCM' || typeof payload.encryption.nonce !== 'string'
    || typeof payload.encryption.tag !== 'string' || typeof payload.ciphertext !== 'string') {
    throw new Error('Garmin OAuth 结果文件与当前请求不匹配。');
  }
  const iv = base64ToBytes(payload.encryption.nonce, 'Garmin OAuth 初始化向量');
  if (iv.length !== AES_GCM_IV_BYTES) throw new Error('Garmin OAuth 初始化向量长度无效。');
  const ciphertext = base64ToBytes(payload.ciphertext, 'Garmin OAuth 密文');
  const tag = base64ToBytes(payload.encryption.tag, 'Garmin OAuth 认证标签');
  if (tag.length !== 16) throw new Error('Garmin OAuth 认证标签长度无效。');
  const encrypted = new Uint8Array(ciphertext.length + tag.length);
  encrypted.set(ciphertext);
  encrypted.set(tag, ciphertext.length);
  return { iv, ciphertext: encrypted };
}

async function decryptSession(content, requestId, region, keyBytes) {
  const { iv, ciphertext } = resultPayload(content, requestId, region);
  let plaintext;
  try {
    const key = await requireCrypto().subtle.importKey('raw', keyBytes, { name: 'AES-GCM' }, false, ['decrypt']);
    const additionalData = new TextEncoder().encode(`garmin-oauth-result:1:${region}:${requestId}`);
    plaintext = await requireCrypto().subtle.decrypt({ name: 'AES-GCM', iv, additionalData, tagLength: 128 }, key, ciphertext);
  } catch {
    throw new Error('无法解密 Garmin OAuth 结果。请重新获取会话。');
  }
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(plaintext);
  } catch {
    throw new Error('Garmin OAuth 结果不是有效的 UTF-8 数据。');
  }
  const session = parseGarminSession(text);
  return { oauth1: JSON.stringify(session.oauth1), oauth2: JSON.stringify(session.oauth2) };
}

function wait(milliseconds) {
  return new Promise((resolve) => { window.setTimeout(resolve, milliseconds); });
}

/** Generate a request handle that is safe for Secret names and workflow inputs. */
export function createGarminOAuthRequest() {
  const crypto = requireCrypto();
  const requestBytes = crypto.getRandomValues(new Uint8Array(REQUEST_ID_BYTES));
  const resultKey = crypto.getRandomValues(new Uint8Array(RESULT_KEY_BYTES));
  return {
    requestId: Array.from(requestBytes, (byte) => byte.toString(16).padStart(2, '0')).join('').toUpperCase(),
    resultKey,
    resultKeyBase64: bytesToBase64(resultKey),
  };
}

/**
 * Exchange one region's Garmin account/password for OAuth secrets through the user's private Actions runner.
 * The caller owns the form fields and should clear them as soon as this promise is created.
 */
export async function authorizeGarmin({
  client,
  region,
  username,
  password,
  onProgress = () => {},
  sleepImpl = wait,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  nowImpl = Date.now,
} = {}) {
  validateClient(client);
  const target = safeRegion(region);
  if (typeof username !== 'string' || !username.trim() || typeof password !== 'string' || !password) {
    throw new Error('请填写 Garmin 账号和密码。');
  }
  if (typeof onProgress !== 'function' || typeof nowImpl !== 'function') throw new Error('Garmin OAuth 回调无效。');
  validatePolling(timeoutMs, pollIntervalMs, sleepImpl);

  const request = createGarminOAuthRequest();
  const deadline = nowImpl() + timeoutMs;
  let operationError = null;
  let authorizationResult = null;
  try {
    onProgress('正在加密保存临时 Garmin 凭据…');
    try {
      await client.beginGarminOAuth({
        requestId: request.requestId,
        region: target,
        username,
        password,
        resultKey: request.resultKeyBase64,
      }, onProgress);
    } finally {
      username = '';
      password = '';
      request.resultKeyBase64 = '';
    }

    for (;;) {
      const run = await client.getGarminOAuthRun(request.requestId);
      if (run?.status === 'completed' && run.conclusion !== 'success') {
        throw new Error('Garmin OAuth 获取任务失败。请在 GitHub Actions 中查看运行详情后重试。');
      }
      const result = await client.readGarminOAuthResult(request.requestId);
      if (result) {
        onProgress('正在验证并加密保存 Garmin OAuth 会话…');
        const session = await decryptSession(result.content, request.requestId, target, request.resultKey);
        await client.saveGarminOAuthSession(target, session, onProgress);
        onProgress('Garmin OAuth 会话已保存，正在清理临时凭据…');
        authorizationResult = { requestId: request.requestId, region: target, cleanupWarning: null };
        return authorizationResult;
      }
      if (nowImpl() >= deadline) throw new Error('等待 Garmin OAuth 结果超时。临时凭据将被清理。');
      onProgress(run ? 'Garmin OAuth 任务正在运行…' : '正在等待 GitHub Actions 创建 Garmin OAuth 任务…');
      await sleepImpl(pollIntervalMs);
    }
  } catch (error) {
    operationError = error;
    throw error;
  } finally {
    const cleanup = await Promise.allSettled([
      client.deleteGarminOAuthResult(request.requestId),
      client.deleteGarminOAuthTemporarySecrets(request.requestId),
    ]);
    const failures = cleanup.filter((entry) => entry.status === 'rejected');
    request.resultKey.fill(0);
    if (failures.length) {
      const cleanupError = new Error('Garmin OAuth 已结束，但部分临时凭据或结果文件未能清理。请重新连接后执行残留清理。');
      if (operationError) operationError.cleanupError = cleanupError;
      else if (authorizationResult) authorizationResult.cleanupWarning = cleanupError.message;
      else throw cleanupError;
    }
  }
}

/** Remove only strictly named OAuth temporary Secrets left by an interrupted request. */
export async function cleanupGarminOAuthSecrets(client, options) {
  if (!client || typeof client.cleanupGarminOAuthSecrets !== 'function') {
    throw new Error('GitHub OAuth 客户端不可用，请重新连接私有仓库。');
  }
  return client.cleanupGarminOAuthSecrets(options);
}
