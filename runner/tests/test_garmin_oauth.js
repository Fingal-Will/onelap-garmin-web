"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  decryptSession,
  encryptSession,
  run,
  validateContext,
  validateSession,
} = require("../scripts/garmin_oauth");

const key = Buffer.alloc(32, 7).toString("base64");
const baseEnvironment = {
  GARMIN_OAUTH_REGION: "CN",
  GARMIN_OAUTH_REQUEST_ID: "0123456789ABCDEF0123456789ABCDEF",
  GARMIN_OAUTH_USERNAME: "user@example.com",
  GARMIN_OAUTH_PASSWORD: "password",
  GARMIN_OAUTH_RESULT_KEY: key,
};
const session = {
  oauth1: { oauth_token: "oauth1-token", oauth_token_secret: "oauth1-secret" },
  oauth2: { access_token: "oauth2-access", refresh_token: "oauth2-refresh" },
};

const workflow = fs.readFileSync(
  path.join(__dirname, "..", ".github", "workflows", "garmin-session.yml"),
  "utf8",
);

test("validates and encrypts a complete OAuth session with request-bound authentication", () => {
  const context = validateContext(baseEnvironment);
  const result = encryptSession(validateSession(session), context);
  assert.equal(result.schema_version, 1);
  assert.equal(result.request_id, baseEnvironment.GARMIN_OAUTH_REQUEST_ID);
  assert.equal(result.region, "CN");
  assert.equal(result.encryption.algorithm, "AES-256-GCM");
  assert.doesNotMatch(result.ciphertext, /oauth1-token|oauth2-access/);
  assert.deepEqual(decryptSession(result, context), session);
  assert.throws(
    () => decryptSession(result, { ...context, region: "GLOBAL" }),
    /invalid_result/,
  );
});

test("rejects invalid request IDs, keys, and incomplete OAuth exports", () => {
  assert.throws(
    () => validateContext({ ...baseEnvironment, GARMIN_OAUTH_REQUEST_ID: "not-safe" }),
    /invalid_input/,
  );
  assert.throws(
    () => validateContext({ ...baseEnvironment, GARMIN_OAUTH_RESULT_KEY: "short" }),
    /invalid_key/,
  );
  assert.throws(
    () => validateSession({ oauth1: {}, oauth2: {} }),
    /invalid_session/,
  );
});

test("keeps repository credentials out of the Garmin login step", () => {
  assert.match(
    workflow,
    /group: garmin-oauth-\$\{\{ github\.repository \}\}-\$\{\{ inputs\.request_id \}\}/,
  );
  assert.doesNotMatch(workflow, /group: onelap-garmin-ledger/);
  assert.match(workflow, /persist-credentials: false/);
  assert.doesNotMatch(workflow, /git pull\b/);

  const obtainStart = workflow.indexOf("- name: 获取并加密 Garmin OAuth 会话");
  const publishStart = workflow.indexOf("- name: 发布临时加密 OAuth 结果");
  assert.ok(obtainStart >= 0 && publishStart > obtainStart);
  assert.doesNotMatch(workflow.slice(obtainStart, publishStart), /github\.token|GITHUB_TOKEN|REPOSITORY_TOKEN/);
  assert.equal(workflow.match(/\$\{\{ github\.token \}\}/g)?.length, 1);

  const publishStep = workflow.slice(publishStart);
  assert.match(publishStep, /REPOSITORY_TOKEN: \$\{\{ github\.token \}\}/);
  assert.match(publishStep, /http\.https:\/\/github\.com\/\.extraheader/);
  assert.match(publishStep, /trap cleanup_git_credentials EXIT/);
  assert.match(publishStep, /git ls-remote --exit-code "\$remote" "\$ref"/);
  assert.match(publishStep, /git push "\$remote" "\$commit:\$ref"/);
});

test("writes only encrypted output and reports external login errors generically", async () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "garmin-oauth-test-"));
  class FakeGarminConnect {
    constructor(credentials, domain) {
      this.credentials = credentials;
      this.domain = domain;
    }

    async login() {}
    async getUserProfile() {}
    exportToken() { return session; }
  }
  try {
    const result = await run(baseEnvironment, { GarminConnect: FakeGarminConnect, root: temporary });
    const output = fs.readFileSync(result.output, "utf8");
    assert.equal(result.code, "success");
    assert.doesNotMatch(output, /oauth1-token|oauth2-access/);
    assert.deepEqual(
      decryptSession(JSON.parse(output), validateContext(baseEnvironment)),
      session,
    );
    await assert.rejects(
      run(baseEnvironment, {
        root: temporary,
        GarminConnect: class {
          async login() { throw new Error("response contained a token"); }
        },
      }),
      /result_exists|authentication_failed/,
    );
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
