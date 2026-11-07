import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Platform } from "react-native";
import Constants from "expo-constants";
import type { OpenCuriaClient, PublishedApp } from "@opencuria/client";
import { showToast } from "../ui/toast";
import { errorText } from "../workspaces/workspaceOperations";
import { AppIconCache, appIconSource } from "./appIcons";
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
  type AppGatewayLaunch,
} from "./appRuntime";
import { createExpoAppFileSystem } from "./appStorage";

let sharedInstaller: AppInstaller | null = null;
let sharedIcons: AppIconCache | null = null;

/** One installer per process: it serializes installs and owns the registry. */
export function appInstaller(): AppInstaller {
  sharedInstaller ??= new AppInstaller(createExpoAppFileSystem());
  return sharedInstaller;
}

function appIconCache(): AppIconCache {
  sharedIcons ??= new AppIconCache();
  return sharedIcons;
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
  /** The server is unreachable: only installed apps show, and they open offline. */
  offline: boolean;
  platform: AppPlatform;
  refresh: () => Promise<void>;
  /** Install the newest version, or update to it; resolves to success. */
  install: (appId: string) => Promise<boolean>;
  open: (appId: string) => Promise<void>;
  uninstall: (appId: string) => Promise<void>;
  /** Delete the app from the server; an installed copy stays. */
  remove: (appId: string) => Promise<void>;
  /** Small copies of the apps' icons (`file://`) by app id, once they exist. */
  icons: Readonly<Record<string, string>>;
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
 * The route from the app to processes of its workspace, or `null` when the
 * server cannot be reached (installed apps still open offline).
 */
async function appGateway(
  client: OpenCuriaClient | null,
  appId: string,
): Promise<AppGatewayLaunch | null> {
  if (!client) return null;
  try {
    const { url, token, domain } = await client.apps.gatewaySession(appId);
    return { url, token, domain };
  } catch {
    return null;
  }
}

/** Apps that are starting; another request for one of them is dropped. */
const opening = new Set<string>();

/**
 * Open the copy of an app installed for `scope`, from the Apps list, a chat
 * or the home screen widget. Resolves to false when this device has no such
 * copy; a copy that fails to start shows an error.
 */
export async function openApp(
  client: OpenCuriaClient | null,
  scope: string,
  appId: string,
  installer: AppInstaller = appInstaller(),
): Promise<boolean> {
  // From the registry, not the last render: right after an update the
  // rows still show the previous version, whose files are already gone.
  const copy = (await installer.installed()).find(
    (app) => app.appId === appId && app.scope === scope,
  );
  if (!copy) return false;
  if (opening.has(appId)) return true;
  opening.add(appId);
  try {
    const [request, gateway] = await Promise.all([
      installer.launchRequest(copy),
      appGateway(client, appId),
    ]);
    await openInstalledApp(gateway ? { ...request, gateway } : request);
    // Recently used apps lead the home screen widget.
    void installer.markOpened(appId).catch(() => undefined);
  } catch (cause) {
    showToast({
      title: `Could not open ${copy.name}`,
      message: errorText(cause, "Install it again."),
      tone: "error",
    });
  } finally {
    opening.delete(appId);
  }
  return true;
}

/**
 * The apps agents published to the signed-in user, joined with the ones
 * installed on this device. Installed apps open offline; the server list
 * refreshes on demand and whenever a run finishes. While `offline`, the
 * server is not asked and only installed apps show.
 */
export function useApps(
  client: OpenCuriaClient | null,
  scope: string,
  installer: AppInstaller = appInstaller(),
  {
    offline = false,
    icons: iconCache = appIconCache(),
  }: {
    offline?: boolean;
    icons?: AppIconCache;
  } = {},
): AppsState {
  const [published, setPublished] = useState<PublishedApp[]>([]);
  const [installed, setInstalled] = useState<InstalledApp[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<Record<string, number>>({});
  const [icons, setIcons] = useState<Record<string, string>>({});
  const generation = useRef(0);
  const server = offline ? null : client;

  const reloadInstalled = useCallback(async () => {
    setInstalled(await installer.installed());
  }, [installer]);

  const refresh = useCallback(async () => {
    void reloadInstalled();
    const client = server;
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
  }, [server, reloadInstalled]);

  useEffect(() => {
    generation.current += 1;
    setPublished([]);
    setLoaded(false);
    setError(null);
    void refresh();
    const client = server;
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
  }, [server, scope, refresh]);

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

  useEffect(() => {
    let current = true;
    // Icons of apps that are not installed come from the server (online only).
    const fileRequest = server
      ? (appId: string, versionId: string, sha256: string) =>
          server.apps.fileRequest(appId, versionId, sha256)
      : undefined;
    const sources = rows.flatMap((row) => {
      const source = appIconSource(row, installer, fileRequest);
      return source ? [source] : [];
    });
    void iconCache
      .icons(sources)
      .then((next) => {
        if (current)
          setIcons((previous) => (sameIcons(previous, next) ? previous : next));
      })
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [rows, server, installer, iconCache]);

  // Apps with an install or open running; `progress` only shows after the
  // next render, too late for a quick second tap.
  const pending = useRef(new Set<string>());
  const once = useCallback(
    async <T>(appId: string, idle: T, task: () => Promise<T>): Promise<T> => {
      if (pending.current.has(appId)) return idle;
      pending.current.add(appId);
      try {
        return await task();
      } finally {
        pending.current.delete(appId);
      }
    },
    [],
  );

  const install = useCallback(
    (appId: string): Promise<boolean> =>
      once(appId, false, async () => {
        const row = rowsRef.current.find((item) => item.appId === appId);
        const app = row?.published;
        const version = app?.latest_version;
        if (!client || !app || !version) return false;
        const updating = Boolean(row?.installed);
        setProgress((current) => ({ ...current, [appId]: 0 }));
        try {
          const manifest = await client.apps.manifest(
            app.id,
            version.id,
            PLATFORM,
          );
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
      }),
    [client, installer, once, reloadInstalled, scope],
  );

  const open = useCallback(
    (appId: string): Promise<void> =>
      once(appId, undefined, async () => {
        // Offline, the app opens at once instead of waiting for a route.
        await openApp(server, scope, appId, installer);
      }),
    [server, installer, once, scope],
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

  return {
    rows,
    loaded,
    loading,
    error,
    progress,
    supported: isAppRuntimeAvailable(),
    offline,
    platform: PLATFORM,
    refresh,
    install,
    open,
    uninstall,
    remove,
    icons,
  };
}

function sameIcons(
  left: Readonly<Record<string, string>>,
  right: Readonly<Record<string, string>>,
): boolean {
  const keys = Object.keys(left);
  return (
    keys.length === Object.keys(right).length &&
    keys.every((key) => left[key] === right[key])
  );
}
