import assert from "node:assert/strict";

import { coverRunStatusLabel } from "../src/coverDisplay.ts";

assert.equal(coverRunStatusLabel("partial_failed", 0), "部分完成");
assert.equal(coverRunStatusLabel("partial_failed", 2), "部分失败");
assert.equal(coverRunStatusLabel("completed", 0), "已完成");

console.log("coverDisplay tests passed");
