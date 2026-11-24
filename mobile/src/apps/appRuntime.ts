import { requireOptionalNativeModule } from "expo-modules-core";

/**
 * Routes the app's `http://<process>.<domain>[:port]` requests to processes
 * of its workspace. Native code rewrites only those requests; the token
 * never reaches the app's JavaScript.
 */
export interface AppGatewayLaunch {
  /** Gateway URL of the app; requests go to `<url>/<process>/<port>/<path>`. */
  url: string;
  /** App session token, sent as `X-OpenCuria-App-Session`. */
  token: string;
  /** `opencuria.local`. */
  domain: string;
}

/**
 * What the native runtime needs to open one installed app version. The
 * runtime finds the app's files from its id (`appLayout` in
 * `appInstaller.ts`), never from paths in the request, so an app can only
 * ever reach its own sandbox.
 */
export interface AppLaunchRequest {
  appId: string;
  versionId: string;
  name: string;
  /** Updates-style manifest; `extra.expoClient` is the app's Expo config. */
  manifest: Record<string, unknown>;
  /** Metro asset hash -> `file://` URI of the downloaded asset. */
  localAssets: Record<string, string>;
  orientation: "default" | "portrait" | "landscape";
  backgroundColor?: string;
  /** Missing offline: backend requests then fail like without network. */
  gateway?: AppGatewayLaunch;
  /**
   * The chat companion over the app: the account the app was installed for
   * and its workspace, whose chats it shows (`src/apps/companion`).
   */
  companion?: {
    scope: string;
    workspaceId: string | null;
    versionName: string;
  };
}

interface AppRuntimeModule {
  open(request: string): Promise<void>;
  clearData(appId: string): Promise<void>;
  /** Missing in development clients built before it existed. */
  close?(): Promise<void>;
  /** Android only: iOS draws its widget with expo-widgets. */
  updateAppsWidget?(snapshot: string): Promise<void>;
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

/** Close the app that is open, if any; OpenCuria shows again. */
export async function closeOpenApp(): Promise<void> {
  await native?.close?.();
}

/** Remove what the native runtime keeps for an app (cache, SecureStore). */
export async function clearAppRuntimeData(appId: string): Promise<void> {
  await native?.clearData(appId);
}

/** Whether this build draws the Android home screen widget. */
export function isAppsWidgetAvailable(): boolean {
  return typeof native?.updateAppsWidget === "function";
}

/** Hand the Android home screen widget its content (JSON `AppsWidgetSnapshot`). */
export async function updateAppsWidget(snapshot: string): Promise<void> {
  await native?.updateAppsWidget?.(snapshot);
}
