import { AppState, Linking, Platform } from "react-native";
import * as Application from "expo-application";
import * as Notifications from "expo-notifications";
import { nativeNotifications, setViewedNotificationSession } from "./native";

jest.mock("expo-device", () => ({ isDevice: true }));
jest.mock("expo-application", () => ({
  applicationId: "com.opencuria.mobile.dev",
  getIosPushNotificationServiceEnvironmentAsync: jest.fn(),
  getIosApplicationReleaseTypeAsync: jest.fn(),
  ApplicationReleaseType: { APP_STORE: 5, DEVELOPMENT: 3, SIMULATOR: 1 },
}));
jest.mock("expo-notifications", () => ({
  setNotificationHandler: jest.fn(),
  setNotificationChannelAsync: jest.fn(),
  getPermissionsAsync: jest.fn(),
  requestPermissionsAsync: jest.fn(),
  getDevicePushTokenAsync: jest.fn(),
  dismissAllNotificationsAsync: jest.fn().mockResolvedValue(undefined),
  unregisterForNotificationsAsync: jest.fn().mockResolvedValue(undefined),
  addNotificationResponseReceivedListener: jest.fn(() => ({
    remove: jest.fn(),
  })),
  addNotificationReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  addPushTokenListener: jest.fn(() => ({ remove: jest.fn() })),
  getLastNotificationResponseAsync: jest.fn(),
  clearLastNotificationResponseAsync: jest.fn().mockResolvedValue(undefined),
  DEFAULT_ACTION_IDENTIFIER: "default",
  AndroidImportance: { HIGH: 4 },
  IosAuthorizationStatus: { PROVISIONAL: 3 },
}));

const handler = jest.mocked(Notifications.setNotificationHandler).mock
  .calls[0]![0]!;
const target = {
  deviceId: "11111111-1111-4111-8111-111111111111",
  organizationId: "22222222-2222-4222-8222-222222222222",
  workspaceId: "33333333-3333-4333-8333-333333333333",
  sessionId: "44444444-4444-4444-8444-444444444444",
};

beforeEach(() => {
  jest.clearAllMocks();
  setViewedNotificationSession(null);
  jest
    .mocked(Application.getIosPushNotificationServiceEnvironmentAsync)
    .mockResolvedValue("development");
  jest
    .mocked(Application.getIosApplicationReleaseTypeAsync)
    .mockResolvedValue(3);
  jest.spyOn(Linking, "getInitialURL").mockResolvedValue(null);
  jest
    .mocked(Notifications.getLastNotificationResponseAsync)
    .mockResolvedValue(null);
  Object.defineProperty(AppState, "currentState", {
    value: "active",
    writable: true,
    configurable: true,
  });
});
afterEach(() => jest.restoreAllMocks());

it("uses the native application identity and signing environment", async () => {
  expect(nativeNotifications.appId).toBe("com.opencuria.mobile.dev");
  expect(await nativeNotifications.environment()).toBe("sandbox");
  jest
    .mocked(Application.getIosPushNotificationServiceEnvironmentAsync)
    .mockResolvedValue("production");
  expect(await nativeNotifications.environment()).toBe("production");
  jest
    .mocked(Application.getIosPushNotificationServiceEnvironmentAsync)
    .mockResolvedValue(null);
  jest
    .mocked(Application.getIosApplicationReleaseTypeAsync)
    .mockResolvedValue(5);
  expect(await nativeNotifications.environment()).toBe("production");
  jest
    .mocked(Application.getIosApplicationReleaseTypeAsync)
    .mockResolvedValue(1);
  await expect(nativeNotifications.environment()).rejects.toThrow(
    /provisioned/,
  );
});

it("suppresses alerts only for the conversation visible in the foreground", async () => {
  const notification = { request: { content: { data: target } } } as never;
  setViewedNotificationSession(target.sessionId);
  expect(await handler.handleNotification(notification)).toMatchObject({
    shouldShowBanner: false,
    shouldPlaySound: false,
  });
  setViewedNotificationSession(null);
  expect(await handler.handleNotification(notification)).toMatchObject({
    shouldShowBanner: true,
  });
});

it("creates the Android channel with the system sound before checking permission", async () => {
  jest.replaceProperty(Platform, "OS", "android");
  jest.mocked(Notifications.getPermissionsAsync).mockResolvedValue({
    granted: true,
  } as never);
  expect(await nativeNotifications.permission(false)).toBe(true);
  expect(Notifications.setNotificationChannelAsync).toHaveBeenCalledWith(
    "agent-activity",
    {
      name: "Agent activity",
      importance: Notifications.AndroidImportance.HIGH,
      description: "Agent completions, errors, approvals and questions",
    },
  );
  expect(
    jest.mocked(Notifications.setNotificationChannelAsync).mock
      .invocationCallOrder[0],
  ).toBeLessThan(
    jest.mocked(Notifications.getPermissionsAsync).mock.invocationCallOrder[0]!,
  );
  expect(Notifications.requestPermissionsAsync).not.toHaveBeenCalled();
});

it("consumes a cold-start response once and clears the native saved response", async () => {
  jest
    .mocked(Notifications.getLastNotificationResponseAsync)
    .mockResolvedValue({
      actionIdentifier: "default",
      notification: { request: { content: { data: target } } },
    } as never);
  const listener = jest.fn();
  const dispose = nativeNotifications.onResponse(listener);
  await Promise.resolve();
  await Promise.resolve();
  expect(listener).toHaveBeenCalledWith(target);
  expect(
    Notifications.clearLastNotificationResponseAsync,
  ).toHaveBeenCalledTimes(1);
  dispose();
});

it("keeps a live tap when an older startup response resolves afterward", async () => {
  let resolveSaved!: (value: Notifications.NotificationResponse) => void;
  jest.mocked(Notifications.getLastNotificationResponseAsync).mockReturnValue(
    new Promise((resolve) => {
      resolveSaved = resolve;
    }),
  );
  let resolveLink!: (value: string | null) => void;
  jest.mocked(Linking.getInitialURL).mockReturnValue(
    new Promise((resolve) => {
      resolveLink = resolve;
    }),
  );
  const listener = jest.fn();
  const dispose = nativeNotifications.onResponse(listener);
  const live = jest.mocked(
    Notifications.addNotificationResponseReceivedListener,
  ).mock.calls[0]![0];
  const latest = {
    ...target,
    sessionId: "55555555-5555-4555-8555-555555555555",
  };
  live({
    actionIdentifier: "default",
    notification: {
      request: { identifier: "latest", content: { data: latest } },
    },
  } as never);
  resolveSaved({
    actionIdentifier: "default",
    notification: { request: { identifier: "old", content: { data: target } } },
  } as never);
  resolveLink(`opencuria:///?${new URLSearchParams(target).toString()}`);
  await Promise.resolve();
  await Promise.resolve();

  expect(listener).toHaveBeenCalledTimes(1);
  expect(listener).toHaveBeenLastCalledWith(latest);
  dispose();
});

it("does not replay a cold-start response when the live listener reports the same alert", async () => {
  const value = {
    actionIdentifier: "default",
    notification: {
      request: { identifier: "same", content: { data: target } },
    },
  } as never;
  jest
    .mocked(Notifications.getLastNotificationResponseAsync)
    .mockResolvedValue(value);
  const listener = jest.fn();
  const dispose = nativeNotifications.onResponse(listener);
  await Promise.resolve();
  const live = jest.mocked(
    Notifications.addNotificationResponseReceivedListener,
  ).mock.calls[0]![0];
  live(value);
  expect(listener).toHaveBeenCalledTimes(1);
  dispose();
});

it.each(["unrelated link", "nondefault action", "invalid payload"])(
  "keeps a saved notification tap after a live %s",
  async (event) => {
    let resolveSaved!: (value: Notifications.NotificationResponse) => void;
    jest.mocked(Notifications.getLastNotificationResponseAsync).mockReturnValue(
      new Promise((resolve) => {
        resolveSaved = resolve;
      }),
    );
    const linkSubscription = jest.spyOn(Linking, "addEventListener");
    const listener = jest.fn();
    const dispose = nativeNotifications.onResponse(listener);
    if (event === "unrelated link") {
      linkSubscription.mock.calls[0]![1]({ url: "opencuria://oauth/callback" });
    } else {
      const live = jest.mocked(
        Notifications.addNotificationResponseReceivedListener,
      ).mock.calls[0]![0];
      live({
        actionIdentifier: event === "nondefault action" ? "dismiss" : "default",
        notification: {
          request: {
            identifier: "ignored",
            content: { data: event === "invalid payload" ? {} : target },
          },
        },
      } as never);
    }
    expect(listener).not.toHaveBeenCalled();
    resolveSaved({
      actionIdentifier: "default",
      notification: {
        request: { identifier: "saved", content: { data: target } },
      },
    } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith(target);
    dispose();
  },
);

it("ignores callbacks after the notification scope is disposed", async () => {
  let resolveSaved!: (value: Notifications.NotificationResponse) => void;
  jest.mocked(Notifications.getLastNotificationResponseAsync).mockReturnValue(
    new Promise((resolve) => {
      resolveSaved = resolve;
    }),
  );
  const listener = jest.fn();
  const dispose = nativeNotifications.onResponse(listener);
  dispose();
  resolveSaved({
    actionIdentifier: "default",
    notification: { request: { identifier: "old", content: { data: target } } },
  } as never);
  await Promise.resolve();
  expect(listener).not.toHaveBeenCalled();
});

it("clears delivered alerts and invalidates the native token on sign-out", async () => {
  await nativeNotifications.reset();
  expect(Notifications.dismissAllNotificationsAsync).toHaveBeenCalledTimes(1);
  expect(Notifications.unregisterForNotificationsAsync).toHaveBeenCalledTimes(
    1,
  );
});
