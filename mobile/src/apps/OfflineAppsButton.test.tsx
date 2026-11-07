import React from "react";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import type { InstalledApp } from "./appInstaller";
import { appRows, appScope } from "./appPresentation";
import { OfflineAppsButton } from "./OfflineAppsButton";
import type { AppsState } from "./useApps";

const mockUseApps = jest.fn();
jest.mock("./useApps", () => ({
  ...jest.requireActual("./useApps"),
  useApps: (...args: unknown[]) => mockUseApps(...args),
}));
jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

const SCOPE = appScope("https://curia.test", "org", 7);
const client = { api: { baseUrl: "https://curia.test" } } as OpenCuriaClient;
const session = {
  accessToken: "a",
  refreshToken: "r",
  serverUrl: "https://curia.test",
  organizationId: "org",
  userId: 7,
};

function copy(scope = SCOPE): InstalledApp {
  return {
    appId: "game",
    scope,
    name: "Game",
    slug: "game",
    versionId: "game-v1",
    versionNumber: 1,
    versionName: "1.0.0",
    sdkVersion: "57.0.0",
    publishedAt: "2026-10-01T00:00:00Z",
    installedAt: "2026-10-02T00:00:00Z",
    hasIcon: false,
  };
}

function apps(installed: InstalledApp[]): AppsState {
  return {
    rows: appRows([], installed, {
      scope: SCOPE,
      platform: "android",
      hostSdk: 57,
    }),
    loaded: false,
    loading: false,
    error: "Could not load apps.",
    progress: {},
    supported: true,
    offline: false,
    platform: "android",
    refresh: jest.fn(async () => undefined),
    install: jest.fn(async () => false),
    open: jest.fn(async () => undefined),
    uninstall: jest.fn(async () => undefined),
    remove: jest.fn(async () => undefined),
    icons: {},
  };
}

function renderButton(value = session) {
  return render(
    <ThemeProvider initialMode="light">
      <OfflineAppsButton client={client} session={value} />
    </ThemeProvider>,
  );
}

describe("OfflineAppsButton", () => {
  it("opens installed apps of the saved account while the server is offline", () => {
    const state = apps([copy()]);
    mockUseApps.mockReturnValue(state);
    renderButton();

    expect(mockUseApps).toHaveBeenCalledWith(client, SCOPE);
    fireEvent.press(screen.getByTestId("session-apps"));
    fireEvent.press(screen.getByText("Game"));

    expect(state.open).toHaveBeenCalledWith("game");
  });

  it("stays hidden without installed apps or a known account", () => {
    mockUseApps.mockReturnValue(apps([copy(appScope("x", "org", 8))]));
    renderButton();
    expect(screen.queryByTestId("session-apps")).toBeNull();

    mockUseApps.mockReturnValue(apps([copy()]));
    const { userId: _unknown, ...withoutAccount } = session;
    renderButton(withoutAccount);
    expect(screen.queryByTestId("session-apps")).toBeNull();
  });
});
