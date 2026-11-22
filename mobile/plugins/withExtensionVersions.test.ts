import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const xcode = require("xcode");
const { addExtensionVersionPhases, PHASE_NAME } = require("./withExtensionVersions.js");

type Target = { name: string; productType: string; buildConfigurationList: string; buildPhases: { comment: string }[] };

/** An Xcode project as expo prebuild leaves it: the app and the widget extension. */
function project(appInfoPlist: string | null = "OpenCuria/Info.plist") {
  const target = (name: string, type: string, list: string): Target => ({
    name,
    productType: `"com.apple.product-type.${type}"`,
    buildConfigurationList: list,
    buildPhases: [],
  });
  const config = (name: string, buildSettings: Record<string, string>) => ({
    isa: "XCBuildConfiguration",
    name,
    buildSettings,
  });
  const appSettings = appInfoPlist ? { INFOPLIST_FILE: appInfoPlist } : {};
  const result = xcode.project("project.pbxproj");
  result.hash = {
    project: {
      objects: {
        PBXNativeTarget: {
          APP: target("OpenCuria", "application", "APP_LIST"),
          APP_comment: "OpenCuria",
          WIDGET: target("ExpoWidgetsTarget", "app-extension", "WIDGET_LIST"),
          WIDGET_comment: "ExpoWidgetsTarget",
        },
        XCConfigurationList: {
          APP_LIST: { buildConfigurations: [{ value: "APP_DEBUG" }, { value: "APP_RELEASE" }] },
          WIDGET_LIST: { buildConfigurations: [{ value: "WIDGET_DEBUG" }, { value: "WIDGET_RELEASE" }] },
        },
        XCBuildConfiguration: {
          APP_DEBUG: config("Debug", { ...appSettings }),
          APP_RELEASE: config("Release", { ...appSettings }),
          WIDGET_DEBUG: config("Debug", { CURRENT_PROJECT_VERSION: '"1"' }),
          WIDGET_RELEASE: config("Release", { CURRENT_PROJECT_VERSION: '"1"' }),
        },
      },
    },
  };
  return result;
}

const objects = (p: ReturnType<typeof project>) => p.hash.project.objects;
const phasesOf = (p: ReturnType<typeof project>, key: string) =>
  (objects(p).PBXNativeTarget[key] as Target).buildPhases.filter((phase) => phase.comment === PHASE_NAME);
const scriptOf = (p: ReturnType<typeof project>) =>
  Object.entries(objects(p).PBXShellScriptBuildPhase as Record<string, { shellScript: string }>).find(
    ([key]) => !key.endsWith("_comment"),
  )?.[1].shellScript;

describe("addExtensionVersionPhases", () => {
  it("copies the app's versions into each extension, last and outside the sandbox", () => {
    const p = project();

    expect(addExtensionVersionPhases(p, "OpenCuria")).toEqual(["ExpoWidgetsTarget"]);

    expect(phasesOf(p, "WIDGET")).toHaveLength(1);
    expect(phasesOf(p, "APP")).toHaveLength(0);
    expect(scriptOf(p)).toContain('app_plist=\\"${SRCROOT}/OpenCuria/Info.plist\\"');
    expect(scriptOf(p)).toContain("CFBundleVersion CFBundleShortVersionString");
    expect(objects(p).XCBuildConfiguration.WIDGET_RELEASE.buildSettings.ENABLE_USER_SCRIPT_SANDBOXING).toBe("NO");
    expect(objects(p).XCBuildConfiguration.APP_RELEASE.buildSettings.ENABLE_USER_SCRIPT_SANDBOXING).toBeUndefined();
  });

  it("adds the phase once across repeated prebuilds", () => {
    const p = project();
    addExtensionVersionPhases(p, "OpenCuria");
    addExtensionVersionPhases(p, "OpenCuria");

    expect(phasesOf(p, "WIDGET")).toHaveLength(1);
  });

  it("evaluates build settings in the app's Info.plist path for the shell", () => {
    const p = project('"$(SRCROOT)/OpenCuria/Info.plist"');
    addExtensionVersionPhases(p, "OpenCuria");

    expect(scriptOf(p)).toContain('app_plist=\\"${SRCROOT}/OpenCuria/Info.plist\\"');
  });

  it("changes nothing without the app target or its Info.plist", () => {
    const unknown = project();
    expect(addExtensionVersionPhases(unknown, "Other")).toEqual([]);
    expect(phasesOf(unknown, "WIDGET")).toHaveLength(0);

    const withoutPlist = project(null);
    expect(addExtensionVersionPhases(withoutPlist, "OpenCuria")).toEqual([]);
    expect(phasesOf(withoutPlist, "WIDGET")).toHaveLength(0);
  });
});
