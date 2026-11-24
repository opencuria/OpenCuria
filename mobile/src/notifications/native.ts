import { AppState, Linking, Platform } from "react-native";
import { requireOptionalNativeModule } from "expo";
import * as Device from "expo-device";
import * as Application from "expo-application";
import * as Notifications from "expo-notifications";
import {
  notificationTarget,
  targetFromLink,
  type NotificationTarget,
} from "./target";

let viewedSession: string | null = null;
export function setViewedNotificationSession(id: string | null): void {
  viewedSession = id;
}

Notifications.setNotificationHandler({
  async handleNotification(notification) {
    const target = notificationTarget(notification.request.content.data);
    const visible =
      AppState.currentState === "active" && target?.sessionId === viewedSession;
    return {
      shouldShowBanner: !visible,
      shouldShowList: !visible,
      shouldPlaySound: !visible,
      shouldSetBadge: false,
    };
  },
});

/** RFC 8291 receiver key and auth secret, both unpadded base64url. */
export interface PushKeys {
  publicKey: string;
  authSecret: string;
}

/**
 * Keys live in the shared keychain (iOS: app group, read by the notification
 * service and Live Activity extensions) or the Android Keystore; the private
 * key never reaches JavaScript.
 */
interface OpenCuriaPushModule {
  getKeys(id: string): Promise<PushKeys>;
  deleteKeys(id: string): Promise<void>;
}

const pushKeys =
  requireOptionalNativeModule<OpenCuriaPushModule>("OpenCuriaPush");

export interface NativeNotifications {
  platform: "ios" | "android";
  appId: string;
  environment(): Promise<"sandbox" | "production">;
  available: boolean;
  isActive(): boolean;
  permission(request: boolean): Promise<boolean>;
  token(): Promise<string>;
  /** This installation's decryption keys for one account; created once. */
  keys(id: string): Promise<PushKeys>;
  deleteKeys(id: string): Promise<void>;
  reset(): Promise<void>;
  onToken(listener: (token: string) => void): () => void;
  onResponse(listener: (target: NotificationTarget) => void): () => void;
  onActivity(listener: () => void): () => void;
}

/** Native tokens only ever go to the push proxy, which returns opaque handles. */
export const nativeNotifications: NativeNotifications = {
  platform: Platform.OS === "ios" ? "ios" : "android",
  appId: Application.applicationId ?? "",
  async environment() {
    if (Platform.OS !== "ios") return "production";
    const environment =
      await Application.getIosPushNotificationServiceEnvironmentAsync();
    if (environment === "development") return "sandbox";
    if (environment === "production") return "production";
    // App Store builds do not contain an embedded provisioning profile.
    if (
      (await Application.getIosApplicationReleaseTypeAsync()) ===
      Application.ApplicationReleaseType.APP_STORE
    )
      return "production";
    throw new Error("Push notifications require a provisioned iOS build.");
  },
  available:
    Platform.OS === "android" || (Platform.OS === "ios" && Device.isDevice),
  isActive: () => AppState.currentState === "active",
  async permission(request) {
    if (Platform.OS === "android") {
      await Notifications.setNotificationChannelAsync("agent-activity", {
        name: "Agent activity",
        importance: Notifications.AndroidImportance.HIGH,
        description: "Agent completions, errors, approvals and questions",
      });
    }
    let status = await Notifications.getPermissionsAsync();
    if (request && !status.granted && status.canAskAgain) {
      status = await Notifications.requestPermissionsAsync({
        ios: { allowAlert: true, allowSound: true, allowBadge: false },
      });
    }
    return (
      status.granted ||
      status.ios?.status === Notifications.IosAuthorizationStatus.PROVISIONAL
    );
  },
  async token() {
    const value = await Notifications.getDevicePushTokenAsync();
    if (typeof value.data !== "string" || !value.data.trim())
      throw new Error("This device could not register for push notifications.");
    return value.data;
  },
  async keys(id) {
    if (!pushKeys)
      throw new Error(
        "This build cannot decrypt notifications. Update OpenCuria.",
      );
    return pushKeys.getKeys(id);
  },
  async deleteKeys(id) {
    await pushKeys?.deleteKeys(id);
  },
  async reset() {
    await Promise.allSettled([
      Notifications.dismissAllNotificationsAsync(),
      Notifications.unregisterForNotificationsAsync(),
    ]);
  },
  onToken(listener) {
    const subscription = Notifications.addPushTokenListener((token) => {
      if (typeof token.data === "string" && token.data) listener(token.data);
    });
    return () => subscription.remove();
  },
  onResponse(listener) {
    let active = true;
    let liveResponseReceived = false;
    const delivered = new Set<string>();
    const response = (
      value: Notifications.NotificationResponse | null,
      fromLiveEvent = false,
    ) => {
      if (
        !active ||
        !value ||
        value.actionIdentifier !== Notifications.DEFAULT_ACTION_IDENTIFIER
      )
        return;
      const target = notificationTarget(
        value.notification.request.content.data,
      );
      if (target) {
        if (fromLiveEvent) liveResponseReceived = true;
        const identifier = value.notification.request.identifier;
        if (identifier && delivered.has(identifier)) return;
        if (identifier) delivered.add(identifier);
        listener(target);
        void Notifications.clearLastNotificationResponseAsync().catch(
          () => undefined,
        );
      }
    };
    const link = (url: string, fromLiveEvent = false) => {
      const target = targetFromLink(url);
      if (active && target) {
        if (fromLiveEvent) liveResponseReceived = true;
        listener(target);
      }
    };
    const notifications = Notifications.addNotificationResponseReceivedListener(
      (value) => {
        response(value, true);
      },
    );
    const links = Linking.addEventListener("url", ({ url }) => {
      link(url, true);
    });
    void Notifications.getLastNotificationResponseAsync()
      .then((value) => {
        if (!liveResponseReceived) response(value);
      })
      .catch(() => undefined);
    void Linking.getInitialURL()
      .then((url) => {
        if (url && !liveResponseReceived) link(url);
      })
      .catch(() => undefined);
    return () => {
      active = false;
      notifications.remove();
      links.remove();
    };
  },
  onActivity(listener) {
    const foreground = AppState.addEventListener("change", (state) => {
      if (state === "active") listener();
    });
    const notifications =
      Notifications.addNotificationReceivedListener(listener);
    return () => {
      foreground.remove();
      notifications.remove();
    };
  },
};
