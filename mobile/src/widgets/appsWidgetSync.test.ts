import { describe, expect, it, vi } from "vitest";
import type { AppInstaller, InstalledApp } from "../apps/appInstaller";
import type { AppsWidgetTarget } from "./appsWidget";
import type { AppsWidgetSnapshot } from "./appsWidgetSnapshot";
import { AppsWidgetSync } from "./appsWidgetSync";

const SCOPE = "https://curia|org|1";

function installed(appId: string, scope = SCOPE): InstalledApp {
  return {
    appId,
    scope,
    name: appId,
    slug: appId,
    versionId: "v1",
    versionNumber: 1,
    versionName: "1.0.0",
    sdkVersion: "57.0.0",
    publishedAt: "2026-11-01T00:00:00.000Z",
    installedAt: "2026-11-01T00:00:00.000Z",
    hasIcon: false,
  };
}

/** The installer surface the widget follows. */
function fakeInstaller(apps: InstalledApp[]) {
  const listeners = new Set<() => void>();
  return {
    apps,
    installed: vi.fn(async () => [...apps]),
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    change() {
      for (const listener of listeners) listener();
    },
  };
}

function fakeWidget(available = true) {
  const published: AppsWidgetSnapshot[] = [];
  const widget: AppsWidgetTarget = {
    available,
    iconDirectory: async () => null,
    publish: async (snapshot) => {
      published.push(snapshot);
    },
  };
  return { widget, published };
}

function setup(apps: InstalledApp[], available = true) {
  const installer = fakeInstaller(apps);
  const { widget, published } = fakeWidget(available);
  const sync = new AppsWidgetSync(
    installer as unknown as AppInstaller,
    widget,
    async () => ({}),
  );
  return { installer, published, sync };
}

describe("AppsWidgetSync", () => {
  it("waits for the session, then shows the account's apps", async () => {
    const { published, sync } = setup([
      installed("mine"),
      installed("theirs", "https://curia|org|2"),
    ]);
    const stop = sync.start();
    await sync.request();
    expect(published).toEqual([]);

    sync.setScope(SCOPE);
    await sync.request();

    expect(published).toHaveLength(1);
    expect(published[0]?.apps.map((app) => app.id)).toEqual(["mine"]);
    stop();
  });

  it("follows installs and skips unchanged content", async () => {
    const { installer, published, sync } = setup([installed("a")]);
    sync.start();
    sync.setScope(SCOPE);
    await sync.request();

    installer.apps.push(installed("b"));
    installer.change();
    await sync.request();
    await sync.request();

    expect(published.map((snapshot) => snapshot.apps.length)).toEqual([1, 2]);
  });

  it("shows the sign-in state after signing out", async () => {
    const { published, sync } = setup([installed("a")]);
    sync.setScope(SCOPE);
    await sync.request();
    sync.setScope(null);
    await sync.request();

    expect(published.at(-1)).toMatchObject({ status: "signedOut", apps: [] });
  });

  it("shows the latest account when it changes during an update", async () => {
    const { installer, published, sync } = setup([
      installed("a"),
      installed("b", "https://curia|org|2"),
    ]);
    let release: () => void = () => undefined;
    installer.installed.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve([...installer.apps]);
        }),
    );
    sync.setScope(SCOPE);
    sync.setScope("https://curia|org|2");
    release();
    await sync.request();

    expect(published.map((snapshot) => snapshot.apps[0]?.id)).toEqual(["b"]);
  });

  it("does nothing in builds without the widget", async () => {
    const { installer, published, sync } = setup([installed("a")], false);
    sync.setScope(SCOPE);
    await sync.request();

    expect(published).toEqual([]);
    expect(installer.installed).not.toHaveBeenCalled();
  });
});
