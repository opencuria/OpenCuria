import { beforeEach, describe, expect, it } from "vitest";
import { Directory, File, __files } from "expo-file-system";
import type { AppInstaller, InstalledApp } from "../apps/appInstaller";
import { prepareWidgetIcons } from "./widgetIcons";

const ROOT = "file:///group/opencuria-apps";

function installed(appId: string, versionId: string, hasIcon = true) {
  return { appId, versionId, hasIcon } as InstalledApp;
}

const installer = {
  iconUri: (app: InstalledApp) =>
    app.hasIcon ? `file:///docs/opencuria-apps/${app.appId}/icon.png` : null,
} as AppInstaller;

describe("prepareWidgetIcons", () => {
  beforeEach(() => __files.clear());

  it("makes one small copy per app version and removes stale ones", async () => {
    const directory = new Directory(ROOT);
    const stale = new File(directory, "gone-v1.png");
    stale.create();
    const kept = new File(directory, "notes-v2.png");
    kept.create();
    kept.write("existing");

    const icons = await prepareWidgetIcons(
      [
        installed("notes", "v2"),
        installed("timer", "v1"),
        installed("plain", "v1", false),
      ],
      installer,
      directory,
    );

    expect(icons).toEqual({
      notes: `${ROOT}/notes-v2.png`,
      timer: `${ROOT}/timer-v1.png`,
    });
    // An existing copy is reused, not made again.
    expect(await new File(`${ROOT}/notes-v2.png`).text()).toBe("existing");
    expect(new File(`${ROOT}/timer-v1.png`).exists).toBe(true);
    expect(stale.exists).toBe(false);
    expect([...__files.keys()].some((key) => key.includes("/cache/"))).toBe(
      false,
    );
  });

  it("removes every copy when no app is shown", async () => {
    const directory = new Directory(ROOT);
    new File(directory, "notes-v2.png").create();

    expect(await prepareWidgetIcons([], installer, directory)).toEqual({});
    expect(directory.list()).toEqual([]);
  });
});
