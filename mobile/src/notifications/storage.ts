import * as SecureStore from "expo-secure-store";
import {
  digestStringAsync,
  CryptoDigestAlgorithm,
  randomUUID,
} from "expo-crypto";

export interface NotificationPreferences {
  deviceId: string;
  notifications: boolean;
  liveActivities: boolean;
}

/** Random, account-scoped installation ID and explicit consent; no chat content. */
export class NotificationStorage {
  constructor(private readonly scope: string) {}

  private async key(): Promise<string> {
    const digest = await digestStringAsync(
      CryptoDigestAlgorithm.SHA256,
      this.scope,
    );
    return `opencuria.notifications.${digest}`;
  }

  async load(): Promise<NotificationPreferences> {
    const key = await this.key();
    const raw = await SecureStore.getItemAsync(key);
    if (raw) {
      try {
        const value = JSON.parse(raw) as NotificationPreferences;
        if (
          typeof value.deviceId === "string" &&
          /^[0-9a-f-]{36}$/i.test(value.deviceId) &&
          typeof value.notifications === "boolean" &&
          typeof value.liveActivities === "boolean"
        )
          return value;
      } catch {
        /* A malformed record cannot opt the user in. */
      }
    }
    const value = {
      deviceId: randomUUID(),
      notifications: false,
      liveActivities: false,
    };
    await SecureStore.setItemAsync(key, JSON.stringify(value));
    return value;
  }

  async save(value: NotificationPreferences): Promise<void> {
    await SecureStore.setItemAsync(await this.key(), JSON.stringify(value));
  }
}
