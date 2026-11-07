import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Platform } from "react-native";
import Constants from "expo-constants";
import type { OpenCuriaClient, PublishedApp } from "@opencuria/client";
import { showToast } from "../ui/toast";
import { errorText } from "../workspaces/workspaceOperations";
import { AppInstaller, type InstalledApp } from "./appInstaller";
import {
  appRows,
  sdkMajor,
  type AppPlatform,
  type AppRow,
} from "./appPresentation";
import {
  clearAppRuntimeData,
  isAppRuntimeAvailable,
  openInstalledApp,
} from "./appRuntime";
import { createExpoAppFileSystem } from "./appStorage";

let sharedInstaller: AppInstaller | null = null;

/** One installer per process: it serializes installs and owns the registry. */
export function appInstaller(): AppInstaller {
  sharedInstaller ??= new AppInstaller(createExpoAppFileSystem());
  return sharedInstaller;
}

export interface AppsState {
  rows: AppRow[];
  /** The server's list has been fetched once for this account. */
  loaded: boolean;
  loading: boolean;
  error: string | null;
  /** Download progress (0–1) of apps being installed or updated. */
  progress: Readonly<Record<string, number>>;
  /** Whether this build can run apps at all. */
  supported: boolean;
  platform: AppPlatform;
  refresh: () => Promise<void>;
  /** Install the newest version, or update to it; resolves to success. */
  install: (appId: string) => Promise<boolean>;
  open: (appId: string) => Promise<void>;
  uninstall: (appId: string) => Promise<void>;
  /** Delete the app from the server; an installed copy stays. */
  remove: (appId: string) => Promise<void>;
  iconUri: (appId: string) => string | null;
}

/** Run events arrive in bursts; refresh once they settle. */
const EVENT_SETTLE_MS = 600;
/**
 * What a tap on an app does: open it, update and then open it, or install it
 * (installed apps then open from the list).
 */
export async function runPrimaryAction(apps: AppsState, row: AppRow) {
  if (row.state === "installed") await apps.open(row.appId);
  else if (row.state === "update") {
    if (await apps.install(row.appId)) await apps.open(row.appId);
  } else if (row.state === "available") await apps.install(row.appId);
}

const HOST_SDK = sdkMajor(Constants.expoConfig?.sdkVersion);
const PLATFORM: AppPlatform = Platform.OS === "ios" ? "ios" : "android";

/**
 * The apps agents published to the signed-in user, joined with the ones
 * installed on this device. Installed apps open offline; the server list
 * refreshes on demand and whenever a run finishes.
 */
export function useApps(
  client: OpenCuriaClient | null,
  scope: string,
  installer: AppInstaller = appInstaller(),
): AppsState {
  const [published, setPublished] = useState<PublishedApp[]>([]);
  const [installed, setInstalled] = useState<InstalledApp[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<Record<string, number>>({});
  const generation = useRef(0);

  const reloadInstalled = useCallback(async () => {
    setInstalled(await installer.installed());
  }, [installer]);

  const refresh = useCallback(async () => {
    void reloadInstalled();
    if (!client) return;
    const request = ++generation.current;
    setLoading(true);
    try {
      const apps = await client.apps.list();
      if (request !== generation.current) return;
      setPublished(apps);
      setLoaded(true);
      setError(null);
    } catch (cause) {
      if (request === generation.current)
        setError(errorText(cause, "Could not load apps."));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [client, reloadInstalled]);

  useEffect(() => {
    generation.current += 1;
    setPublished([]);
    setLoaded(false);
    setError(null);
    void refresh();
    if (!client) return;
    // Agents publish during runs; a finished run is when a new app appears.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const settle = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void refresh(), EVENT_SETTLE_MS);
    };
    const detach = [
      client.realtime.on("harness.session_status", settle),
      client.realtime.onReconnect(settle),
    ];
    return () => {
      generation.current += 1;
      if (timer) clearTimeout(timer);
      detach.forEach((off) => off());
    };
  }, [client, scope, refresh]);

  const rows = useMemo(
    () =>
      appRows(published, installed, {
        scope,
        platform: PLATFORM,
        hostSdk: HOST_SDK,
      }),
    [published, installed, scope],
  );
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  const install = useCallback(
    async (appId: string): Promise<boolean> => {
      const row = rowsRef.current.find((item) => item.appId === appId);
      const app = row?.published;
      const version = app?.latest_version;
      if (!client || !app || !version) return false;
      const updating = Boolean(row?.installed);
      setProgress((current) => ({ ...current, [appId]: 0 }));
      try {
        const manifest = await client.apps.manifest(app.id, version.id, PLATFORM);
        await installer.install(manifest, {
          scope,
          fileRequest: (sha256) =>
            client.apps.fileRequest(app.id, version.id, sha256),
          onProgress: ({ done, total }) =>
            setProgress((current) => ({
              ...current,
              [appId]: total ? done / total : 1,
            })),
        });
        await reloadInstalled();
        showToast({
          title: updating ? `${app.name} updated` : `${app.name} installed`,
          message: updating ? undefined : "Open it from Apps.",
          tone: "success",
        });
        return true;
      } catch (cause) {
        showToast({
          title: updating
            ? `Could not update ${app.name}`
            : `Could not install ${app.name}`,
          message: errorText(cause, "Check the connection and try again."),
          tone: "error",
        });
        return false;
      } finally {
        setProgress(({ [appId]: _done, ...rest }) => rest);
      }
    },
    [client, installer, reloadInstalled, scope],
  );

  const open = useCallback(
    async (appId: string) => {
      const copy = rowsRef.current.find((row) => row.appId === appId)?.installed;
      if (!copy) return;
      try {
        await openInstalledApp(await installer.launchRequest(copy));
      } catch (cause) {
        showToast({
          title: `Could not open ${copy.name}`,
          message: errorText(cause, "Install it again."),
          tone: "error",
        });
      }
    },
    [installer],
  );

  const uninstall = useCallback(
    async (appId: string) => {
      const name = rowsRef.current.find((row) => row.appId === appId)?.name;
      try {
        await installer.uninstall(appId);
        await clearAppRuntimeData(appId);
        await reloadInstalled();
        showToast({ title: `${name ?? "App"} removed`, tone: "success" });
      } catch (cause) {
        showToast({
          title: "Could not remove the app",
          message: errorText(cause, "Please try again."),
          tone: "error",
        });
      }
    },
    [installer, reloadInstalled],
  );

  const remove = useCallback(
    async (appId: string) => {
      if (!client) return;
      const name = rowsRef.current.find((row) => row.appId === appId)?.name;
      try {
        await client.apps.delete(appId);
        setPublished((apps) => apps.filter((app) => app.id !== appId));
        showToast({ title: `${name ?? "App"} deleted`, tone: "success" });
      } catch (cause) {
        showToast({
          title: "Could not delete the app",
          message: errorText(cause, "Please try again."),
          tone: "error",
        });
      }
    },
    [client],
  );

  const iconUri = useCallback(
    (appId: string) => {
      const copy = rowsRef.current.find((row) => row.appId === appId)?.installed;
      return copy ? installer.iconUri(copy) : null;
    },
    [installer],
  );

  return {
    rows,
    loaded,
    loading,
    error,
    progress,
    supported: isAppRuntimeAvailable(),
    platform: PLATFORM,
    refresh,
    install,
    open,
    uninstall,
    remove,
    iconUri,
  };
}
