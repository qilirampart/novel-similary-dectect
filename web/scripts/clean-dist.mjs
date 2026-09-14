import { existsSync, lstatSync, mkdirSync, readdirSync, unlinkSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

export function cleanDist(projectRoot, target = path.join(projectRoot, "dist")) {
  const resolvedRoot = path.resolve(projectRoot);
  const resolvedTarget = path.resolve(target);
  const expectedTarget = path.join(resolvedRoot, "dist");

  if (resolvedTarget !== expectedTarget) {
    throw new Error(`Refusing to clean unexpected build directory: ${resolvedTarget}`);
  }

  mkdirSync(resolvedTarget, { recursive: true });

  const pending = [resolvedTarget];
  while (pending.length > 0) {
    const current = pending.pop();
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const entryPath = path.join(current, entry.name);
      const metadata = lstatSync(entryPath);
      if (metadata.isSymbolicLink()) {
        throw new Error(`Refusing to clean symbolic link in build directory: ${entryPath}`);
      }
      if (metadata.isDirectory()) {
        pending.push(entryPath);
      } else {
        unlinkSync(entryPath);
      }
    }
  }

  const remainingFiles = readdirSync(resolvedTarget, { recursive: true }).filter((entry) => {
    const entryPath = path.join(resolvedTarget, entry);
    return existsSync(entryPath) && !lstatSync(entryPath).isDirectory();
  });
  if (remainingFiles.length > 0) {
    throw new Error(`Build directory still contains ${remainingFiles.length} file(s) after cleanup`);
  }
}

const scriptPath = fileURLToPath(import.meta.url);
if (process.argv[1] && path.basename(process.argv[1]).toLowerCase() === "clean-dist.mjs") {
  cleanDist(path.resolve(path.dirname(scriptPath), ".."));
}
