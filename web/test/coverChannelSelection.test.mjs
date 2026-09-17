import assert from "node:assert/strict";

import {
  invertFilteredSelection,
  selectAllFiltered,
  selectFirstFiltered
} from "../src/coverChannelSelection.ts";

assert.deepEqual([...selectAllFiltered([4, 2, 4, 8])], [4, 2, 8]);
assert.deepEqual([...invertFilteredSelection([1, 2, 3, 4], new Set([2, 4, 99]))], [1, 3]);
assert.deepEqual([...selectFirstFiltered([8, 7, 6, 5], 2)], [8, 7]);
assert.deepEqual([...selectFirstFiltered([8, 7], 20)], [8, 7]);
assert.deepEqual([...selectFirstFiltered([8, 7], 0)], []);

console.log("coverChannelSelection tests passed");
