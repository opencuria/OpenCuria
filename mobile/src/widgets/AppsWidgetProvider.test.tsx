import React from "react";
import { Text } from "react-native";
import { act, render, screen, waitFor } from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import { appScope } from "../apps/appPresentation";
import {
  AppsWidgetProvider,
  useAppsWidgetRequests,
} from "./AppsWidgetProvider";

const mockOpenApp = jest.fn();
const mockCloseOpenApp = jest.fn();
const mockToast = jest.fn();
const mockSession = jest.fn();
jest.mock("../apps/useApps", () => ({
  appInstaller: () => ({ subscribe: () => () => undefined }),
  openApp: (...args: unknown[]) => mockOpenApp(...args),
}));
jest.mock("../apps/appRuntime", () => ({
  closeOpenApp: () => mockCloseOpenApp(),
}));
jest.mock("../screens/SessionContext", () => ({
  useSession: () => mockSession(),
}));
jest.mock("../ui/toast", () => ({
  showToast: (...args: unknown[]) => mockToast(...args),
}));
jest.mock("./appsWidget", () => ({
  appsWidget: {
    available: false,
    iconDirectory: async () => null,
    publish: async () => undefined,
  },
}));
// expo-linking keeps the latest link of the process until it is cleared.
let mockLinkingUrl: string | null = null;
let mockSendUrl: (url: string) => void = () => undefined;
jest.mock("expo-linking", () => ({
  getLinkingURL: () => mockLinkingUrl,
  clearInitialURL: () => {
    mockLinkingUrl = null;
  },
  addEventListener: (
    _type: string,
    listener: (event: { url: string }) => void,
  ) => {
    mockSendUrl = (url) => {
      mockLinkingUrl = url;
      listener({ url });
    };
    return { remove: jest.fn() };
  },
}));

const client = { api: { baseUrl: "https://curia.test" } } as OpenCuriaClient;
const session = {
  accessToken: "a",
  refreshToken: "r",
  serverUrl: "https://curia.test",
  organizationId: "org",
  userId: 7,
};
const SCOPE = appScope("https://curia.test", "org", 7);
const sendUrl = (url: string) => mockSendUrl(url);

function Requests() {
  const { appsRequested, consumeAppsRequest } = useAppsWidgetRequests();
  return (
    <Text testID="apps-requested" onPress={consumeAppsRequest}>
      {appsRequested ? "yes" : "no"}
    </Text>
  );
}

function renderProvider() {
  return render(
    <AppsWidgetProvider>
      <Requests />
    </AppsWidgetProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockLinkingUrl = null;
  mockOpenApp.mockResolvedValue(true);
  mockCloseOpenApp.mockResolvedValue(undefined);
  mockSession.mockReturnValue({ booting: false, session, client });
});

describe("AppsWidgetProvider", () => {
  it("opens the app a widget tile links to", async () => {
    renderProvider();
    act(() => sendUrl("opencuria:///?app=notes"));

    await waitFor(() =>
      expect(mockOpenApp).toHaveBeenCalledWith(client, SCOPE, "notes"),
    );
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("waits for the saved session before opening an app it was started for", async () => {
    mockLinkingUrl = "opencuria:///?app=notes";
    mockSession.mockReturnValue({ booting: true, session: null, client: null });
    const view = renderProvider();
    await act(async () => undefined);
    expect(mockOpenApp).not.toHaveBeenCalled();

    mockSession.mockReturnValue({ booting: true, session, client });
    view.rerender(
      <AppsWidgetProvider>
        <Requests />
      </AppsWidgetProvider>,
    );

    await waitFor(() =>
      expect(mockOpenApp).toHaveBeenCalledWith(client, SCOPE, "notes"),
    );
    expect(mockOpenApp).toHaveBeenCalledTimes(1);
  });

  it("explains when the app is no longer on this device", async () => {
    mockOpenApp.mockResolvedValue(false);
    renderProvider();
    act(() => sendUrl("opencuria:///?app=gone"));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "App not installed" }),
      ),
    );
  });

  it("asks for the Apps list and closes an app that would cover it", async () => {
    renderProvider();
    expect(screen.getByTestId("apps-requested")).toHaveTextContent("no");

    act(() => sendUrl("opencuria:///?apps"));

    expect(screen.getByTestId("apps-requested")).toHaveTextContent("yes");
    expect(mockCloseOpenApp).toHaveBeenCalledTimes(1);
    expect(mockOpenApp).not.toHaveBeenCalled();
  });

  it("ignores links that are not for apps and leaves them to others", async () => {
    renderProvider();
    act(() => sendUrl("opencuria:///?deviceId=x&sessionId=y"));
    await act(async () => undefined);

    expect(mockOpenApp).not.toHaveBeenCalled();
    expect(screen.getByTestId("apps-requested")).toHaveTextContent("no");
    expect(mockLinkingUrl).toBe("opencuria:///?deviceId=x&sessionId=y");
  });

  it("opens a link once, even when OpenCuria mounts again", async () => {
    mockLinkingUrl = "opencuria:///?app=notes";
    const first = renderProvider();
    await waitFor(() => expect(mockOpenApp).toHaveBeenCalledTimes(1));
    first.unmount();

    renderProvider();
    await act(async () => undefined);

    expect(mockOpenApp).toHaveBeenCalledTimes(1);
    expect(mockLinkingUrl).toBeNull();
  });
});
