"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  fetchActivities,
  readActivities,
  normalizeActivity,
} = require("../uploader/garmin-activities");

test("normalizes only fields needed for reconciliation", () => {
  assert.deepEqual(
    normalizeActivity({
      activityId: 123,
      activityName: "Morning Ride",
      startTimeLocal: "2026-08-25 07:00:00",
      activityType: { typeKey: "cycling" },
      distance: 30000,
      duration: 3600,
      ignored: "value",
    }),
    {
      activityId: "123",
      activityName: "Morning Ride",
      startTimeLocal: "2026-08-25 07:00:00",
      activityType: "cycling",
      distanceMeters: 30000,
      durationSeconds: 3600,
    },
  );
});

test("does not treat GMT or missing metrics as complete local data", () => {
  assert.deepEqual(
    normalizeActivity({
      activityId: 123,
      startTimeGMT: "2026-08-24 23:00:00",
      distance: null,
      duration: "",
    }),
    {
      activityId: "123",
      activityName: "",
      startTimeLocal: null,
      activityType: null,
      distanceMeters: null,
      durationSeconds: null,
    },
  );
});

test("paginates a bounded Garmin date query", async () => {
  const calls = [];
  const client = {
    async getActivities(...args) {
      calls.push(args);
      if (calls.length === 1) {
        return [
          { activityId: 1, distance: 1000, duration: 100 },
          { activityId: 2, distance: 2000, duration: 200 },
        ];
      }
      return [{ activityId: 3, distance: 3000, duration: 300 }];
    },
  };

  const result = await fetchActivities(
    client,
    "2026-08-24",
    "2026-08-26",
    2,
    10,
  );

  assert.equal(result.truncated, false);
  assert.deepEqual(
    result.activities.map((activity) => activity.activityId),
    ["1", "2", "3"],
  );
  assert.equal(calls[0][0], 0);
  assert.equal(calls[1][0], 2);
  assert.equal(calls[0][7], "2026-08-24");
  assert.equal(calls[0][8], "2026-08-26");
});

test("marks a full maximum-sized result as truncated", async () => {
  const client = {
    async getActivities() {
      return [
        { activityId: 1, distance: 1000, duration: 100 },
        { activityId: 2, distance: 2000, duration: 200 },
      ];
    },
  };

  const result = await fetchActivities(
    client,
    "2026-08-24",
    "2026-08-26",
    2,
    2,
  );

  assert.equal(result.truncated, true);
});

for (const [method, stage] of [
  ["loadToken", "load_token"],
  ["getUserProfile", "user_profile"],
  ["getActivities", "activities"],
]) {
  test(`identifies ${stage} failures without retrying or logging credentials`, async () => {
    const calls = [];
    const events = [];
    const failure = Object.assign(new Error("Network error or unknown error occurred"), {
      code: "ECONNRESET",
      response: { status: 503, data: { access_token: "private-token" } },
      config: { headers: { Authorization: "private-token" } },
    });
    const client = Object.fromEntries(
      ["loadToken", "getUserProfile", "getActivities"].map((name) => [
        name,
        async () => {
          calls.push(name);
          if (name === method) throw failure;
          return name === "getActivities" ? [] : { username: "private-user" };
        },
      ]),
    );
    await assert.rejects(
      readActivities(client, { token: "private-token" }, {}, "2026-10-01", "2026-10-23",
        (event) => events.push(event)),
      (error) => error === failure,
    );
    assert.equal(calls.filter((name) => name === method).length, 1);
    assert.equal(calls.at(-1), method);
    const event = events.at(-1);
    assert.equal(event.stage, stage);
    assert.equal(event.status, "failed");
    assert.equal(event.httpStatus, 503);
    assert.equal(event.code, "ECONNRESET");
    assert.ok(event.elapsedMs >= 0);
    assert.ok(!JSON.stringify(events).includes("private-"));
  });
}

test("reports successful stages and each activity page without exposing activity data", async () => {
  const events = [];
  let pages = 0;
  const client = {
    async loadToken() {},
    async getUserProfile() { return { username: "private-user" }; },
    async getActivities() {
      pages += 1;
      return pages === 1
        ? Array.from({ length: 100 }, (_, index) => ({ activityId: index, activityName: "private-ride" }))
        : [];
    },
  };
  const result = await readActivities(client, {}, {}, "2026-10-01", "2026-10-23",
    (event) => events.push(event));
  assert.equal(result.activities.length, 100);
  assert.equal(result.truncated, false);
  assert.deepEqual(events.map(({ stage, status }) => [stage, status]), [
    ["load_token", "started"], ["load_token", "completed"],
    ["user_profile", "started"], ["user_profile", "completed"],
    ["activities", "started"], ["activities", "completed"],
    ["activities", "started"], ["activities", "completed"],
  ]);
  assert.equal(events[5].count, 100);
  assert.equal(events[6].start, 100);
  assert.equal(events[7].count, 0);
  assert.ok(!JSON.stringify(events).includes("private-"));
});

test("reports an invalid activity response as a query stage failure", async () => {
  const events = [];
  await assert.rejects(fetchActivities({ async getActivities() { return {}; } },
    "2026-10-01", "2026-10-23", 100, 5000, (event) => events.push(event)), /不是数组/);
  assert.equal(events.at(-1).stage, "activities");
  assert.equal(events.at(-1).status, "failed");
});
