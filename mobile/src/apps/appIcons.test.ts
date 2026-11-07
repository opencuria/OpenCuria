import { beforeEach, describe, expect, it, vi } from "vitest";
import { Directory, File, __files } from "expo-file-system";
import type { PublishedApp } from "@opencuria/client";
import type { InstalledApp } from "./appInstaller";
import { AppIconCache, appIconSource } from "./appIcons";
import type { AppRow } from "./appPresentation";

const ROOT = "file:///cache/opencuria-app-icons";

function cache() {
  const shrink = vi.fn(async (source: string, target: File) => {
    target.create();
    target.write(`small:${source}`);
    return true;
  });
  const download = vi.fn(async (request: { url: string }, target: File) => {
    target.create();
    target.write(`png:${request.url}`);
  });
  return {
    shrink,
    download,
    icons: new AppIconCache(new Directory(ROOT), shrink, download),
  };
}

describe("AppIconCache", () => {
  beforeEach(() => __files.clear());

  it("shrinks an installed icon once and keeps one copy per app", async () => {
    const { icons, shrink } = cache();
    const old = new File(new Directory(ROOT), "todo.v1.png");
    old.create();
    const other = new File(new Directory(ROOT), "notes.v1.png");
    other.create();

    const first = await icons.icons([
      { appId: "todo", token: "v2", file: "file:///docs/todo/icon.png" },
    ]);
    expect(first).toEqual({ todo: `${ROOT}/todo.v2.png` });
    expect(await new File(`${ROOT}/todo.v2.png`).text()).toBe(
      "small:file:///docs/todo/icon.png",
    );
    // The previous version's copy goes; other apps keep theirs.
    expect(old.exists).toBe(false);
    expect(other.exists).toBe(true);

    await icons.icons([
      { appId: "todo", token: "v2", file: "file:///docs/todo/icon.png" },
    ]);
    expect(shrink).toHaveBeenCalledTimes(1);
  });

  it("downloads the icon of an app that is not installed, then removes the original", async () => {
    const { icons, download } = cache();
    const result = await icons.icons([
      {
        appId: "game",
        token: "abc",
        request: () => ({ url: "https://curia.test/icon", headers: {} }),
      },
    ]);
    expect(result).toEqual({ game: `${ROOT}/game.abc.png` });
    expect(download).toHaveBeenCalledTimes(1);
    expect(await new File(`${ROOT}/game.abc.png`).text()).toBe(
      `small:${ROOT}/game.abc.png.download`,
    );
    expect(new File(`${ROOT}/game.abc.png.download`).exists).toBe(false);
  });

  it("leaves an app without icon when making one fails", async () => {
    const { icons, download, shrink } = cache();
    download.mockRejectedValueOnce(new Error("offline"));
    shrink.mockResolvedValueOnce(false);
    const result = await icons.icons([
      {
        appId: "game",
        token: "abc",
        request: () => ({ url: "https://curia.test/icon", headers: {} }),
      },
      { appId: "todo", token: "v1", file: "file:///docs/todo/icon.png" },
    ]);
    expect(result).toEqual({});
    expect(__files.size).toBe(0);
  });
});

describe("appIconSource", () => {
  const installer = {
    iconUri: (app: InstalledApp) =>
      app.hasIcon ? `file:///docs/${app.appId}/icon.png` : null,
  };
  const fileRequest = (app: string, version: string, sha: string) => ({
    url: `https://curia.test/${app}/${version}/${sha}`,
    headers: {},
  });

  function row(overrides: Partial<AppRow>): AppRow {
    return {
      appId: "game",
      name: "Game",
      state: "available",
      installed: null,
      published: null,
      ...overrides,
    };
  }

  it("prefers the installed copy's icon", () => {
    const installed = {
      appId: "game",
      versionId: "v1",
      hasIcon: true,
    } as InstalledApp;
    expect(
      appIconSource(row({ installed, state: "installed" }), installer),
    ).toEqual({
      appId: "game",
      token: "v1",
      file: "file:///docs/game/icon.png",
    });
    expect(
      appIconSource(
        row({ installed: { ...installed, hasIcon: false } }),
        installer,
      ),
    ).toBeNull();
  });

  it("downloads the newest version's icon of an app to install", () => {
    const published = {
      id: "game",
      latest_version: { id: "v3", icon_sha256: "a".repeat(64) },
    } as PublishedApp;
    const source = appIconSource(row({ published }), installer, fileRequest);
    expect(source).toMatchObject({ appId: "game", token: "a".repeat(16) });
    expect(source && "request" in source && source.request()).toEqual({
      url: `https://curia.test/game/v3/${"a".repeat(64)}`,
      headers: {},
    });
    // Offline there is no way to fetch it.
    expect(appIconSource(row({ published }), installer)).toBeNull();
    expect(
      appIconSource(
        row({
          published: {
            ...published,
            latest_version: { ...published.latest_version!, icon_sha256: null },
          },
        }),
        installer,
        fileRequest,
      ),
    ).toBeNull();
  });
});
