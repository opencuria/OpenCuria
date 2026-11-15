import type {
  AgentActivitySnapshot,
  OpenCuriaClient,
  PushCapabilities,
} from "@opencuria/client";
import type { NotificationPreferences } from "./storage";
import type { NativeNotifications } from "./native";
import type { LiveActivities, LiveActivityHandle } from "./liveActivity";
import type { NotificationTarget } from "./target";

export interface NotificationState {
  ready: boolean;
  busy: boolean;
  notifications: boolean;
  liveActivities: boolean;
  permissionGranted: boolean;
  capabilities: PushCapabilities | null;
  error: string | null;
  target: NotificationTarget | null;
}

export interface PreferenceStorage {
  load(): Promise<NotificationPreferences>;
  save(preferences: NotificationPreferences): Promise<void>;
}

/** Account-scoped, serialized lifecycle; stale listeners can never re-register after logout. */
export class NotificationController {
  private state: NotificationState = {
    ready: false,
    busy: false,
    notifications: false,
    liveActivities: false,
    permissionGranted: false,
    capabilities: null,
    error: null,
    target: null,
  };
  private listeners = new Set<() => void>();
  private preferences: NotificationPreferences | null = null;
  private cleanup: Array<() => void> = [];
  private activity: LiveActivityHandle | null = null;
  private stopToken: (() => void) | null = null;
  private activityToken = "";
  private deviceToken: string | null = null;
  private queue: Promise<unknown> = Promise.resolve();
  private disposed = false;
  private started = false;
  private revoking = false;

  constructor(
    private readonly client: OpenCuriaClient,
    private readonly organizationId: string,
    private readonly storage: PreferenceStorage,
    readonly native: NativeNotifications,
    readonly live: LiveActivities,
  ) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): NotificationState => this.state;

  start(): Promise<void> {
    if (this.started) return this.queue.then(() => undefined);
    this.started = true;
    return this.enqueue(async () => {
      this.preferences = await this.storage.load();
      if (this.disposed) return;
      this.patch({
        notifications: this.preferences.notifications,
        liveActivities: this.preferences.liveActivities,
      });
      this.cleanup.push(
        this.native.onResponse((target) => {
          if (
            !this.disposed &&
            target.deviceId === this.preferences?.deviceId &&
            target.organizationId === this.organizationId
          )
            this.patch({ target });
        }),
      );
      this.cleanup.push(
        this.native.onToken((token) => {
          if (
            this.disposed ||
            this.revoking ||
            !token ||
            token === this.deviceToken
          )
            return;
          const replacing = this.deviceToken !== null;
          this.deviceToken = token;
          // Native getDevicePushTokenAsync also emits this event. An unchanged
          // token must never trigger another read/registration in a loop.
          if (replacing) void this.refresh();
        }),
      );
      this.cleanup.push(
        this.native.onActivity(() => {
          void this.refresh();
        }),
      );
      for (const event of [
        "harness.session_status",
        "harness.permission_required",
        "harness.question_required",
        "harness.conversations_changed",
      ] as const) {
        this.cleanup.push(
          this.client.realtime.on(event, () => {
            void this.refresh();
          }),
        );
      }
      await this.synchronize(false);
      this.patch({ ready: true });
    });
  }

  refresh(): Promise<void> {
    if (!this.preferences && this.state.error) {
      this.patch({ error: null });
      this.started = false;
      return this.start();
    }
    return this.enqueue(() => this.synchronize(false));
  }

  setEnabled(
    kind: "notifications" | "liveActivities",
    enabled: boolean,
  ): Promise<void> {
    return this.enqueue(async () => {
      if (!this.preferences) return;
      const before = this.preferences;
      const next = { ...before, [kind]: enabled };
      // Persist consent before registering a destination. A failed secure-store
      // write must never leave an enabled server binding hidden from settings.
      await this.storage.save(next);
      this.preferences = next;
      try {
        await this.synchronize(
          kind === "notifications" && enabled,
          enabled ? kind : undefined,
        );
        this.patch({
          notifications: this.preferences.notifications,
          liveActivities: this.preferences.liveActivities,
        });
      } catch (cause) {
        this.preferences = before;
        await this.storage.save(before);
        try {
          await this.synchronize(false);
        } catch {
          /* Retry on foreground. */
        }
        throw cause;
      }
    });
  }

  consumeTarget(target?: NotificationTarget): void {
    if (target && this.state.target !== target) return;
    this.patch({ target: null });
  }

  async revoke(): Promise<void> {
    this.revoking = true;
    await this.queue;
    try {
      this.preferences ??= await this.storage.load();
      if (this.preferences) {
        // Revoke even if local consent is off: a response lost during an opt-in
        // or rollback must never leave a server registration behind on logout.
        await this.client.notifications.unregisterDevice(
          this.preferences.deviceId,
        );
        this.preferences = {
          ...this.preferences,
          notifications: false,
          liveActivities: false,
        };
        await this.storage.save(this.preferences);
      }
      await this.native.reset();
      await this.endActivities();
    } catch {
      this.revoking = false;
      throw new Error(
        "Could not revoke this device's notifications. Reconnect and try again.",
      );
    }
  }

  dispose(): void {
    this.disposed = true;
    for (const dispose of this.cleanup.splice(0)) dispose();
    this.stopToken?.();
    this.stopToken = null;
    this.listeners.clear();
    // Do not enumerate later: a new account may already have created its card.
    const activity = this.activity;
    this.activity = null;
    void activity?.end().catch(() => undefined);
  }

  private async synchronize(
    requestPermission: boolean,
    enabling?: "notifications" | "liveActivities",
  ): Promise<void> {
    const preferences = this.preferences;
    if (!preferences || this.disposed || this.revoking) return;
    const capabilities = await this.client.notifications.capabilities();
    if (this.disposed || this.revoking) return;
    this.patch({ capabilities });
    const permission = this.native.available
      ? await this.native.permission(requestPermission)
      : false;
    this.patch({ permissionGranted: permission });
    if (requestPermission && !permission)
      throw new Error(
        "Allow OpenCuria notifications in your device settings to receive agent updates.",
      );
    if (enabling === "notifications" && !capabilities[this.native.platform])
      throw new Error(
        "This server has not configured push notifications for this device.",
      );
    if (enabling === "liveActivities" && !capabilities.live_activities)
      throw new Error("This server has not configured Live Activities.");
    if (enabling === "liveActivities" && !this.live.available)
      throw new Error(
        "Live Activities are unavailable in this build or on this device.",
      );
    const notifications =
      preferences.notifications &&
      permission &&
      capabilities[this.native.platform];
    const liveEnabled =
      preferences.liveActivities &&
      this.live.available &&
      capabilities.live_activities;
    const token = notifications ? await this.native.token() : "";
    if (notifications) this.deviceToken = token;
    const environment =
      notifications || liveEnabled
        ? await this.native.environment()
        : "production";
    if (this.disposed || this.revoking) return;
    await this.client.notifications.registerDevice(preferences.deviceId, {
      platform: this.native.platform,
      app_id: this.native.appId,
      apns_environment: environment,
      push_token: token,
      notifications_enabled: notifications,
      live_activities_enabled: liveEnabled,
    });
    if (this.disposed || this.revoking) return;
    if (!liveEnabled) {
      await this.endActivities();
      return;
    }
    const snapshot = await this.client.notifications.activity(
      preferences.deviceId,
    );
    if (this.disposed || this.revoking) return;
    await this.reconcileActivity(snapshot);
  }

  private async reconcileActivity(
    snapshot: AgentActivitySnapshot,
  ): Promise<void> {
    const instances = await this.live.instances();
    if (this.disposed || this.revoking) return;
    const current =
      instances.find((item) => item.id === this.activity?.id) ??
      instances[0] ??
      null;
    for (const duplicate of instances.filter((item) => item.id !== current?.id))
      await duplicate.end();
    if (!snapshot.activeCount) {
      await this.endActivities();
      return;
    }
    const activity =
      current ??
      (this.native.isActive() ? await this.live.start(snapshot) : null);
    if (!activity) return;
    if (this.disposed || this.revoking) {
      await activity.end();
      return;
    }
    if (this.activity?.id !== activity.id) {
      this.stopToken?.();
      this.activity = activity;
      this.activityToken = "";
      this.stopToken = activity.onToken((token) => {
        void this.enqueue(async () =>
          this.registerActivityToken(activity.id, token),
        );
      });
    }
    await activity.update(snapshot);
    const token = await activity.token();
    if (token) await this.registerActivityToken(activity.id, token);
  }

  private async registerActivityToken(
    id: string,
    token: string,
  ): Promise<void> {
    if (
      this.disposed ||
      this.revoking ||
      !this.preferences?.liveActivities ||
      this.activity?.id !== id ||
      token === this.activityToken
    )
      return;
    await this.client.notifications.registerActivity(
      this.preferences.deviceId,
      id,
      token,
    );
    if (!this.disposed && this.activity?.id === id) this.activityToken = token;
  }

  private async endActivities(): Promise<void> {
    this.stopToken?.();
    this.stopToken = null;
    this.activity = null;
    this.activityToken = "";
    if (!this.live.available) return;
    for (const instance of await this.live.instances()) await instance.end();
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const next = this.queue
      .then(async () => {
        if (this.disposed || this.revoking) return;
        this.patch({ busy: true, error: null });
        try {
          await operation();
        } finally {
          this.patch({ busy: false, ready: true });
        }
      })
      .catch((error: unknown) => {
        if (!this.disposed)
          this.patch({
            error:
              error instanceof Error
                ? error.message
                : "Could not update notifications.",
          });
      });
    this.queue = next;
    return next;
  }

  private patch(update: Partial<NotificationState>): void {
    if (this.disposed) return;
    this.state = { ...this.state, ...update };
    for (const listener of this.listeners) listener();
  }
}
