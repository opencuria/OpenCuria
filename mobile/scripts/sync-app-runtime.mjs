#!/usr/bin/env node
/**
 * Writes the native runtime that apps published by agents can rely on to
 * backend/apps/mobile_apps/runtime.json: the Expo SDK, React Native and every
 * native package of this app with its installed version. The backend checks
 * published builds against it; `src/apps/appRuntimeSpec.test.ts` keeps it in sync.
 *
 * Usage: node mobile/scripts/sync-app-runtime.mjs [--check]
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const mobileRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const target = resolve(mobileRoot, "../backend/apps/mobile_apps/runtime.json");
const require = createRequire(join(mobileRoot, "package.json"));

/** Host-only packages that apps must not depend on. */
const PRIVATE = [/^opencuria-/, /^expo-dev-/, /^expo-widgets$/];

/** Installed directory of a dependency, as Node would resolve it. */
function packageDir(name) {
  for (const base of require.resolve.paths(name) ?? []) {
    const dir = join(base, name);
    if (existsSync(join(dir, "package.json"))) return dir;
  }
  throw new Error(`${name} is not installed; run npm ci first.`);
}

function hasNativeCode(dir) {
  return ["android", "ios", "apple", "expo-module.config.json"].some((entry) =>
    existsSync(join(dir, entry)),
  );
}

export function computeAppRuntime() {
  const manifest = JSON.parse(
    readFileSync(join(mobileRoot, "package.json"), "utf8"),
  );
  const version = (name) =>
    JSON.parse(readFileSync(join(packageDir(name), "package.json"), "utf8"))
      .version;
  const packages = {};
  for (const name of Object.keys(manifest.dependencies).sort()) {
    if (PRIVATE.some((pattern) => pattern.test(name))) continue;
    const dir = packageDir(name);
    const core = name === "react" || name === "react-native";
    if (core || hasNativeCode(dir)) packages[name] = version(name);
  }
  const sdk = version("expo").split(".")[0];
  return {
    sdk_version: `${sdk}.0.0`,
    react_native_version: version("react-native"),
    packages,
  };
}

export function serializeAppRuntime(runtime) {
  return `${JSON.stringify(runtime, null, 2)}\n`;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const next = serializeAppRuntime(computeAppRuntime());
  const current = existsSync(target) ? readFileSync(target, "utf8") : "";
  if (process.argv.includes("--check")) {
    if (current !== next) {
      console.error(
        "runtime.json is stale; run node mobile/scripts/sync-app-runtime.mjs",
      );
      process.exit(1);
    }
  } else if (current !== next) {
    writeFileSync(target, next);
    console.log(`Updated ${target}`);
  }
}
