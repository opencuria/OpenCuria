import { requireOptionalNativeModule } from "expo-modules-core";

/** What the native runtime needs to open one installed app version. */
export interface AppLaunchRequest {
  appId: string;
  versionId: string;
  name: string;
  /** Absolute path of the Hermes bundle. */
  bundlePath: string;
  /** Absolute path of the app's installed files (read-only for the app). */
  installDir: string;
  /** Absolute path of the app's own data directory. */
  dataDir: string;
  /** Updates-style manifest; `extra.expoClient` is the app's Expo config. */
  manifest: Record<string, unknown>;
  /** Metro asset hash -> `file://` URI of the downloaded asset. */
  localAssets: Record<string, string>;
  orientation: "default" | "portrait" | "landscape";
  backgroundColor?: string;
  /** Absolute path of the app icon, shown while the app starts. */
  iconPath?: string;
}

interface AppRuntimeModule {
  open(request: string): Promise<void>;
  clearData(appId: string): Promise<void>;
}

const native = requireOptionalNativeModule<AppRuntimeModule>(
  "OpenCuriaAppRuntime",
);

/** Whether this build can run installed apps (`modules/opencuria-app-runtime`). */
export function isAppRuntimeAvailable(): boolean {
  return native !== null;
}

/** Open an installed app inside OpenCuria, like an Expo Go experience. */
export async function openInstalledApp(
  request: AppLaunchRequest,
): Promise<void> {
  if (!native) throw new Error("This build of OpenCuria cannot open apps.");
  await native.open(JSON.stringify(request));
}

/** Remove what the native runtime keeps for an app (cache, SecureStore). */
export async function clearAppRuntimeData(appId: string): Promise<void> {
  await native?.clearData(appId);
}
