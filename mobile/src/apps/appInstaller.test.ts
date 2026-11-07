import { createHash } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppManifest } from "@opencuria/client";
import { AppInstaller, appLayout } from "./appInstaller";
import type { AppFileSystem } from "./appStorage";

const md5 = (text: string) => createHash("md5").update(text).digest("hex");
const ROOT = "file:///docs/opencuria-apps";

/** In-memory file system; `remote` holds what the server serves by sha256. */
class MemoryFileSystem implements AppFileSystem {
  readonly root = ROOT;
  files = new Map<string, string>();
  directories = new Set<string>();
  remote = new Map<string, string>();
  downloads: string[] = [];
  corrupt = new Set<string>();
  /** Downloads of these sha256 take a few milliseconds. */
  slow = new Set<string>();

  exists(uri: string) {
    return this.files.has(uri) || this.directories.has(uri);
  }
  async readText(uri: string) {
    const text = this.files.get(uri);
    if (text === undefined) throw new Error(`missing ${uri}`);
    return text;
  }
  writeText(uri: string, text: string) {
    this.files.set(uri, text);
  }
  makeDirectory(uri: string) {
    this.directories.add(uri);
  }
  remove(uri: string) {
    this.files.delete(uri);
    this.directories.delete(uri);
    for (const key of [...this.files.keys(), ...this.directories])
      if (key.startsWith(`${uri}/`)) {
        this.files.delete(key);
        this.directories.delete(key);
      }
  }
  list(uri: string) {
    const names = new Set<string>();
    for (const key of [...this.files.keys(), ...this.directories])
      if (key.startsWith(`${uri}/`))
        names.add(key.slice(uri.length + 1).split("/")[0]!);
    return [...names];
  }
  move(from: string, to: string) {
    this.files.set(to, this.files.get(from)!);
    this.files.delete(from);
  }
  size(uri: string) {
    return this.files.get(uri)?.length ?? 0;
  }
  md5(uri: string) {
    const text = this.files.get(uri);
    return text === undefined ? null : md5(text);
  }
  async download(url: string, headers: Record<string, string>, to: string) {
    expect(headers.Authorization).toBe("Bearer token");
    const sha = url.split("/").filter(Boolean).pop()!;
    this.downloads.push(sha);
    if (this.slow.has(sha)) await new Promise((done) => setTimeout(done, 5));
    const body = this.remote.get(sha);
    if (body === undefined) throw new Error("404");
    this.files.set(to, this.corrupt.has(sha) ? `${body}!` : body);
  }
}

function file(fs: MemoryFileSystem, content: string) {
  const sha256 = `sha-${content}`;
  fs.remote.set(sha256, content);
  return { sha256, md5: md5(content), size: content.length };
}

function manifest(
  fs: MemoryFileSystem,
  version: { id: string; number: number; bundle: string; assets: string[] },
): AppManifest {
  return {
    app_id: "app-1",
    version_id: version.id,
    number: version.number,
    name: "Todo",
    slug: "todo",
    version_name: `1.${version.number}.0`,
    sdk_version: "57.0.0",
    platform: "android",
    expo_config: { name: "Todo", orientation: "portrait" },
    bundle: file(fs, version.bundle),
    assets: version.assets.map((content) => ({
      ...file(fs, content),
      key: `k${md5(content).slice(0, 8)}`,
      extension: "png",
    })),
    icon: file(fs, "icon"),
    created_at: "2026-11-07T10:00:00Z",
  };
}

const options = (fs: MemoryFileSystem) => ({
  scope: "https://curia|org|1",
  fileRequest: (sha256: string) => ({
    url: `https://curia/api/v1/apps/app-1/versions/v/files/${sha256}/`,
    headers: { Authorization: "Bearer token" },
  }),
});

describe("AppInstaller", () => {
  let fs: MemoryFileSystem;
  let installer: AppInstaller;
  beforeEach(() => {
    fs = new MemoryFileSystem();
    installer = new AppInstaller(fs, () => new Date("2026-11-07T12:00:00Z"));
  });

  it("installs a version and prepares an offline launch", async () => {
    const v1 = manifest(fs, {
      id: "v1",
      number: 1,
      bundle: "bundle-1",
      assets: ["logo"],
    });
    const progress = vi.fn();

    const installed = await installer.install(v1, {
      ...options(fs),
      onProgress: progress,
    });

    expect(installed).toMatchObject({
      appId: "app-1",
      versionId: "v1",
      versionNumber: 1,
      hasIcon: true,
      scope: "https://curia|org|1",
    });
    expect(progress).toHaveBeenLastCalledWith({ done: 16, total: 16 });
    const layout = appLayout(ROOT, "app-1");
    expect(fs.files.get(layout.bundle("v1"))).toBe("bundle-1");
    expect([...fs.files.keys()].some((key) => key.endsWith(".part"))).toBe(
      false,
    );
    expect(await installer.installed()).toHaveLength(1);

    const launch = await installer.launchRequest(installed);
    expect(launch).toMatchObject({
      appId: "app-1",
      bundlePath: "/docs/opencuria-apps/app-1/versions/v1/index.bundle",
      installDir: "/docs/opencuria-apps/app-1",
      dataDir: "/docs/opencuria-apps/app-1/data",
      orientation: "portrait",
      localAssets: {
        [v1.assets[0]!.key]: `${layout.assets}/${v1.assets[0]!.key}.png`,
      },
    });
    expect(launch.manifest).toMatchObject({
      id: "v1",
      metadata: {},
      extra: { expoClient: { name: "Todo", orientation: "portrait" } },
    });
  });

  it("updates by downloading only changed files and removes stale ones", async () => {
    await installer.install(
      manifest(fs, { id: "v1", number: 1, bundle: "b1", assets: ["a", "b"] }),
      options(fs),
    );
    fs.downloads = [];
    const v2 = manifest(fs, {
      id: "v2",
      number: 2,
      bundle: "b2",
      assets: ["a", "c"],
    });

    const installed = await installer.install(v2, options(fs));

    expect(fs.downloads.sort()).toEqual(["sha-b2", "sha-c"]);
    expect(installed.versionNumber).toBe(2);
    expect(installed.installedAt).toBe("2026-11-07T12:00:00.000Z");
    const layout = appLayout(ROOT, "app-1");
    expect(fs.list(layout.versions)).toEqual(["v2"]);
    expect(fs.list(layout.assets).sort()).toEqual(
      v2.assets.map((asset) => `${asset.key}.png`).sort(),
    );
  });

  it("keeps the installed version when a download is damaged", async () => {
    const v1 = manifest(fs, { id: "v1", number: 1, bundle: "b1", assets: [] });
    await installer.install(v1, options(fs));
    const v2 = manifest(fs, { id: "v2", number: 2, bundle: "b2", assets: [] });
    fs.corrupt.add("sha-b2");

    await expect(installer.install(v2, options(fs))).rejects.toThrow(
      /damaged/,
    );

    const [current] = await installer.installed();
    expect(current?.versionId).toBe("v1");
    expect(fs.exists(appLayout(ROOT, "app-1").bundle("v1"))).toBe(true);
    expect([...fs.files.keys()].some((key) => key.endsWith(".part"))).toBe(
      false,
    );
  });

  it("stops all downloads before a failed install rejects", async () => {
    const assets = ["a1", "a2", "a3", "a4", "a5", "a6", "a7"];
    const v1 = manifest(fs, { id: "v1", number: 1, bundle: "b1", assets });
    fs.remote.delete(v1.bundle.sha256);
    for (const asset of v1.assets) fs.slow.add(asset.sha256);
    const onProgress = vi.fn();

    await expect(
      installer.install(v1, { ...options(fs), onProgress }),
    ).rejects.toThrow("404");
    const downloads = fs.downloads.length;
    const reports = onProgress.mock.calls.length;
    await new Promise((done) => setTimeout(done, 30));

    // Nothing keeps downloading or reporting progress after the rejection.
    expect(fs.downloads).toHaveLength(downloads);
    expect(downloads).toBeLessThan(assets.length + 2);
    expect(onProgress).toHaveBeenCalledTimes(reports);
    expect([...fs.files.keys()].some((key) => key.endsWith(".part"))).toBe(
      false,
    );
    expect(await installer.installed()).toEqual([]);
  });

  it("rejects manifests whose ids would escape the app directory", async () => {
    const bad = {
      ...manifest(fs, { id: "v1", number: 1, bundle: "b", assets: [] }),
      version_id: "../../escape",
    };
    await expect(installer.install(bad, options(fs))).rejects.toThrow(
      /Invalid version id/,
    );
    expect(fs.downloads).toEqual([]);
  });

  it("recovers the registry from an interrupted replacement", async () => {
    await installer.install(
      manifest(fs, { id: "v1", number: 1, bundle: "b", assets: [] }),
      options(fs),
    );
    // Android removes the old file before renaming the new one into place.
    const registry = `${ROOT}/registry.json`;
    fs.files.set(`${registry}.tmp`, fs.files.get(registry)!);
    fs.files.delete(registry);

    const [recovered] = await new AppInstaller(fs).installed();
    expect(recovered?.versionId).toBe("v1");
  });

  it("never saves over a registry it could not read", async () => {
    await installer.install(
      manifest(fs, { id: "v1", number: 1, bundle: "b", assets: [] }),
      options(fs),
    );
    const reloaded = new AppInstaller(fs);
    const readText = vi
      .spyOn(fs, "readText")
      .mockRejectedValueOnce(new Error("I/O error"));
    await expect(reloaded.installed()).rejects.toThrow("I/O error");
    readText.mockRestore();

    await reloaded.install(
      { ...manifest(fs, { id: "w1", number: 1, bundle: "c", assets: [] }), app_id: "app-2" },
      options(fs),
    );
    expect((await reloaded.installed()).map((app) => app.appId).sort()).toEqual([
      "app-1",
      "app-2",
    ]);
  });

  it("uninstalls an app with its data and survives a reload", async () => {
    const installed = await installer.install(
      manifest(fs, { id: "v1", number: 1, bundle: "b", assets: [] }),
      options(fs),
    );
    await installer.launchRequest(installed);
    expect(fs.exists(appLayout(ROOT, "app-1").data)).toBe(true);

    await installer.uninstall("app-1");

    expect(fs.exists(appLayout(ROOT, "app-1").app)).toBe(false);
    expect(await new AppInstaller(fs).installed()).toEqual([]);
  });

  it("remembers opening an app until its next update", async () => {
    let now = new Date("2026-11-07T12:00:00Z");
    installer = new AppInstaller(fs, () => now);
    const installed = await installer.install(
      manifest(fs, { id: "v1", number: 1, bundle: "b", assets: [] }),
      options(fs),
    );
    expect(installed.updatedAt).toBe("2026-11-07T12:00:00.000Z");
    expect(installed.openedAt).toBeUndefined();

    now = new Date("2026-11-07T13:00:00Z");
    await installer.markOpened("app-1");
    await installer.markOpened("unknown");
    const [opened] = await new AppInstaller(fs).installed();
    expect(opened?.openedAt).toBe("2026-11-07T13:00:00.000Z");

    now = new Date("2026-11-07T14:00:00Z");
    const updated = await installer.install(
      manifest(fs, { id: "v2", number: 2, bundle: "b2", assets: [] }),
      options(fs),
    );
    expect(updated).toMatchObject({
      installedAt: "2026-11-07T12:00:00.000Z",
      updatedAt: "2026-11-07T14:00:00.000Z",
      openedAt: "2026-11-07T13:00:00.000Z",
    });
  });

  it("counts apps installed before use was tracked as opened", async () => {
    const app = {
      appId: "app-1",
      scope: "s",
      name: "Todo",
      slug: "todo",
      versionId: "v1",
      versionNumber: 1,
      versionName: "1.0.0",
      sdkVersion: "57.0.0",
      publishedAt: "2026-11-01T10:00:00Z",
      installedAt: "2026-11-02T10:00:00Z",
      hasIcon: false,
    };
    fs.files.set(
      `${ROOT}/registry.json`,
      JSON.stringify({ version: 1, apps: [app] }),
    );

    const [migrated] = await installer.installed();

    expect(migrated).toMatchObject({
      updatedAt: "2026-11-02T10:00:00Z",
      openedAt: "2026-11-02T10:00:00Z",
    });
  });

  it("tells subscribers about installs, opens and removals", async () => {
    const listener = vi.fn();
    const unsubscribe = installer.subscribe(listener);

    await installer.install(
      manifest(fs, { id: "v1", number: 1, bundle: "b", assets: [] }),
      options(fs),
    );
    await installer.markOpened("app-1");
    await installer.uninstall("app-1");
    expect(listener).toHaveBeenCalledTimes(3);

    unsubscribe();
    await installer.install(
      manifest(fs, { id: "v1", number: 1, bundle: "b", assets: [] }),
      options(fs),
    );
    expect(listener).toHaveBeenCalledTimes(3);
  });
});
