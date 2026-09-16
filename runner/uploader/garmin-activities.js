"use strict";

const PAGE_SIZE = 100;
const MAX_ACTIVITIES = 5000;

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

function normalizeActivity(activity) {
  const activityType =
    activity && activity.activityType && activity.activityType.typeKey
      ? activity.activityType.typeKey
      : activity && activity.activityType;
  return {
    activityId:
      activity && activity.activityId != null
        ? String(activity.activityId)
        : null,
    activityName: String((activity && activity.activityName) || ""),
    startTimeLocal:
      (activity && activity.startTimeLocal) || null,
    activityType: activityType || null,
    distanceMeters:
      activity &&
      activity.distance != null &&
      activity.distance !== "" &&
      Number.isFinite(Number(activity.distance))
        ? Number(activity.distance)
        : null,
    durationSeconds:
      activity &&
      activity.duration != null &&
      activity.duration !== "" &&
      Number.isFinite(Number(activity.duration))
        ? Number(activity.duration)
        : null,
  };
}

async function fetchActivities(
  client,
  startDate,
  endDate,
  pageSize = PAGE_SIZE,
  maximum = MAX_ACTIVITIES,
) {
  const activities = [];
  let start = 0;
  let truncated = false;
  while (activities.length < maximum) {
    const limit = Math.min(pageSize, maximum - activities.length);
    const page = await client.getActivities(
      start,
      limit,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      startDate,
      endDate,
    );
    if (!Array.isArray(page)) {
      throw new Error("Garmin 活动列表返回的不是数组");
    }
    activities.push(...page.map(normalizeActivity));
    if (page.length < limit) {
      break;
    }
    start += page.length;
    if (activities.length >= maximum) {
      truncated = true;
    }
  }
  return { activities, truncated };
}

function emit(payload) {
  process.stdout.write(
    `${JSON.stringify({ type: "garmin-activities-result", ...payload })}\n`,
  );
}

async function main() {
  const { GarminConnect } = require("@gooin/garmin-connect");
  const args = parseArgs(process.argv.slice(2));
  const region = String(args.region || "").toUpperCase();
  if (!["CN", "GLOBAL"].includes(region)) {
    throw new Error("--region 必须是 CN 或 GLOBAL");
  }
  if (!args["start-date"] || !args["end-date"]) {
    throw new Error("缺少 --start-date 或 --end-date");
  }

  const names = envNames(region);
  const oauth1 = parseSecret(process.env[names.oauth1], names.oauth1);
  const oauth2 = parseSecret(process.env[names.oauth2], names.oauth2);
  const credentials = {
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
    const result = await fetchActivities(
      client,
      args["start-date"],
      args["end-date"],
    );
    emit({
      ok: !result.truncated,
      authRequired: false,
      region,
      activities: result.activities,
      truncated: result.truncated,
      message: result.truncated
        ? `时间窗内活动超过安全上限 ${MAX_ACTIVITIES}，本次不判断缺失`
        : `读取到 ${result.activities.length} 条活动`,
    });
  } catch (error) {
    emit({
      ok: false,
      authRequired: isAuthError(error),
      region,
      activities: [],
      truncated: false,
      message: errorText(error),
    });
    process.exitCode = 1;
  }
}

if (require.main === module) {
  main().catch((error) => {
    emit({
      ok: false,
      authRequired: isAuthError(error),
      activities: [],
      truncated: false,
      message: errorText(error),
    });
    process.exitCode = 1;
  });
}

module.exports = {
  fetchActivities,
  normalizeActivity,
};
