const fs = require("fs");
const path = require("path");
const plist = require("@expo/plist").default;
const { withDangerousMod, withXcodeProject } = require("expo/config-plugins");

const TARGET = "OpenCuriaNotificationService";
const MODULE = path.join(__dirname, "..", "modules", "opencuria-push");
const SHARED = "OpenCuriaPushCrypto.swift";
const SOURCES = {
  "NotificationService.swift": path.join(
    MODULE,
    "extension",
    "NotificationService.swift",
  ),
  [SHARED]: path.join(MODULE, "ios", "Shared", SHARED),
};
// expo-widgets' Live Activity extension; it decrypts pushed content state.
const WIDGET_TARGET = "ExpoWidgetsTarget";

const unquote = (value) =>
  typeof value === "string" ? value.replace(/^"(.*)"$/, "$1") : value;

function infoPlist({ groupIdentifier, version, buildNumber }) {
  return plist.build({
    CFBundleDevelopmentRegion: "$(DEVELOPMENT_LANGUAGE)",
    CFBundleDisplayName: TARGET,
    CFBundleExecutable: "$(EXECUTABLE_NAME)",
    CFBundleIdentifier: "$(PRODUCT_BUNDLE_IDENTIFIER)",
    CFBundleInfoDictionaryVersion: "6.0",
    CFBundleName: "$(PRODUCT_NAME)",
    CFBundlePackageType: "$(PRODUCT_BUNDLE_PACKAGE_TYPE)",
    CFBundleShortVersionString: version,
    CFBundleVersion: buildNumber,
    // Read by OpenCuriaPushCrypto.swift to find the shared keychain.
    ExpoWidgetsAppGroupIdentifier: groupIdentifier,
    NSExtension: {
      NSExtensionPointIdentifier: "com.apple.usernotifications.service",
      NSExtensionPrincipalClass: "$(PRODUCT_MODULE_NAME).NotificationService",
    },
  });
}

/** Writes the extension's sources and copies the shared decryption code. */
const withExtensionFiles = (config, { groupIdentifier }) =>
  withDangerousMod(config, [
    "ios",
    async (config) => {
      const root = config.modRequest.platformProjectRoot;
      const directory = path.join(root, TARGET);
      fs.mkdirSync(directory, { recursive: true });
      for (const [name, source] of Object.entries(SOURCES)) {
        fs.copyFileSync(source, path.join(directory, name));
      }
      fs.writeFileSync(
        path.join(directory, "Info.plist"),
        infoPlist({
          groupIdentifier,
          version: config.ios?.version ?? config.version ?? "1.0",
          buildNumber: config.ios?.buildNumber ?? "1",
        }),
      );
      fs.writeFileSync(
        path.join(directory, `${TARGET}.entitlements`),
        plist.build({
          "com.apple.security.application-groups": [groupIdentifier],
        }),
      );
      const widget = path.join(root, WIDGET_TARGET);
      if (!fs.existsSync(widget)) {
        throw new Error(
          `${WIDGET_TARGET} is missing: list withNotificationServiceExtension before expo-widgets.`,
        );
      }
      fs.copyFileSync(SOURCES[SHARED], path.join(widget, SHARED));
      return config;
    },
  ]);

/**
 * Adds `fileName` (already in the target's folder) to a target's group and
 * Sources phase. The xcode library reuses any file reference with the same
 * path, which would point the widget at the extension's copy, so the
 * reference is created here. Idempotent.
 */
function addSourceToTarget(project, targetName, fileName) {
  const targetKey = project.findTargetKey(targetName);
  const groupKey = project.findPBXGroupKey({ name: targetName });
  if (!targetKey || !groupKey) {
    throw new Error(`Xcode target or group ${targetName} not found`);
  }
  const group = project.getPBXGroupByKey(groupKey);
  if (group.children.some((child) => unquote(child.comment) === fileName)) {
    return;
  }
  const objects = project.hash.project.objects;
  const fileRef = project.generateUuid();
  const buildFile = project.generateUuid();
  objects.PBXFileReference[fileRef] = {
    isa: "PBXFileReference",
    lastKnownFileType: "sourcecode.swift",
    path: fileName,
    sourceTree: '"<group>"',
  };
  objects.PBXFileReference[`${fileRef}_comment`] = fileName;
  objects.PBXBuildFile[buildFile] = {
    isa: "PBXBuildFile",
    fileRef,
    fileRef_comment: fileName,
  };
  objects.PBXBuildFile[`${buildFile}_comment`] = `${fileName} in Sources`;
  group.children.push({ value: fileRef, comment: fileName });
  project
    .pbxSourcesBuildPhaseObj(targetKey)
    .files.push({ value: buildFile, comment: `${fileName} in Sources` });
}

/** Adds the extension target (embedded in the app) and wires the shared file. */
const withExtensionTarget = (config) =>
  withXcodeProject(config, (config) => {
    const project = config.modResults;
    const bundleIdentifier = `${config.ios.bundleIdentifier}.NotificationService`;
    if (!project.pbxTargetByName(TARGET)) {
      const objects = project.hash.project.objects;
      objects.PBXTargetDependency = objects.PBXTargetDependency ?? {};
      objects.PBXContainerItemProxy = objects.PBXContainerItemProxy ?? {};
      // Info.plist stays out of the group: the widget's has the same path.
      const group = project.addPbxGroup(
        [...Object.keys(SOURCES), `${TARGET}.entitlements`],
        TARGET,
        TARGET,
      );
      const mainGroup = project.getFirstProject().firstProject.mainGroup;
      project.addToPbxGroup(group.uuid, mainGroup);
      const target = project.addTarget(
        TARGET,
        "app_extension",
        TARGET,
        bundleIdentifier,
      );
      project.addBuildPhase(
        Object.keys(SOURCES),
        "PBXSourcesBuildPhase",
        "Sources",
        target.uuid,
      );
      project.addBuildPhase(
        [],
        "PBXResourcesBuildPhase",
        "Resources",
        target.uuid,
      );
      project.addBuildPhase(
        [],
        "PBXFrameworksBuildPhase",
        "Frameworks",
        target.uuid,
      );
      const configurations = project.pbxXCBuildConfigurationSection();
      const list =
        project.pbxXCConfigurationList()[
          target.pbxNativeTarget.buildConfigurationList
        ];
      for (const { value } of list.buildConfigurations) {
        Object.assign(configurations[value].buildSettings, {
          INFOPLIST_FILE: `${TARGET}/Info.plist`,
          CODE_SIGN_ENTITLEMENTS: `${TARGET}/${TARGET}.entitlements`,
          CODE_SIGN_STYLE: "Automatic",
          IPHONEOS_DEPLOYMENT_TARGET: config.ios?.deploymentTarget ?? "16.4",
          TARGETED_DEVICE_FAMILY: '"1,2"',
          SWIFT_VERSION: "5.0",
          MARKETING_VERSION: `"${config.ios?.version ?? config.version ?? "1.0"}"`,
          CURRENT_PROJECT_VERSION: `"${config.ios?.buildNumber ?? "1"}"`,
          APPLICATION_EXTENSION_API_ONLY: "YES",
          ...(config.ios?.appleTeamId
            ? { DEVELOPMENT_TEAM: config.ios.appleTeamId }
            : {}),
        });
      }
    }
    addSourceToTarget(project, WIDGET_TARGET, SHARED);
    return config;
  });

/** EAS signs every extension; this one shares the app group's keychain. */
function withEasExtension(config, { groupIdentifier }) {
  const bundleIdentifier = `${config.ios.bundleIdentifier}.NotificationService`;
  const extensions = (
    config.extra?.eas?.build?.experimental?.ios?.appExtensions ?? []
  ).filter((extension) => extension.targetName !== TARGET);
  config.extra = {
    ...config.extra,
    eas: {
      ...config.extra?.eas,
      build: {
        ...config.extra?.eas?.build,
        experimental: {
          ...config.extra?.eas?.build?.experimental,
          ios: {
            ...config.extra?.eas?.build?.experimental?.ios,
            appExtensions: [
              ...extensions,
              {
                targetName: TARGET,
                bundleIdentifier,
                entitlements: {
                  "com.apple.security.application-groups": [groupIdentifier],
                },
              },
            ],
          },
        },
      },
    },
  };
  return config;
}

/**
 * iOS notification service extension that decrypts push proxy messages
 * (modules/opencuria-push). List it before expo-widgets: Expo runs these mods
 * after the widget extension exists, which then also gets the decryption code.
 */
const withNotificationServiceExtension = (config, props) => {
  if (!props?.groupIdentifier) {
    throw new Error("withNotificationServiceExtension needs a groupIdentifier");
  }
  if (!config.ios?.bundleIdentifier) {
    throw new Error(
      "withNotificationServiceExtension needs ios.bundleIdentifier",
    );
  }
  config = withEasExtension(config, props);
  config = withExtensionFiles(config, props);
  return withExtensionTarget(config);
};

module.exports = withNotificationServiceExtension;
module.exports.addSourceToTarget = addSourceToTarget;
module.exports.TARGET = TARGET;
