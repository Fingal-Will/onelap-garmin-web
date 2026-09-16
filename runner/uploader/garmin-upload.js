"use strict";

const fs = require("node:fs");
const path = require("node:path");

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const current = argv[index];
    if (current.startsWith("--")) {
      result[current.slice(2)] = argv[index + 1];
      index += 1;
    }
  }
  return result;
}

function parseSecret(value, name) {
  if (!value) {
    throw new Error(`缺少 ${name}`);
  }
  let text = value.trim();
  if (text.startsWith("base64:")) {
    text = Buffer.from(text.slice("base64:".length), "base64").toString("utf8");
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${name} 不是有效 JSON`);
  }
}

function envNames(region) {
  return region === "CN"
    ? {
        oauth1: "GARMIN_CN_OAUTH1",
        oauth2: "GARMIN_CN_OAUTH2",
        username: "GARMIN_CN_USERNAME",
        password: "GARMIN_CN_PASSWORD",
      }
    : {
        oauth1: "GARMIN_GLOBAL_OAUTH1",
        oauth2: "GARMIN_GLOBAL_OAUTH2",
        username: "GARMIN_GLOBAL_USERNAME",
        password: "GARMIN_GLOBAL_PASSWORD",
      };
}

function errorText(error) {
  const parts = [
    error && error.message,
    error && error.response && error.response.status,
    error && error.response && JSON.stringify(error.response.data),
  ].filter(Boolean);
  return parts.join(" | ") || String(error);
}

function isDuplicate(error) {
  const text = errorText(error).toLowerCase();
  const status = error && error.response && error.response.status;
  return (
    status === 409 ||
    /(^|\D)409(\D|$)/.test(text) ||
    text.includes("duplicate") ||
    text.includes("already exists") ||
    text.includes("already uploaded") ||
    text.includes("已存在") ||
    text.includes("重复")
  );
}

function isAuthError(error) {
  const text = errorText(error).toLowerCase();
  const status = error && error.response && error.response.status;
  return (
    status === 401 ||
    status === 403 ||
    text.includes("unauthorized") ||
    text.includes("forbidden") ||
    text.includes("oauth") ||
    text.includes("token expired") ||
    text.includes("login") ||
    text.includes("sign in") ||
    text.includes("authentication")
  );
}

function remoteActivityId(upload) {
  if (!upload || typeof upload !== "object") {
    return null;
  }
  return (
    upload.activityId ||
    upload.activityIdStr ||
    upload.id ||
    (upload.detailedImportResult &&
      (upload.detailedImportResult.activityId ||
        upload.detailedImportResult.uploadId ||
        (Array.isArray(upload.detailedImportResult.successes) &&
          upload.detailedImportResult.successes[0] &&
          upload.detailedImportResult.successes[0].internalId))) ||
    null
  );
}

function emit(payload) {
  process.stdout.write(
    `${JSON.stringify({ type: "garmin-upload-result", ...payload })}\n`,
  );
}

async function main() {
  const { GarminConnect } = require("@gooin/garmin-connect");
  const args = parseArgs(process.argv.slice(2));
  const region = String(args.region || "").toUpperCase();
  if (!["CN", "GLOBAL"].includes(region)) {
    throw new Error("--region 必须是 CN 或 GLOBAL");
  }
  if (!args.file) {
    throw new Error("缺少 --file");
  }

  const fitPath = path.resolve(args.file);
  if (!fs.existsSync(fitPath)) {
    throw new Error(`FIT 文件不存在: ${fitPath}`);
  }

  const names = envNames(region);
  const oauth1 = parseSecret(process.env[names.oauth1], names.oauth1);
  const oauth2 = parseSecret(process.env[names.oauth2], names.oauth2);
  const credentials = {
    // 1.8.x 构造器要求非空字段；实际认证仍只使用随后加载的 OAuth。
    username: process.env[names.username] || "oauth-session",
    password: process.env[names.password] || "oauth-session",
  };
  const client =
    region === "CN"
      ? new GarminConnect(credentials, "garmin.cn")
      : new GarminConnect(credentials);

  try {
    await client.loadToken(oauth1, oauth2);
    await client.getUserProfile();
    const upload = await client.uploadActivity(fitPath);
    emit({
      ok: true,
      duplicate: false,
      authRequired: false,
      region,
      remoteActivityId: remoteActivityId(upload),
      message: "上传成功",
    });
  } catch (error) {
    if (isDuplicate(error)) {
      emit({
        ok: true,
        duplicate: true,
        authRequired: false,
        region,
        remoteActivityId: null,
        message: errorText(error),
      });
      return;
    }
    emit({
      ok: false,
      duplicate: false,
      authRequired: isAuthError(error),
      region,
      remoteActivityId: null,
      message: errorText(error),
    });
    process.exitCode = 1;
  }
}

if (require.main === module) {
  main().catch((error) => {
    emit({
      ok: false,
      duplicate: false,
      authRequired: isAuthError(error),
      remoteActivityId: null,
      message: errorText(error),
    });
    process.exitCode = 1;
  });
}

module.exports = {
  isDuplicate,
  remoteActivityId,
};
