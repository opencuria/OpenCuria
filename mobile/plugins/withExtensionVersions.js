const { withXcodeProject } = require("expo/config-plugins");

const PHASE_NAME = "[OpenCuria] Copy the app version";
const VERSION_KEYS = ["CFBundleVersion", "CFBundleShortVersionString"];

const unquote = (value) =>
  typeof value === "string" ? value.replace(/^"(.*)"$/, "$1") : value;

function buildSettingsOf(project, target) {
  const list = project.pbxXCConfigurationList()[target.buildConfigurationList];
  const configurations = project.pbxXCBuildConfigurationSection();
  return (list?.buildConfigurations ?? [])
    .map(({ value }) => configurations[value]?.buildSettings)
    .filter(Boolean);
}

/** A shell path for an INFOPLIST_FILE value, which is relative to SRCROOT. */
function shellPath(infoPlistFile) {
  const path = infoPlistFile.replace(/\$\((\w+)\)/g, "${$1}");
  return path.startsWith("/") || path.startsWith("${") ? path : `\${SRCROOT}/${path}`;
}

function script(appInfoPlist) {
  return `# EAS writes its build number into the app's Info.plist; extensions must match.
app_plist="${shellPath(appInfoPlist)}"
built_plist="\${TARGET_BUILD_DIR}/\${INFOPLIST_PATH}"
for key in ${VERSION_KEYS.join(" ")}; do
  value=$(/usr/libexec/PlistBuddy -c "Print :$key" "$app_plist" 2>/dev/null) || continue
  case "$value" in ""|*'$'*) continue ;; esac
  /usr/libexec/PlistBuddy -c "Set :$key $value" "$built_plist"
done
`;
}

/**
 * Adds a last build phase to every app extension that copies the app's
 * version and build number into the extension's built Info.plist, before it
 * is signed. EAS writes its remote build number into each target's
 * Info.plist, but extensions that generate theirs (expo-widgets) take
 * CFBundleVersion from a fixed CURRENT_PROJECT_VERSION, which App Store
 * Connect reports as a mismatch with the app (ITMS-90473). Values that still
 * hold a build setting (`$(…)`) are left alone. Returns the extensions.
 */
function addExtensionVersionPhases(project, appTargetName) {
  const targets = project.pbxNativeTargetSection();
  const appTarget = targets[project.findTargetKey(appTargetName) ?? ""];
  const appInfoPlist = appTarget
    ? buildSettingsOf(project, appTarget).map((s) => unquote(s.INFOPLIST_FILE)).find(Boolean)
    : undefined;
  if (!appInfoPlist) {
    return [];
  }
  const extensions = [];
  for (const [key, target] of Object.entries(targets)) {
    if (
      key.endsWith("_comment") ||
      unquote(target.productType) !== "com.apple.product-type.app-extension"
    ) {
      continue;
    }
    // The phase edits the built Info.plist, which it cannot declare as an
    // output (Xcode already produces it), so it runs outside the sandbox.
    for (const settings of buildSettingsOf(project, target)) {
      settings.ENABLE_USER_SCRIPT_SANDBOXING = "NO";
    }
    if (!(target.buildPhases ?? []).some((phase) => phase.comment === PHASE_NAME)) {
      const { buildPhase } = project.addBuildPhase([], "PBXShellScriptBuildPhase", PHASE_NAME, key, {
        shellPath: "/bin/sh",
        shellScript: script(appInfoPlist),
      });
      buildPhase.alwaysOutOfDate = 1;
    }
    extensions.push(unquote(target.name));
  }
  return extensions;
}

/** Expo runs Xcode project mods in reverse order: list this before plugins that add extensions. */
const withExtensionVersions = (config) =>
  withXcodeProject(config, (config) => {
    addExtensionVersionPhases(config.modResults, config.modRequest.projectName);
    return config;
  });

module.exports = withExtensionVersions;
module.exports.addExtensionVersionPhases = addExtensionVersionPhases;
module.exports.PHASE_NAME = PHASE_NAME;
