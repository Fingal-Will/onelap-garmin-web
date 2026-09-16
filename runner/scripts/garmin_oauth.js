"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const REQUEST_ID_PATTERN = /^[A-F0-9]{32}$/;
const RESULT_VERSION = 1;
const RESULT_DIRECTORY = path.join("data", "oauth-results");
const MAX_OAUTH_PART_BYTES = 48 * 1024;

class OAuthResultError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function requiredString(value, code) {
  if (typeof value !== "string" || !value) throw new OAuthResultError(code);
  return value;
}

function validateContext(environment) {
  const region = requiredString(environment.GARMIN_OAUTH_REGION, "invalid_input");
  const requestId = requiredString(
    environment.GARMIN_OAUTH_REQUEST_ID,
    "invalid_input",
  );
  if (!new Set(["CN", "GLOBAL"]).has(region) || !REQUEST_ID_PATTERN.test(requestId)) {
    throw new OAuthResultError("invalid_input");
  }
  const username = requiredString(environment.GARMIN_OAUTH_USERNAME, "missing_credentials");
  const password = requiredString(environment.GARMIN_OAUTH_PASSWORD, "missing_credentials");
  const resultKey = parseResultKey(environment.GARMIN_OAUTH_RESULT_KEY);
  return { region, requestId, username, password, resultKey };
}

function parseResultKey(value) {
  if (typeof value !== "string" || !value) throw new OAuthResultError("invalid_key");
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new OAuthResultError("invalid_key");
  }
  const key = Buffer.from(value, "base64");
  if (key.length !== 32 || key.toString("base64") !== value) {
    throw new OAuthResultError("invalid_key");
  }
  return key;
}

function validateToken(part, field) {
  if (!part || typeof part !== "object" || Array.isArray(part)
    || typeof part[field] !== "string" || !part[field]) {
    throw new OAuthResultError("invalid_session");
  }
}

function validateSession(session) {
  if (!session || typeof session !== "object" || Array.isArray(session)) {
    throw new OAuthResultError("invalid_session");
  }
  validateToken(session.oauth1, "oauth_token");
  validateToken(session.oauth1, "oauth_token_secret");
  validateToken(session.oauth2, "access_token");
  validateToken(session.oauth2, "refresh_token");
  for (const part of [session.oauth1, session.oauth2]) {
    if (Buffer.byteLength(JSON.stringify(part), "utf8") > MAX_OAUTH_PART_BYTES) {
      throw new OAuthResultError("invalid_session");
    }
  }
  return { oauth1: session.oauth1, oauth2: session.oauth2 };
}

function associatedData(context) {
  return Buffer.from(`garmin-oauth-result:${RESULT_VERSION}:${context.region}:${context.requestId}`, "utf8");
}

function encryptSession(session, context) {
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", context.resultKey, nonce);
  cipher.setAAD(associatedData(context));
  const plaintext = Buffer.from(JSON.stringify(session), "utf8");
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  return {
    schema_version: RESULT_VERSION,
    request_id: context.requestId,
    region: context.region,
    encryption: {
      algorithm: "AES-256-GCM",
      nonce: nonce.toString("base64"),
      tag: cipher.getAuthTag().toString("base64"),
    },
    ciphertext: ciphertext.toString("base64"),
  };
}

function decryptSession(result, context) {
  if (!result || result.schema_version !== RESULT_VERSION || result.request_id !== context.requestId
    || result.region !== context.region || result.encryption?.algorithm !== "AES-256-GCM") {
    throw new OAuthResultError("invalid_result");
  }
  try {
    const decipher = crypto.createDecipheriv(
      "aes-256-gcm",
      context.resultKey,
      Buffer.from(result.encryption.nonce, "base64"),
    );
    decipher.setAAD(associatedData(context));
    decipher.setAuthTag(Buffer.from(result.encryption.tag, "base64"));
    return JSON.parse(Buffer.concat([
      decipher.update(Buffer.from(result.ciphertext, "base64")),
      decipher.final(),
    ]).toString("utf8"));
  } catch {
    throw new OAuthResultError("invalid_result");
  }
}

function resultPath(root, requestId) {
  if (!REQUEST_ID_PATTERN.test(requestId)) throw new OAuthResultError("invalid_input");
  return path.resolve(root, RESULT_DIRECTORY, `${requestId}.json`);
}

function writeEncryptedResult(root, result) {
  const target = resultPath(root, result.request_id);
  const directory = path.dirname(target);
  try {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const temporary = `${target}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(result)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    try {
      fs.linkSync(temporary, target);
    } catch (error) {
      if (error && error.code === "EEXIST") throw new OAuthResultError("result_exists");
      throw error;
    } finally {
      fs.rmSync(temporary, { force: true });
    }
    fs.chmodSync(target, 0o600);
    return target;
  } catch (error) {
    if (error instanceof OAuthResultError) throw error;
    throw new OAuthResultError("result_write_failed");
  }
}

async function quietly(operation) {
  const methods = ["log", "warn", "error", "info", "debug"];
  const original = Object.fromEntries(methods.map((method) => [method, console[method]]));
  for (const method of methods) console[method] = () => {};
  try {
    return await operation();
  } finally {
    for (const method of methods) console[method] = original[method];
  }
}

async function obtainSession(context, GarminConnect) {
  try {
    return await quietly(async () => {
      const client = context.region === "CN"
        ? new GarminConnect({ username: context.username, password: context.password }, "garmin.cn")
        : new GarminConnect({ username: context.username, password: context.password });
      await client.login();
      await client.getUserProfile();
      return validateSession(client.exportToken());
    });
  } catch (error) {
    if (error instanceof OAuthResultError) throw error;
    throw new OAuthResultError("authentication_failed");
  }
}

async function run(environment = process.env, dependencies = {}) {
  const context = validateContext(environment);
  const GarminConnect = dependencies.GarminConnect || require("@gooin/garmin-connect").GarminConnect;
  const root = dependencies.root || process.cwd();
  const session = await obtainSession(context, GarminConnect);
  const result = encryptSession(session, context);
  const output = writeEncryptedResult(root, result);
  return { code: "success", output };
}

async function main() {
  try {
    const result = await run();
    process.stdout.write(`GARMIN_OAUTH_RESULT=${result.code}\n`);
  } catch (error) {
    const code = error instanceof OAuthResultError ? error.code : "authentication_failed";
    process.stderr.write(`GARMIN_OAUTH_RESULT=${code}\n`);
    process.exitCode = 1;
  }
}

if (require.main === module) main();

module.exports = {
  OAuthResultError,
  RESULT_VERSION,
  decryptSession,
  encryptSession,
  parseResultKey,
  resultPath,
  run,
  validateContext,
  validateSession,
  writeEncryptedResult,
};
