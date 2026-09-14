import assert from "node:assert/strict";
import { existsSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { cleanDist } from "../scripts/clean-dist.mjs";

const projectRoot = mkdtempSync(path.join(tmpdir(), "novel-web-clean-dist-"));

try {
  const dist = path.join(projectRoot, "dist");
  const staleAsset = path.join(dist, "assets", "stale.js");
  mkdirSync(path.dirname(staleAsset), { recursive: true });
  writeFileSync(staleAsset, "stale");

  cleanDist(projectRoot);

  assert.equal(existsSync(staleAsset), false);
  assert.equal(existsSync(dist), true);
  assert.throws(
    () => cleanDist(projectRoot, path.join(projectRoot, "src")),
    /Refusing to clean unexpected build directory/
  );
} finally {
  rmSync(projectRoot, { recursive: true, force: true });
}

console.log("cleanDist tests passed");
