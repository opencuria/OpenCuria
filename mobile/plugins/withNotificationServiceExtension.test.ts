import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const xcode = require("xcode");
const withNotificationServiceExtension = require("./withNotificationServiceExtension.js");
const { addSourceToTarget, TARGET } = withNotificationServiceExtension;

/** The widget extension as expo-widgets leaves it, next to the service extension. */
function project() {
  const result = xcode.project("project.pbxproj");
  result.hash = {
    project: {
      objects: {
        PBXNativeTarget: {
          WIDGET: {
            name: "ExpoWidgetsTarget",
            buildPhases: [{ value: "SOURCES", comment: "Sources" }],
          },
          WIDGET_comment: "ExpoWidgetsTarget",
        },
        PBXSourcesBuildPhase: {
          SOURCES: { isa: "PBXSourcesBuildPhase", files: [] },
          SOURCES_comment: "Sources",
        },
        PBXGroup: {
          WIDGET_GROUP: {
            name: "ExpoWidgetsTarget",
            path: "ExpoWidgetsTarget",
            children: [],
          },
          WIDGET_GROUP_comment: "ExpoWidgetsTarget",
          NSE_GROUP: {
            name: TARGET,
            path: TARGET,
            children: [
              { value: "NSE_FILE", comment: "OpenCuriaPushCrypto.swift" },
            ],
          },
          NSE_GROUP_comment: TARGET,
        },
        PBXFileReference: {
          NSE_FILE: {
            isa: "PBXFileReference",
            path: "OpenCuriaPushCrypto.swift",
          },
          NSE_FILE_comment: "OpenCuriaPushCrypto.swift",
        },
        PBXBuildFile: {},
      },
    },
  };
  return result;
}

describe("withNotificationServiceExtension", () => {
  it("gives the widget its own reference to the shared decryption code, once", () => {
    const p = project();
    addSourceToTarget(p, "ExpoWidgetsTarget", "OpenCuriaPushCrypto.swift");
    addSourceToTarget(p, "ExpoWidgetsTarget", "OpenCuriaPushCrypto.swift");
    const objects = p.hash.project.objects;
    const [child] = objects.PBXGroup.WIDGET_GROUP.children;
    expect(objects.PBXGroup.WIDGET_GROUP.children).toHaveLength(1);
    expect(child.value).not.toBe("NSE_FILE");
    expect(objects.PBXFileReference[child.value]).toMatchObject({
      path: "OpenCuriaPushCrypto.swift",
      sourceTree: '"<group>"',
    });
    const [built] = objects.PBXSourcesBuildPhase.SOURCES.files;
    expect(objects.PBXSourcesBuildPhase.SOURCES.files).toHaveLength(1);
    expect(objects.PBXBuildFile[built.value].fileRef).toBe(child.value);
  });

  it("registers the extension with EAS in the shared app group", () => {
    const base = {
      name: "OpenCuria",
      slug: "opencuria",
      ios: { bundleIdentifier: "com.example.app" },
    };
    const props = { groupIdentifier: "group.com.example.app" };
    const config = withNotificationServiceExtension(
      withNotificationServiceExtension(base, props),
      props,
    );
    expect(config.extra.eas.build.experimental.ios.appExtensions).toEqual([
      {
        targetName: TARGET,
        bundleIdentifier: "com.example.app.NotificationService",
        entitlements: {
          "com.apple.security.application-groups": ["group.com.example.app"],
        },
      },
    ]);
    expect(() => withNotificationServiceExtension(base, {})).toThrow(
      /groupIdentifier/,
    );
  });
});
