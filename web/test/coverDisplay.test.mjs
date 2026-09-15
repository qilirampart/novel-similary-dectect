import assert from "node:assert/strict";

import { coverRunActions, coverRunStatusLabel } from "../src/coverDisplay.ts";

assert.equal(coverRunStatusLabel("partial_failed", 0), "部分完成");
assert.equal(coverRunStatusLabel("partial_failed", 2), "部分失败");
assert.equal(coverRunStatusLabel("completed", 0), "已完成");

assert.deepEqual(coverRunActions("queued"), ["pause", "cancel"]);
assert.deepEqual(coverRunActions("running"), ["pause", "cancel"]);
assert.deepEqual(coverRunActions("pause_requested"), ["cancel"]);
assert.deepEqual(coverRunActions("paused"), ["resume", "cancel"]);
assert.deepEqual(coverRunActions("cancel_requested"), []);
assert.deepEqual(coverRunActions("completed"), []);

console.log("coverDisplay tests passed");
