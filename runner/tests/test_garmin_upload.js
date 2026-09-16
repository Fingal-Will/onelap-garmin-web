"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  isDuplicate,
  remoteActivityId,
} = require("../uploader/garmin-upload");

test("recognizes a duplicate from an HTTP 409 response", () => {
  assert.equal(
    isDuplicate({
      message: "Conflict",
      response: { status: 409, data: {} },
    }),
    true,
  );
});

test("recognizes a duplicate when the library only exposes 409 in text", () => {
  assert.equal(isDuplicate(new Error("HTTP Error (409): Conflict")), true);
});

test("does not treat another HTTP failure as a duplicate", () => {
  assert.equal(isDuplicate(new Error("HTTP Error (500): Server Error")), false);
});

test("extracts activity id from detailed import successes", () => {
  assert.equal(
    remoteActivityId({
      detailedImportResult: {
        successes: [{ internalId: 123456 }],
      },
    }),
    123456,
  );
});
