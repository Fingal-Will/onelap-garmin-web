import assert from 'node:assert/strict';
import test from 'node:test';

import { authorizeGarmin, createGarminOAuthRequest } from '../web/src/oauth.js';

const requestId = 'A'.repeat(32);

function base64(bytes) {
  let text = '';
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

async function encryptedResult({ region = 'CN', key, id = requestId } = {}) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const additionalData = new TextEncoder().encode(`garmin-oauth-result:1:${region}:${id}`);
  const aesKey = await crypto.subtle.importKey('raw', key, { name: 'AES-GCM' }, false, ['encrypt']);
  const session = JSON.stringify({
    oauth1: { oauth_token: 'oauth1', oauth_token_secret: 'oauth1-secret' },
    oauth2: { access_token: 'access', refresh_token: 'refresh' },
  });
  const encrypted = new Uint8Array(await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv, additionalData, tagLength: 128 }, aesKey, new TextEncoder().encode(session),
  ));
  return JSON.stringify({
    schema_version: 1,
    request_id: id,
    region,
    encryption: {
      algorithm: 'AES-256-GCM',
      nonce: base64(iv),
      tag: base64(encrypted.slice(-16)),
    },
    ciphertext: base64(encrypted.slice(0, -16)),
  });
}

test('creates independent uppercase 128-bit request IDs and AES-256 result keys', () => {
  const first = createGarminOAuthRequest();
  const second = createGarminOAuthRequest();
  assert.match(first.requestId, /^[A-F0-9]{32}$/);
  assert.notEqual(first.requestId, second.requestId);
  assert.equal(first.resultKey.length, 32);
  assert.equal(atob(first.resultKeyBase64).length, 32);
});

test('authorizes Garmin from an encrypted orphan-ref result and always removes temporary resources', async () => {
  let clock = 0;
  let reads = 0;
  const calls = { begin: [], saved: [], resultDeletes: [], secretDeletes: [] };
  const client = {
    async beginGarminOAuth(input) {
      calls.begin.push(input);
      calls.result = await encryptedResult({
        region: input.region, id: input.requestId,
        key: Uint8Array.from(atob(input.resultKey), (character) => character.charCodeAt(0)),
      });
    },
    async getGarminOAuthRun() { return reads ? { id: 7, status: 'completed', conclusion: 'success' } : null; },
    async readGarminOAuthResult() { reads += 1; return reads === 1 ? null : { content: calls.result }; },
    async saveGarminOAuthSession(region, session) { calls.saved.push({ region, session }); },
    async deleteGarminOAuthResult(id) { calls.resultDeletes.push(id); },
    async deleteGarminOAuthTemporarySecrets(id) { calls.secretDeletes.push(id); },
  };
  const result = await authorizeGarmin({
    client, region: 'CN', username: 'garmin-user', password: 'garmin-password',
    sleepImpl: async () => { clock += 1_000; }, nowImpl: () => clock, pollIntervalMs: 1_000, timeoutMs: 10_000,
  });
  assert.equal(result.region, 'CN');
  assert.match(result.requestId, /^[A-F0-9]{32}$/);
  assert.deepEqual(calls.begin[0].region, 'CN');
  assert.equal(calls.begin[0].username, 'garmin-user');
  assert.equal(calls.saved.length, 1);
  assert.equal(calls.saved[0].region, 'CN');
  assert.deepEqual(JSON.parse(calls.saved[0].session.oauth1), { oauth_token: 'oauth1', oauth_token_secret: 'oauth1-secret' });
  assert.deepEqual(calls.resultDeletes, [result.requestId]);
  assert.deepEqual(calls.secretDeletes, [result.requestId]);
});

test('reports incomplete cleanup as a warning after the permanent OAuth secrets were saved', async () => {
  let content;
  let saved = false;
  const client = {
    async beginGarminOAuth(input) {
      content = await encryptedResult({
        region: input.region, id: input.requestId,
        key: Uint8Array.from(atob(input.resultKey), (character) => character.charCodeAt(0)),
      });
    },
    async getGarminOAuthRun() { return { id: 8, status: 'completed', conclusion: 'success' }; },
    async readGarminOAuthResult() { return { content }; },
    async saveGarminOAuthSession() { saved = true; },
    async deleteGarminOAuthResult() { throw new Error('temporary ref delete failed'); },
    async deleteGarminOAuthTemporarySecrets() {},
  };

  const result = await authorizeGarmin({
    client, region: 'GLOBAL', username: 'u', password: 'p', sleepImpl: async () => {}, timeoutMs: 10_000,
  });
  assert.equal(saved, true);
  assert.equal(result.region, 'GLOBAL');
  assert.match(result.cleanupWarning, /临时凭据或结果文件未能清理/);
});

test('cleans temporary resources after a failed workflow without exposing credentials', async () => {
  const removed = [];
  const client = {
    async beginGarminOAuth() {},
    async getGarminOAuthRun() { return { id: 9, status: 'completed', conclusion: 'failure' }; },
    async readGarminOAuthResult() { throw new Error('must not read a failed result'); },
    async saveGarminOAuthSession() { throw new Error('must not save a failed result'); },
    async deleteGarminOAuthResult(id) { removed.push(`result:${id}`); },
    async deleteGarminOAuthTemporarySecrets(id) { removed.push(`secret:${id}`); },
  };
  await assert.rejects(authorizeGarmin({
    client, region: 'GLOBAL', username: 'u', password: 'p', sleepImpl: async () => {}, timeoutMs: 10_000,
  }), /获取任务失败/);
  assert.equal(removed.length, 2);
  assert.match(removed[0], /^result:[A-F0-9]{32}$/);
  assert.match(removed[1], /^secret:[A-F0-9]{32}$/);
});

test('rejects a result whose authenticated context does not match the active region', async () => {
  const removed = [];
  let content;
  const client = {
    async beginGarminOAuth(input) {
      content = await encryptedResult({
        region: 'GLOBAL', id: input.requestId,
        key: Uint8Array.from(atob(input.resultKey), (character) => character.charCodeAt(0)),
      });
    },
    async getGarminOAuthRun() { return { id: 10, status: 'completed', conclusion: 'success' }; },
    async readGarminOAuthResult() { return { content }; },
    async saveGarminOAuthSession() { throw new Error('must not save mismatched session'); },
    async deleteGarminOAuthResult() { removed.push('result'); },
    async deleteGarminOAuthTemporarySecrets() { removed.push('secret'); },
  };
  await assert.rejects(authorizeGarmin({
    client, region: 'CN', username: 'u', password: 'p', sleepImpl: async () => {}, timeoutMs: 10_000,
  }), /结果文件与当前请求不匹配|无法解密/);
  assert.deepEqual(removed, ['result', 'secret']);
});
