import assert from "node:assert/strict";

import {
  buildReviewPath,
  buildReviewSearchParams,
  isHighRiskReviewLabel,
  parsePositiveId,
  parseReviewQueryState
} from "../src/workflowLinks.ts";

assert.equal(parsePositiveId("1"), 1);
assert.equal(parsePositiveId("42"), 42);
assert.equal(parsePositiveId("0"), null);
assert.equal(parsePositiveId("-9"), null);
assert.equal(parsePositiveId("1.5"), null);
assert.equal(parsePositiveId("abc"), null);
assert.equal(parsePositiveId(null), null);

assert.equal(isHighRiskReviewLabel("强证据"), true);
assert.equal(isHighRiskReviewLabel(" 强证据 "), true);
assert.equal(isHighRiskReviewLabel("中证据"), false);

assert.equal(
  buildReviewPath({
    taskId: " task-1 ",
    resultId: " 12 ",
    status: " completed ",
    reviewStatus: " confirmed_high_risk ",
    q: " alpha "
  }),
  "/review?taskId=task-1&resultId=12&status=completed&reviewStatus=confirmed_high_risk&q=alpha"
);
assert.equal(buildReviewPath({ taskId: "  " }), "/review");

const params = buildReviewSearchParams({
  taskId: "task-2",
  resultId: 88,
  status: "completed",
  reviewStatus: "needs_followup",
  q: "keyword"
});

assert.equal(
  params.toString(),
  "taskId=task-2&resultId=88&status=completed&reviewStatus=needs_followup&q=keyword"
);

assert.deepEqual(parseReviewQueryState(params), {
  taskId: "task-2",
  resultId: 88,
  status: "completed",
  reviewStatus: "needs_followup",
  q: "keyword"
});

console.log("workflowLinks tests passed");
