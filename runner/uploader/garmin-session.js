"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { GarminConnect } = require("@gooin/garmin-connect");

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

function namesFor(region) {
  return region === "CN"
    ? {
        username: "GARMIN_CN_USERNAME",
        password: "GARMIN_CN_PASSWORD",
      }
    : {
        username: "GARMIN_GLOBAL_USERNAME",
        password: "GARMIN_GLOBAL_PASSWORD",
      };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const region = String(args.region || "").toUpperCase();
  if (!["CN", "GLOBAL"].includes(region)) {
    throw new Error("--region 必须是 CN 或 GLOBAL");
  }
  if (!args.output) {
    throw new Error("缺少 --output");
  }

  const names = namesFor(region);
  const username = process.env[names.username] || "";
  const password = process.env[names.password] || "";
  if (!username || !password) {
    throw new Error(`缺少 ${names.username} 或 ${names.password}`);
  }

  const client =
    region === "CN"
      ? new GarminConnect({ username, password }, "garmin.cn")
      : new GarminConnect({ username, password });
  await client.login();
  await client.getUserProfile();
  const session = client.exportToken();
  if (!session || !session.oauth1 || !session.oauth2) {
    throw new Error("Garmin 登录成功，但没有导出完整 OAuth 会话");
  }

  const outputPath = path.resolve(args.output);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${JSON.stringify(session, null, 2)}\n`, {
    mode: 0o600,
  });
  fs.chmodSync(outputPath, 0o600);
  process.stdout.write(`OAuth 会话已写入 ${outputPath}\n`);
  process.stdout.write("该文件包含敏感凭据，不要提交到 Git。\n");
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
