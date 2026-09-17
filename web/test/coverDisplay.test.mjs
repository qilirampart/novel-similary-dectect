import assert from "node:assert/strict";

import {
  coverRiskCaseStatusLabel,
  coverRunActions,
  coverRunStatusLabel
} from "../src/coverDisplay.ts";

assert.equal(coverRunStatusLabel("partial_failed", 0), "部分完成");
assert.equal(coverRunStatusLabel("partial_failed", 2), "部分失败");
assert.equal(coverRunStatusLabel("completed", 0), "已完成");

assert.deepEqual(coverRunActions("queued"), ["pause", "cancel"]);
assert.deepEqual(coverRunActions("running"), ["pause", "cancel"]);
assert.deepEqual(coverRunActions("pause_requested"), ["cancel"]);
assert.deepEqual(coverRunActions("paused"), ["resume", "cancel"]);
assert.deepEqual(coverRunActions("cancel_requested"), []);
assert.deepEqual(coverRunActions("completed"), []);

assert.equal(coverRiskCaseStatusLabel("confirmed_risk"), "已确认风险");
assert.equal(coverRiskCaseStatusLabel("needs_review"), "待复核");

console.log("coverDisplay tests passed");
