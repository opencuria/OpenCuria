import * as SecureStore from "expo-secure-store";

const AUTH_SESSION_KEY = "opencuria.auth-session.v1";

/** Only authentication session metadata; never persist passwords or login forms. */
export interface NativeAuthSession {
  accessToken: string;
  refreshToken: string;
  serverUrl: string;
  organizationId: string | null;
}

export interface SecureStoreAdapter {
  getItemAsync(key: string): Promise<string | null>;
  setItemAsync(key: string, value: string): Promise<void>;
  deleteItemAsync(key: string): Promise<void>;
}

/**
 * Minimal serialized secure session storage. Logout invalidates older queued
 * saves and reads; all native SecureStore errors intentionally propagate.
 */
export class NativeSessionStorage {
  private queue: Promise<void> = Promise.resolve();
  private generation = 0;

  constructor(private readonly store: SecureStoreAdapter = SecureStore) {}

  async load(): Promise<NativeAuthSession | null> {
    const generation = this.generation;
    return this.enqueue(async () => {
      const raw = await this.store.getItemAsync(AUTH_SESSION_KEY);
      if (generation !== this.generation || raw === null) return null;
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        return null;
      }
      if (!isSession(parsed)) return null;
      return {
        accessToken: parsed.accessToken,
        refreshToken: parsed.refreshToken,
        serverUrl: parsed.serverUrl,
        organizationId: parsed.organizationId,
      };
    });
  }

  async save(session: NativeAuthSession): Promise<void> {
    const generation = this.generation;
    // Snapshot the caller's record before it enters the serialized queue.
    const serialized = JSON.stringify(validateSession(session));
    await this.enqueue(async () => {
      if (generation !== this.generation) return;
      await this.store.setItemAsync(AUTH_SESSION_KEY, serialized);
    });
  }

  async clear(): Promise<void> {
    // Increment synchronously so a queued write is skipped. A native write
    // already in flight is followed by deletion in this serialized queue.
    this.generation += 1;
    await this.enqueue(() => this.store.deleteItemAsync(AUTH_SESSION_KEY));
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.queue.then(operation, operation);
    this.queue = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}

function validateSession(value: NativeAuthSession): NativeAuthSession {
  if (
    !value ||
    typeof value.accessToken !== "string" ||
    !value.accessToken ||
    typeof value.refreshToken !== "string" ||
    !value.refreshToken ||
    typeof value.serverUrl !== "string" ||
    !value.serverUrl ||
    !(typeof value.organizationId === "string" || value.organizationId === null)
  ) {
    throw new TypeError("Invalid native auth session.");
  }
  return {
    accessToken: value.accessToken,
    refreshToken: value.refreshToken,
    serverUrl: value.serverUrl,
    organizationId: value.organizationId,
  };
}

function isSession(value: unknown): value is NativeAuthSession {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<NativeAuthSession>;
  return (
    typeof record.accessToken === "string" &&
    typeof record.refreshToken === "string" &&
    typeof record.serverUrl === "string" &&
    (typeof record.organizationId === "string" ||
      record.organizationId === null)
  );
}
