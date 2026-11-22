import { existsSync, readdirSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
// @ts-expect-error -- plain Node script without type declarations
import { packageDir, PRIVATE } from "../../scripts/sync-app-runtime.mjs";

const MOBILE = fileURLToPath(new URL("../../", import.meta.url));
const RUNTIME = join(MOBILE, "modules/opencuria-app-runtime");

interface ExpoModuleConfig {
  apple?: { modules?: string[]; podspecPath?: string; swiftModuleName?: string };
  android?: { modules?: string[] };
}

const isPrivate = (name: string) =>
  (PRIVATE as RegExp[]).some((pattern) => pattern.test(name));

/** String literals of the first `<start> … <end>` block in a native source. */
function literals(file: string, start: string, end: string): string[] {
  const source = readFileSync(join(RUNTIME, file), "utf8");
  const from = source.indexOf(start);
  expect(from, `${start} in ${file}`).toBeGreaterThanOrEqual(0);
  const block = source.slice(from, source.indexOf(end, from + start.length));
  return [...block.matchAll(/"([^"]+)"/g)].map((match) => match[1]!);
}

const ANDROID_PREFIXES = literals(
  "android/src/main/java/expo/modules/opencuriaappruntime/AppRuntimePackages.kt",
  "HOST_ONLY_PREFIXES = listOf(",
  ")",
);
const IOS_SWIFT_MODULES = literals(
  "ios/AppSandbox.swift",
  "hostOnlySwiftModules: Set<String> = [",
  "]",
);
const IOS_SWIFT_PREFIX = "OpenCuria";

/** Every host-only package: OpenCuria's local modules and private dependencies, one level deep. */
function hostOnlyPackages(): Map<string, string> {
  const packages = new Map<string, string>();
  for (const name of readdirSync(join(MOBILE, "modules"))) {
    packages.set(name, join(MOBILE, "modules", name));
  }
  const manifest = JSON.parse(readFileSync(join(MOBILE, "package.json"), "utf8"));
  for (const name of Object.keys(manifest.dependencies).filter(isPrivate)) {
    const dir = packageDir(name) as string;
    packages.set(name, dir);
    const own = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
    for (const dependency of Object.keys(own.dependencies ?? {}).filter(isPrivate)) {
      packages.set(dependency, packageDir(dependency) as string);
    }
  }
  return packages;
}

function swiftModule(dir: string, config: ExpoModuleConfig): string {
  if (config.apple?.swiftModuleName) return config.apple.swiftModuleName;
  const podspec =
    config.apple?.podspecPath ??
    [dir, join(dir, "ios")]
      .flatMap((base) => (existsSync(base) ? readdirSync(base).map((f) => join(base, f)) : []))
      .find((file) => file.endsWith(".podspec"));
  expect(podspec, `podspec of ${dir}`).toBeTruthy();
  return basename(podspec!, ".podspec");
}

describe("app runtime host modules", () => {
  const packages = hostOnlyPackages();

  it("names OpenCuria's local modules so the runtime spec leaves them out", () => {
    for (const name of readdirSync(join(MOBILE, "modules"))) {
      expect(isPrivate(name), name).toBe(true);
    }
  });

  it("hides the native modules of every host-only package from apps", () => {
    expect(ANDROID_PREFIXES.length).toBeGreaterThan(0);
    expect(IOS_SWIFT_MODULES.length).toBeGreaterThan(0);
    let checked = 0;
    for (const [name, dir] of packages) {
      const file = join(dir, "expo-module.config.json");
      if (!existsSync(file)) continue;
      const config = JSON.parse(readFileSync(file, "utf8")) as ExpoModuleConfig;
      for (const module of config.android?.modules ?? []) {
        expect(
          ANDROID_PREFIXES.some((prefix) => module.startsWith(prefix)),
          `${name}: ${module} (AppRuntimePackages.HOST_ONLY_PREFIXES)`,
        ).toBe(true);
        checked++;
      }
      if (config.apple?.modules?.length) {
        const swift = swiftModule(dir, config);
        expect(
          swift.startsWith(IOS_SWIFT_PREFIX) || IOS_SWIFT_MODULES.includes(swift),
          `${name}: Swift module ${swift} (SandboxModulesProvider)`,
        ).toBe(true);
        checked++;
      }
    }
    expect(checked).toBeGreaterThan(5);
  });
});
