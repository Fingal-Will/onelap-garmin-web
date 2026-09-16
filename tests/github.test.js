import assert from 'node:assert/strict';
import test from 'node:test';
import sodium from 'libsodium-wrappers';

import { GitHubClient } from '../web/src/github.js';

const sha = (character) => character.repeat(40);
const marker = { project: 'onelap-garmin-web', schema_version: 1, version: '0.1.0' };
const markerContent = btoa(JSON.stringify(marker));

function response(body, status = 200) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
  });
}

function repository(overrides = {}) {
  return { private: true, archived: false, disabled: false, permissions: { push: true }, default_branch: 'main', size: 1, ...overrides };
}

function createClient(router) {
  return new GitHubClient({
    token: 'github-token', owner: 'alice', repo: 'private-sync',
    fetchImpl: async (url, options) => router(new URL(url), options),
  });
}

function managedRouter(handler) {
  return (url, options) => {
    if (url.pathname === '/repos/alice/private-sync' && options.method === 'GET') return response(repository());
    if (url.pathname === '/repos/alice/private-sync/contents/.onelap-web.json') return response({ content: markerContent });
    return handler(url, options);
  };
}

test('rejects unsafe repository components and public repositories before writing', async () => {
  assert.throws(() => new GitHubClient({ token: 'x', owner: '../alice', repo: 'sync' }), /用户名/);
  assert.throws(() => new GitHubClient({ token: 'x\n', owner: 'alice', repo: 'sync' }), /Token/);
  const client = createClient((url) => {
    assert.equal(url.pathname, '/repos/alice/private-sync');
    return response(repository({ private: false }));
  });
  await assert.rejects(client.inspectRepository(), /私有仓库/);
});

test('dispatches only declared workflow inputs and encodes booleans as strings', async () => {
  const calls = [];
  const client = createClient(managedRouter((url, options) => {
    calls.push({ url, options });
    assert.equal(url.pathname, '/repos/alice/private-sync/actions/workflows/sync.yml/dispatches');
    assert.equal(options.method, 'POST');
    return response(undefined, 204);
  }));
  await client.dispatchWorkflow('sync.yml', {
    target_regions: 'CN', dry_run: true, full_scan: false, discovery_pages: '3', request_id: 'request_123',
    record_id: '6a8a861b62c3d1c91f03a22a',
  });
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    ref: 'main',
    inputs: { target_regions: 'CN', dry_run: 'true', full_scan: 'false', discovery_pages: '3', request_id: 'request_123', record_id: '6a8a861b62c3d1c91f03a22a' },
  });
  await assert.rejects(client.dispatchWorkflow('sync.yml', { force_reupload: true }), /活动编号/);
  await assert.rejects(client.dispatchWorkflow('sync.yml', { account: 'secret' }), /参数无效/);
});

test('encrypts secrets with the GitHub public key and updates existing variables', async () => {
  await sodium.ready;
  const keys = sodium.crypto_box_keypair();
  const calls = [];
  const client = createClient(managedRouter((url, options) => {
    calls.push({ url, options });
    if (url.pathname.endsWith('/actions/secrets/public-key')) {
      return response({ key_id: 'key-id', key: sodium.to_base64(keys.publicKey, sodium.base64_variants.ORIGINAL) });
    }
    if (url.pathname.endsWith('/actions/variables')) return response({ variables: [{ name: 'SYNC_ENABLED', value: 'false' }] });
    if (url.pathname.endsWith('/actions/secrets/ONELAP_TOKEN')) return response(undefined, 204);
    if (url.pathname.endsWith('/actions/variables/SYNC_ENABLED')) return response({ name: 'SYNC_ENABLED', value: 'true' });
    throw new Error(`unexpected ${options.method} ${url.pathname}`);
  }));
  const result = await client.saveSettings({ secrets: { ONELAP_TOKEN: 'plain-secret' }, variables: { SYNC_ENABLED: 'true' } });
  assert.deepEqual(result, { saved: ['ONELAP_TOKEN', 'SYNC_ENABLED'] });
  const secretCall = calls.find((call) => call.url.pathname.endsWith('/actions/secrets/ONELAP_TOKEN'));
  const secretBody = JSON.parse(secretCall.options.body);
  assert.equal(secretBody.key_id, 'key-id');
  assert.doesNotMatch(secretCall.options.body, /plain-secret/);
  const opened = sodium.crypto_box_seal_open(
    sodium.from_base64(secretBody.encrypted_value, sodium.base64_variants.ORIGINAL), keys.publicKey, keys.privateKey,
  );
  assert.equal(sodium.to_string(opened), 'plain-secret');
  const variableCall = calls.find((call) => call.url.pathname.endsWith('/actions/variables/SYNC_ENABLED'));
  assert.equal(variableCall.options.method, 'PATCH');
});

test('allows an empty optional Garmin software version variable', async () => {
  const calls = [];
  const client = createClient(managedRouter((url, options) => {
    calls.push({ url, options });
    if (url.pathname.endsWith('/actions/variables') && options.method === 'GET') {
      return response({ total_count: 0, variables: [] });
    }
    if (url.pathname.endsWith('/actions/variables') && options.method === 'POST') {
      return response({ name: 'GARMIN_DEVICE_SOFTWARE_VERSION', value: '' }, 201);
    }
    throw new Error(`unexpected ${options.method} ${url.pathname}`);
  }));
  const result = await client.saveSettings({ variables: { GARMIN_DEVICE_SOFTWARE_VERSION: '' } });
  assert.deepEqual(result.saved, ['GARMIN_DEVICE_SOFTWARE_VERSION']);
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), {
    name: 'GARMIN_DEVICE_SOFTWARE_VERSION', value: '',
  });
});

test('cleans only strictly named residual Garmin OAuth Secrets and result refs', async () => {
  const deleted = [];
  const id = 'A'.repeat(32);
  const client = createClient(managedRouter((url, options) => {
    if (url.pathname.endsWith('/actions/secrets') && options.method === 'GET') {
      return response({ secrets: [
        { name: `GARMIN_OAUTH_${id}_USERNAME`, updated_at: '2025-01-01T00:00:00Z' },
        { name: 'GARMIN_OAUTH_not-a-request_PASSWORD' },
        { name: 'GARMIN_CN_OAUTH1' },
      ] });
    }
    if (url.pathname.endsWith('/actions/workflows/garmin-session.yml/runs') && options.method === 'GET') {
      return response({ workflow_runs: [{ display_title: id, status: 'completed' }] });
    }
    if (url.pathname.endsWith('/git/matching-refs/heads/oauth-result-')) {
      return response([
        { ref: `refs/heads/oauth-result-${id}` },
        { ref: 'refs/heads/oauth-result-not-a-request' },
        { ref: 'refs/heads/main' },
      ]);
    }
    if (options.method === 'DELETE') {
      deleted.push(url.pathname);
      return response(undefined, 204);
    }
    throw new Error(`unexpected ${options.method} ${url.pathname}`);
  }));
  const result = await client.cleanupGarminOAuthSecrets();
  assert.deepEqual(result, {
    secrets: [`GARMIN_OAUTH_${id}_USERNAME`],
    refs: [`refs/heads/oauth-result-${id}`],
    protectedRequestIds: [],
  });
  assert.deepEqual(deleted, [
    `/repos/alice/private-sync/actions/secrets/GARMIN_OAUTH_${id}_USERNAME`,
    `/repos/alice/private-sync/git/refs/heads/oauth-result-${id}`,
  ]);
});

test('protects active and newly created Garmin OAuth resources during residual cleanup', async () => {
  const activeId = 'B'.repeat(32);
  const recentId = 'C'.repeat(32);
  const staleId = 'D'.repeat(32);
  const deleted = [];
  const client = createClient(managedRouter((url, options) => {
    if (url.pathname.endsWith('/actions/secrets') && options.method === 'GET') {
      return response({ secrets: [
        { name: `GARMIN_OAUTH_${activeId}_PASSWORD`, updated_at: '2025-01-01T00:00:00Z' },
        { name: `GARMIN_OAUTH_${recentId}_PASSWORD`, updated_at: '2999-01-01T00:00:00Z' },
        { name: `GARMIN_OAUTH_${staleId}_PASSWORD`, updated_at: '2025-01-01T00:00:00Z' },
      ] });
    }
    if (url.pathname.endsWith('/actions/workflows/garmin-session.yml/runs') && options.method === 'GET') {
      assert.equal(url.searchParams.get('event'), 'workflow_dispatch');
      return response({ workflow_runs: [{ display_title: activeId, status: 'in_progress' }] });
    }
    if (url.pathname.endsWith('/git/matching-refs/heads/oauth-result-')) {
      return response([
        { ref: `refs/heads/oauth-result-${activeId}` },
        { ref: `refs/heads/oauth-result-${recentId}` },
        { ref: `refs/heads/oauth-result-${staleId}` },
      ]);
    }
    if (options.method === 'DELETE') {
      deleted.push(url.pathname);
      return response(undefined, 204);
    }
    throw new Error(`unexpected ${options.method} ${url.pathname}`);
  }));

  const result = await client.cleanupGarminOAuthSecrets();
  assert.deepEqual(result, {
    secrets: [`GARMIN_OAUTH_${staleId}_PASSWORD`],
    refs: [`refs/heads/oauth-result-${staleId}`],
    protectedRequestIds: [activeId, recentId],
  });
  assert.deepEqual(deleted, [
    `/repos/alice/private-sync/actions/secrets/GARMIN_OAUTH_${staleId}_PASSWORD`,
    `/repos/alice/private-sync/git/refs/heads/oauth-result-${staleId}`,
  ]);
});

test('reads and removes an OAuth result only through its fixed orphan ref', async () => {
  const id = 'B'.repeat(32);
  const calls = [];
  const client = createClient(managedRouter((url, options) => {
    calls.push({ url, options });
    if (url.pathname === '/repos/alice/private-sync/contents/result.json' && options.method === 'GET') {
      assert.equal(url.searchParams.get('ref'), `oauth-result-${id}`);
      return response({ encoding: 'base64', content: btoa('{"ciphertext":"placeholder"}') });
    }
    if (url.pathname === `/repos/alice/private-sync/git/refs/heads/oauth-result-${id}` && options.method === 'DELETE') {
      return response(undefined, 204);
    }
    throw new Error(`unexpected ${options.method} ${url.pathname}`);
  }));
  const result = await client.readGarminOAuthResult(id);
  assert.deepEqual(result, { ref: `oauth-result-${id}`, content: '{"ciphertext":"placeholder"}' });
  assert.equal(await client.deleteGarminOAuthResult(id), true);
  assert.equal(calls.some((call) => call.url.pathname.includes('/data/oauth-results/')), false);
});

test('initializes an empty private repository and publishes one non-forced install commit', async () => {
  const requests = [];
  let refReads = 0;
  let blobCount = 0;
  const client = createClient((url, options) => {
    requests.push({ url, options });
    if (url.pathname === '/repos/alice/private-sync' && options.method === 'GET') return response(repository({ size: 0 }));
    if (url.pathname === '/repos/alice/private-sync/git/ref/heads/main' && options.method === 'GET') {
      refReads += 1;
      return refReads === 1 ? response({ message: 'not found' }, 404) : response({ object: { sha: sha('a') } });
    }
    if (url.pathname === '/repos/alice/private-sync/contents/README.md') return response({ commit: { sha: sha('i') } }, 201);
    if (url.pathname === `/repos/alice/private-sync/git/commits/${sha('a')}` && options.method === 'GET') return response({ tree: { sha: sha('b') } });
    if (url.pathname === `/repos/alice/private-sync/git/trees/${sha('b')}` && options.method === 'GET') return response({ truncated: false, tree: [{ path: 'README.md', type: 'blob', mode: '100644' }] });
    if (url.pathname === '/repos/alice/private-sync/git/blobs') {
      blobCount += 1;
      return response({ sha: sha(String(blobCount)) }, 201);
    }
    if (url.pathname === '/repos/alice/private-sync/git/trees' && options.method === 'POST') return response({ sha: sha('c') }, 201);
    if (url.pathname === '/repos/alice/private-sync/git/commits' && options.method === 'POST') return response({ sha: sha('d') }, 201);
    if (url.pathname === '/repos/alice/private-sync/git/refs/heads/main' && options.method === 'PATCH') return response({ object: { sha: sha('d') } });
    throw new Error(`unexpected ${options.method} ${url.pathname}${url.search}`);
  });
  const bundle = {
    version: marker.version,
    files: [
      { path: '.onelap-web.json', content: markerContent },
      { path: '.github/workflows/sync.yml', content: btoa('name: sync\n') },
      { path: '.github/workflows/reconcile.yml', content: btoa('name: reconcile\n') },
      { path: '.github/workflows/garmin-session.yml', content: btoa('name: Garmin OAuth\n') },
    ],
  };
  const url = await client.installRunner(bundle);
  assert.equal(url, `https://github.com/alice/private-sync/commit/${sha('d')}`);
  assert.equal(blobCount, 4);
  const patch = requests.find((request) => request.url.pathname.endsWith('/git/refs/heads/main') && request.options.method === 'PATCH');
  assert.deepEqual(JSON.parse(patch.options.body), { sha: sha('d'), force: false });
});

test('refuses to install into a repository that has unrecognized files', async () => {
  const client = createClient((url, options) => {
    if (url.pathname === '/repos/alice/private-sync') return response(repository());
    if (url.pathname.endsWith('/git/ref/heads/main')) return response({ object: { sha: sha('a') } });
    if (url.pathname.endsWith(`/git/commits/${sha('a')}`)) return response({ tree: { sha: sha('b') } });
    if (url.pathname.endsWith(`/git/trees/${sha('b')}`)) return response({ truncated: false, tree: [{ path: 'data.txt', type: 'blob', mode: '100644' }] });
    throw new Error(`unexpected write ${options.method} ${url.pathname}`);
  });
  const bundle = {
    version: marker.version,
    files: [
      { path: '.onelap-web.json', content: markerContent },
      { path: '.github/workflows/sync.yml', content: btoa('sync') },
      { path: '.github/workflows/reconcile.yml', content: btoa('reconcile') },
      { path: '.github/workflows/garmin-session.yml', content: btoa('oauth') },
    ],
  };
  await assert.rejects(client.installRunner(bundle), /包含其他文件/);
});
