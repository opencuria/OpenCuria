import React from "react";
import { Alert, type AlertButton } from "react-native";
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react-native";
import type {
  AppManifest,
  OpenCuriaClient,
  PublishedApp,
} from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import type { AppInstaller, InstalledApp } from "./appInstaller";
import { appRows, appScope } from "./appPresentation";
import { AppsSheet } from "./AppsSheet";
import { PublishedAppCard } from "./PublishedAppCard";
import { runPrimaryAction, useApps, type AppsState } from "./useApps";

const mockOpenInstalledApp = jest.fn(async () => undefined);
const mockClearData = jest.fn(async () => undefined);
jest.mock("./appRuntime", () => ({
  isAppRuntimeAvailable: () => true,
  openInstalledApp: (request: unknown) => mockOpenInstalledApp(request),
  clearAppRuntimeData: (appId: string) => mockClearData(appId),
}));
jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

const SCOPE = appScope("https://curia.test", "org", 7);

function published(id: string, name: string, number = 1): PublishedApp {
  return {
    id,
    name,
    slug: name.toLowerCase(),
    workspace_id: "ws",
    workspace_name: "dev",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    latest_version: {
      id: `${id}-v${number}`,
      number,
      version_name: `1.${number}.0`,
      sdk_version: "57.0.0",
      notes: "",
      created_at: "2026-11-01T00:00:00Z",
      icon_sha256: null,
      platforms: { android: 2_000_000, ios: 2_000_000 },
    },
  };
}

function installed(appId: string, name: string, number = 1): InstalledApp {
  return {
    appId,
    scope: SCOPE,
    name,
    slug: name.toLowerCase(),
    versionId: `${appId}-v${number}`,
    versionNumber: number,
    versionName: `1.${number}.0`,
    sdkVersion: "57.0.0",
    publishedAt: "2026-10-01T00:00:00Z",
    installedAt: "2026-10-02T00:00:00Z",
    hasIcon: false,
  };
}

function state(
  overrides: Partial<AppsState> = {},
  rows = appRows(
    [
      published("todo", "Todo"),
      published("tip", "Tip", 2),
      published("game", "Game"),
    ],
    [installed("todo", "Todo"), installed("tip", "Tip", 1)],
    { scope: SCOPE, platform: "android", hostSdk: 57 },
  ),
): AppsState {
  return {
    rows,
    loaded: true,
    loading: false,
    error: null,
    progress: {},
    supported: true,
    platform: "android",
    refresh: jest.fn(async () => undefined),
    install: jest.fn(async () => true),
    open: jest.fn(async () => undefined),
    uninstall: jest.fn(async () => undefined),
    remove: jest.fn(async () => undefined),
    iconUri: () => null,
    ...overrides,
  };
}

function renderSheet(apps: AppsState) {
  return render(
    <ThemeProvider initialMode="light">
      <AppsSheet visible onClose={jest.fn()} apps={apps} />
    </ThemeProvider>,
  );
}

afterEach(() => jest.restoreAllMocks());

describe("AppsSheet", () => {
  it("groups installed apps and apps to install with their next step", async () => {
    const apps = state();
    renderSheet(apps);
    expect(apps.refresh).toHaveBeenCalled();
    expect(screen.getByText("Installed")).toBeTruthy();
    expect(screen.getByText("Ready to install")).toBeTruthy();
    expect(screen.getByText("Version 1.1.0 → 1.2.0")).toBeTruthy();
    expect(screen.getByText("Update")).toBeTruthy();
    expect(screen.getByText("Install")).toBeTruthy();

    await act(async () =>
      fireEvent.press(screen.getByRole("button", { name: "Todo, Version 1.1.0" })),
    );
    expect(apps.open).toHaveBeenCalledWith("todo");

    await act(async () =>
      fireEvent.press(
        screen.getByRole("button", { name: "Game, Version 1.1.0 · 1.9 MB" }),
      ),
    );
    expect(apps.install).toHaveBeenCalledWith("game");
    expect(apps.open).not.toHaveBeenCalledWith("game");
  });

  it("updates an outdated app before opening it", async () => {
    const apps = state();
    renderSheet(apps);
    await act(async () =>
      fireEvent.press(
        screen.getByRole("button", { name: "Tip, Version 1.1.0 → 1.2.0" }),
      ),
    );
    expect(apps.install).toHaveBeenCalledWith("tip");
    expect(apps.open).toHaveBeenCalledWith("tip");
  });

  it("confirms before removing an app from the device", () => {
    const alert = jest
      .spyOn(Alert, "alert")
      .mockImplementation((_title, _message, buttons?: AlertButton[]) => {
        buttons?.find((button) => button.style === "destructive")?.onPress?.();
      });
    const apps = state();
    renderSheet(apps);
    fireEvent.press(screen.getByRole("button", { name: "Actions for Todo" }));
    fireEvent.press(screen.getByText("Remove from device"));
    expect(alert).toHaveBeenCalledWith(
      "Remove Todo?",
      "The app and its data leave this device.",
      expect.any(Array),
    );
    expect(apps.uninstall).toHaveBeenCalledWith("todo");
  });

  it("keeps installed apps usable while the server is unreachable", () => {
    const apps = state({ error: "Network request failed" });
    renderSheet(apps);
    expect(screen.getByText(/Installed apps still open/)).toBeTruthy();
    expect(screen.getByText("Installed")).toBeTruthy();
  });

  it("shows progress and explains the empty list", () => {
    const busy = state({ progress: { game: 0.42 } });
    const { unmount } = renderSheet(busy);
    expect(screen.getByText("Downloading… 42%")).toBeTruthy();
    unmount();
    renderSheet(state({}, []));
    expect(screen.getByText("No apps yet")).toBeTruthy();
  });
});

describe("PublishedAppCard", () => {
  const reference = {
    appId: "tip",
    versionId: "tip-v2",
    name: "Tip",
    version: 2,
    versionName: "1.2.0",
  };

  it("offers the action that fits the installed copy", async () => {
    const apps = state();
    render(
      <ThemeProvider initialMode="light">
        <PublishedAppCard reference={reference} apps={apps} />
      </ThemeProvider>,
    );
    expect(screen.getByText("Version 1.1.0 → 1.2.0")).toBeTruthy();
    await act(async () =>
      fireEvent.press(screen.getByTestId("published-app-action-tip")),
    );
    expect(apps.install).toHaveBeenCalledWith("tip");
    expect(apps.open).toHaveBeenCalledWith("tip");
  });

  it("says when the app is gone", () => {
    render(
      <ThemeProvider initialMode="light">
        <PublishedAppCard reference={reference} apps={state({}, [])} />
      </ThemeProvider>,
    );
    expect(screen.getByText("No longer available")).toBeTruthy();
    expect(screen.queryByTestId("published-app-action-tip")).toBeNull();
  });
});

describe("useApps", () => {
  const manifest = { app_id: "game", version_id: "game-v1" } as AppManifest;

  function client(apps: PublishedApp[]) {
    return {
      api: { baseUrl: "https://curia.test" },
      apps: {
        list: jest.fn(async () => apps),
        manifest: jest.fn(async () => manifest),
        fileRequest: jest.fn((app: string, version: string, sha: string) => ({
          url: `https://curia.test/apps/${app}/${version}/${sha}`,
          headers: {},
        })),
        delete: jest.fn(async () => undefined),
        gatewaySession: jest.fn(async (app: string) => ({
          url: `https://curia.test/ws/app-backends/${app}`,
          token: "app-session",
          expires_at: "2026-11-07T12:00:00Z",
          domain: "opencuria.local",
        })),
      },
      realtime: {
        on: jest.fn(() => () => undefined),
        onReconnect: jest.fn(() => () => undefined),
      },
    };
  }

  function installer(copies: InstalledApp[] = []) {
    const local = [...copies];
    return {
      installed: jest.fn(async () => [...local]),
      install: jest.fn(async (_manifest: AppManifest, options) => {
        options.fileRequest("sha-1");
        options.onProgress?.({ done: 1, total: 2 });
        const copy = installed("game", "Game");
        local.push(copy);
        return copy;
      }),
      uninstall: jest.fn(async (appId: string) => {
        local.splice(
          local.findIndex((copy) => copy.appId === appId),
          1,
        );
      }),
      launchRequest: jest.fn(async (copy: InstalledApp) => ({
        appId: copy.appId,
      })),
      iconUri: () => null,
    };
  }

  it("installs, opens and removes apps through the installer and runtime", async () => {
    const api = client([published("game", "Game")]);
    const local = installer();
    const { result } = renderHook(() =>
      useApps(
        api as unknown as OpenCuriaClient,
        SCOPE,
        local as unknown as AppInstaller,
      ),
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.rows[0]?.state).toBe("available");

    await act(async () => {
      expect(await result.current.install("game")).toBe(true);
    });
    expect(api.apps.manifest).toHaveBeenCalledWith(
      "game",
      "game-v1",
      expect.stringMatching(/^(android|ios)$/),
    );
    expect(api.apps.fileRequest).toHaveBeenCalledWith(
      "game",
      "game-v1",
      "sha-1",
    );
    expect(local.install).toHaveBeenCalledWith(
      manifest,
      expect.objectContaining({ scope: SCOPE }),
    );
    expect(result.current.rows[0]?.state).toBe("installed");
    expect(result.current.progress).toEqual({});

    await act(async () => result.current.open("game"));
    expect(api.apps.gatewaySession).toHaveBeenCalledWith("game");
    expect(mockOpenInstalledApp).toHaveBeenCalledWith({
      appId: "game",
      gateway: {
        url: "https://curia.test/ws/app-backends/game",
        token: "app-session",
        domain: "opencuria.local",
      },
    });

    await act(async () => result.current.uninstall("game"));
    expect(local.uninstall).toHaveBeenCalledWith("game");
    expect(mockClearData).toHaveBeenCalledWith("game");
    expect(result.current.rows[0]?.state).toBe("available");
  });

  it("opens the new version right after updating it", async () => {
    const api = client([published("game", "Game", 2)]);
    const local = installer([installed("game", "Game", 1)]);
    local.install.mockImplementation(async () => {
      const copy = installed("game", "Game", 2);
      local.installed.mockResolvedValue([copy]);
      return copy;
    });
    const { result } = renderHook(() =>
      useApps(
        api as unknown as OpenCuriaClient,
        SCOPE,
        local as unknown as AppInstaller,
      ),
    );
    await waitFor(() => expect(result.current.rows[0]?.state).toBe("update"));

    // One tap: no render between the update and the open.
    await act(async () =>
      runPrimaryAction(result.current, result.current.rows[0]!),
    );

    expect(local.launchRequest).toHaveBeenCalledTimes(1);
    expect(local.launchRequest).toHaveBeenCalledWith(
      expect.objectContaining({ versionId: "game-v2" }),
    );
  });

  it("ignores a second tap while an install or open is running", async () => {
    const api = client([published("game", "Game")]);
    const local = installer();
    const { result } = renderHook(() =>
      useApps(
        api as unknown as OpenCuriaClient,
        SCOPE,
        local as unknown as AppInstaller,
      ),
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));

    await act(async () => {
      const results = await Promise.all([
        result.current.install("game"),
        result.current.install("game"),
      ]);
      expect(results).toEqual([true, false]);
    });
    await act(async () => {
      await Promise.all([result.current.open("game"), result.current.open("game")]);
    });

    expect(local.install).toHaveBeenCalledTimes(1);
    expect(local.launchRequest).toHaveBeenCalledTimes(1);
    // Once finished, the app can be opened again.
    await act(async () => result.current.open("game"));
    expect(local.launchRequest).toHaveBeenCalledTimes(2);
  });

  it("opens installed apps without a backend route when offline", async () => {
    const api = client([]);
    api.apps.gatewaySession.mockRejectedValueOnce(new Error("offline"));
    const local = installer([installed("game", "Game")]);
    const { result } = renderHook(() =>
      useApps(
        api as unknown as OpenCuriaClient,
        SCOPE,
        local as unknown as AppInstaller,
      ),
    );
    await waitFor(() => expect(result.current.rows).toHaveLength(1));

    await act(async () => result.current.open("game"));

    expect(mockOpenInstalledApp).toHaveBeenLastCalledWith({ appId: "game" });
  });

  it("reports a failed install and keeps the app available", async () => {
    const api = client([published("game", "Game")]);
    api.apps.manifest.mockRejectedValueOnce(new Error("offline"));
    const local = installer();
    const { result } = renderHook(() =>
      useApps(
        api as unknown as OpenCuriaClient,
        SCOPE,
        local as unknown as AppInstaller,
      ),
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));
    await act(async () => {
      expect(await result.current.install("game")).toBe(false);
    });
    expect(result.current.rows[0]?.state).toBe("available");
    expect(result.current.progress).toEqual({});
  });
});
