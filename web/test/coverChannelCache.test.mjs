import assert from "node:assert/strict";

import {
  clearCoverChannelCache,
  getCachedChannelPage,
  getCachedFilterOptions,
  setCachedChannelPage,
  setCachedFilterOptions
} from "../src/coverChannelCache.ts";

const query = { keyword: "", operatorPk: undefined, offset: 0 };
const page = { items: [], total: 728, limit: 50, offset: 0 };
const options = { operators: [], channels: [] };

clearCoverChannelCache();
setCachedChannelPage(query, page, 1_000);
assert.equal(getCachedChannelPage(query, 30_000)?.total, 728);
assert.equal(getCachedChannelPage(query, 62_000), undefined);

setCachedChannelPage(query, page, 1_000);
assert.equal(getCachedChannelPage({ ...query, operatorPk: 9 }, 2_000), undefined);
assert.equal(getCachedChannelPage({ ...query, offset: 50 }, 2_000), undefined);

setCachedFilterOptions(options, 1_000);
assert.deepEqual(getCachedFilterOptions(299_000), options);
assert.equal(getCachedFilterOptions(302_000), undefined);

setCachedChannelPage(query, page, 1_000);
clearCoverChannelCache();
assert.equal(getCachedChannelPage(query, 2_000), undefined);
assert.equal(getCachedFilterOptions(2_000), undefined);

console.log("coverChannelCache tests passed");
