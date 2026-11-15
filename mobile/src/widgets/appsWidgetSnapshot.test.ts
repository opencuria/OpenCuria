import { describe, expect, it } from "vitest";
import type { InstalledApp } from "../apps/appInstaller";
import {
  MAX_WIDGET_APPS,
  appsWidgetSnapshot,
  widgetApps,
} from "./appsWidgetSnapshot";

const SCOPE = "https://curia|org|1";

function app(
  appId: string,
  name: string,
  times: { installedAt?: string; updatedAt?: string; openedAt?: string } = {},
  scope = SCOPE,
): InstalledApp {
  return {
    appId,
    scope,
    name,
    slug: name.toLowerCase(),
    versionId: `${appId}-v1`,
    versionNumber: 1,
    versionName: "1.0.0",
    sdkVersion: "57.0.0",
    publishedAt: "2026-11-01T00:00:00.000Z",
    installedAt: times.installedAt ?? "2026-11-01T00:00:00.000Z",
    ...(times.updatedAt ? { updatedAt: times.updatedAt } : {}),
    ...(times.openedAt ? { openedAt: times.openedAt } : {}),
    hasIcon: true,
  };
}

describe("apps widget snapshot", () => {
  it("asks to sign in without an account", () => {
    expect(appsWidgetSnapshot([app("a", "Notes")], null)).toEqual({
      status: "signedOut",
      apps: [],
      appsUrl: "opencuria:///?apps",
    });
  });

  it("lists the account's apps most recently used first", () => {
    const snapshot = appsWidgetSnapshot(
      [
        app("weather", "Weather", { openedAt: "2026-11-05T00:00:00.000Z" }),
        app("notes", "Notes", { updatedAt: "2026-11-06T00:00:00.000Z" }),
        app("timer", "Timer", { openedAt: "2026-11-04T00:00:00.000Z" }),
        app("other", "Other", {}, "https://curia|org|2"),
      ],
      SCOPE,
      { weather: "file:///icons/weather.png" },
    );

    expect(snapshot.status).toBe("ready");
    expect(snapshot.apps.map((item) => item.id)).toEqual([
      "notes",
      "weather",
      "timer",
    ]);
    expect(snapshot.apps[1]).toEqual({
      id: "weather",
      name: "Weather",
      url: "opencuria:///?app=weather",
      icon: "file:///icons/weather.png",
      isNew: false,
      order: 2,
    });
    // Tiles keep an alphabetical place, whatever was used last.
    expect(
      Object.fromEntries(snapshot.apps.map((item) => [item.id, item.order])),
    ).toEqual({ notes: 0, timer: 1, weather: 2 });
    expect(snapshot.apps[0]).not.toHaveProperty("icon");
    // iOS stores the snapshot in user defaults, which reject null.
    expect(JSON.stringify(snapshot)).not.toContain("null");
  });

  it("marks apps new until they are opened after an install or update", () => {
    const [fresh, opened, updated] = appsWidgetSnapshot(
      [
        app("fresh", "Fresh", { updatedAt: "2026-11-06T00:00:00.000Z" }),
        app("opened", "Opened", {
          updatedAt: "2026-11-05T00:00:00.000Z",
          openedAt: "2026-11-05T01:00:00.000Z",
        }),
        app("updated", "Updated", {
          updatedAt: "2026-11-04T02:00:00.000Z",
          openedAt: "2026-11-04T01:00:00.000Z",
        }),
      ],
      SCOPE,
    ).apps;

    expect(fresh?.isNew).toBe(true);
    expect(opened?.isNew).toBe(false);
    expect(updated?.isNew).toBe(true);
  });

  it("keeps no more apps than the largest widget shows", () => {
    const many = Array.from({ length: MAX_WIDGET_APPS + 5 }, (_, index) =>
      app(`app${index}`, `App ${index}`),
    );
    expect(widgetApps(many, SCOPE)).toHaveLength(MAX_WIDGET_APPS);
  });
});
