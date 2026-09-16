"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  fetchActivities,
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
