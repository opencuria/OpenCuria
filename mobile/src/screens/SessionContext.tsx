import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { AppState, type AppStateStatus } from "react-native";
import { createClient, type OpenCuriaClient } from "@opencuria/client";
import type { AuthUser, Organization, TokenPair } from "@opencuria/client";
import { appScope } from "../apps/appPresentation";
import { isUnreachable, watchNetwork } from "../offline/connectivity";
import { offlineStore } from "../offline/offlineStore";
import {
  NativeSessionStorage,
  type NativeAuthSession,
} from "../platform/storage";
import { normalizeServerUrl } from "./serverConfig";

const storage = new NativeSessionStorage();

/** A slow server does not keep saved chats away for longer than this. */
export const OFFLINE_AFTER_MS = 5_000;
/** Offline, reconnecting is tried after these delays (the last one repeats). */
export const RECONNECT_DELAYS_MS = [5_000, 10_000, 20_000, 30_000, 60_000];
/** A dropped live connection gets this long to come back before a check. */
export const DISCONNECT_GRACE_MS = 5_000;

/** The account whose data is saved for offline use; null until it is known. */
function savedScope(session: NativeAuthSession): string | null {
  if (session.userId === undefined) return null;
  try {
    return appScope(
      normalizeServerUrl(session.serverUrl).apiBaseUrl,
      session.organizationId,
      session.userId,
    );
  } catch {
    return null;
  }
}

type RegisterResource = (dispose: () => void) => () => void;
interface SessionContextValue {
  booting: boolean;
  session: NativeAuthSession | null;
  client: OpenCuriaClient | null;
  user: AuthUser | null;
  organizations: Organization[];
  error: string | null;
  sessionExpired: boolean;
  /**
   * The server cannot be reached: the profile, chats and lists saved on this
   * device show instead, and OpenCuria reconnects by itself.
   */
  offline: boolean;
  /** A reconnect attempt is running while offline. */
  reconnecting: boolean;
  login: (serverUrl: string, email: string, password: string) => Promise<void>;
  retry: () => Promise<void>;
  signOut: () => Promise<void>;
  selectOrganization: (id: string) => Promise<void>;
  /** Creates an organization and switches to it. */
  createOrganization: (name: string) => Promise<void>;
  registerResource: RegisterResource;
  registerSignOutCleanup: (cleanup: () => Promise<void>) => () => void;
  registerForegroundRefresh: (
    refresh: () => void | Promise<void>,
  ) => () => void;
  clearError: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const [booting, setBooting] = useState(true);
  const [session, setSession] = useState<NativeAuthSession | null>(null);
  const [client, setClient] = useState<OpenCuriaClient | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [offline, setOfflineState] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const offlineRef = useRef(false);
  const userRef = useRef<AuthUser | null>(null);
  userRef.current = user;
  /** Counts profiles that came from the server rather than from this device. */
  const freshProfilesRef = useRef(0);
  const probeRef = useRef<Promise<void> | null>(null);
  const reconnectRef = useRef<() => Promise<void>>(async () => undefined);
  const sessionRef = useRef<NativeAuthSession | null>(null);
  const clientRef = useRef<OpenCuriaClient | null>(null);
  const generationRef = useRef(0);
  const lifecycleRef = useRef(0);
  const resourcesRef = useRef(new Set<() => void>());
  const signOutCleanupsRef = useRef(new Set<() => Promise<void>>());
  const foregroundRefreshersRef = useRef(new Set<() => void | Promise<void>>());
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());

  const disposeScope = useCallback((updateClientState = true) => {
    foregroundRefreshersRef.current.clear();
    for (const dispose of resourcesRef.current) {
      try {
        dispose();
      } catch {
        /* Teardown must continue for every resource. */
      }
    }
    resourcesRef.current.clear();
    signOutCleanupsRef.current.clear();
    clientRef.current?.realtime.dispose();
    clientRef.current = null;
    if (updateClientState) setClient(null);
  }, []);

  const registerResource = useCallback<RegisterResource>((dispose) => {
    if (!clientRef.current) {
      dispose();
      return () => undefined;
    }
    resourcesRef.current.add(dispose);
    return () => resourcesRef.current.delete(dispose);
  }, []);

  const registerSignOutCleanup = useCallback((cleanup: () => Promise<void>) => {
    signOutCleanupsRef.current.add(cleanup);
    return () => {
      signOutCleanupsRef.current.delete(cleanup);
    };
  }, []);

  const revokeDevices = useCallback(async (): Promise<boolean> => {
    try {
      for (const cleanup of [...signOutCleanupsRef.current]) await cleanup();
      return true;
    } catch (cause) {
      setError(
        messageOf(cause, "Could not revoke this device's notifications."),
      );
      return false;
    }
  }, []);

  const registerForegroundRefresh = useCallback(
    (refresh: () => void | Promise<void>) => {
      foregroundRefreshersRef.current.add(refresh);
      return () => foregroundRefreshersRef.current.delete(refresh);
    },
    [],
  );

  const setCurrentSession = useCallback((next: NativeAuthSession | null) => {
    sessionRef.current = next;
    setSession(next);
  }, []);

  const setOffline = useCallback((value: boolean) => {
    offlineRef.current = value;
    setOfflineState(value);
  }, []);

  const expireAuthentication = useCallback(async () => {
    const generation = ++generationRef.current;
    disposeScope();
    setCurrentSession(null);
    setUser(null);
    setOrganizations([]);
    setOffline(false);
    setError("Your session expired. Sign in again.");
    setSessionExpired(true);
    setBooting(false);
    // Saved chats need a valid sign-in, like the server's.
    void offlineStore()
      .clear()
      .catch(() => undefined);
    try {
      await storage.clear();
    } catch (cause) {
      if (generation === generationRef.current)
        setError(messageOf(cause, "Could not clear the saved session."));
    }
  }, [disposeScope, setCurrentSession, setOffline]);

  useEffect(() => {
    lifecycleRef.current += 1;
    return () => {
      const lifecycle = ++lifecycleRef.current;
      queueMicrotask(() => {
        if (lifecycleRef.current !== lifecycle) return;
        ++generationRef.current;
        disposeScope(false);
      });
    };
  }, [disposeScope]);

  const makeClient = useCallback(
    (authSession: NativeAuthSession, generation: number): OpenCuriaClient => {
      const config = normalizeServerUrl(authSession.serverUrl);
      const instance = createClient({
        apiBaseUrl: config.apiBaseUrl,
        wsBaseUrl: config.wsBaseUrl,
        getAccessToken: () => {
          const current = sessionRef.current;
          return generation === generationRef.current &&
            current?.serverUrl === authSession.serverUrl
            ? current.accessToken
            : null;
        },
        getRefreshToken: () => {
          const current = sessionRef.current;
          return generation === generationRef.current &&
            current?.serverUrl === authSession.serverUrl
            ? current.refreshToken
            : null;
        },
        getOrganizationId: () => {
          const current = sessionRef.current;
          return generation === generationRef.current &&
            current?.serverUrl === authSession.serverUrl
            ? current.organizationId
            : null;
        },
        setTokens: async (tokens: TokenPair) => {
          if (generation !== generationRef.current) return;
          const current = sessionRef.current;
          if (!current || current.serverUrl !== authSession.serverUrl) return;
          const updated = {
            ...current,
            accessToken: tokens.access_token,
            refreshToken: tokens.refresh_token,
          };
          setCurrentSession(updated);
          const snapshot = generation;
          const write = saveQueueRef.current.then(async () => {
            if (
              snapshot === generationRef.current &&
              sessionRef.current === updated
            )
              await storage.save(updated);
          });
          saveQueueRef.current = write.catch(() => undefined);
          await write;
        },
        onAuthenticationExpired: () => {
          if (generation === generationRef.current) {
            void expireAuthentication();
          }
        },
      });
      clientRef.current = instance;
      setClient(instance);
      return instance;
    },
    [disposeScope, expireAuthentication, setCurrentSession],
  );

  const readProfile = useCallback(
    async (instance: OpenCuriaClient, generation: number) => {
      let profile: AuthUser;
      try {
        profile = await instance.auth.me();
      } catch (firstError) {
        if (!isUnauthorized(firstError)) throw firstError;
        const refreshed = await instance.api.refreshAccessToken();
        if (generation !== generationRef.current) return;
        if (!refreshed) {
          // A false result may mean the refresh endpoint is temporarily offline.
          // Only the client's confirmed 401/403 callback may expire local auth.
          throw new Error(
            "Could not verify your saved session. Check the connection and retry.",
          );
        }
        try {
          profile = await instance.auth.me();
        } catch (retryError) {
          if (isUnauthorized(retryError)) {
            // The server answered: this is no reason to work offline.
            throw Object.assign(
              new Error(
                "Could not verify your saved session. Check the connection and retry.",
              ),
              { status: 401 },
            );
          }
          throw retryError;
        }
      }
      if (generation !== generationRef.current) return;
      const orgs = Array.isArray(profile.organizations)
        ? profile.organizations
        : [];
      const current = sessionRef.current;
      if (!current) return;
      const selected =
        orgs.length === 0
          ? null
          : orgs.some((org) => org.id === current.organizationId)
            ? current.organizationId
            : (orgs[0]?.id ?? null);
      const next = { ...current, organizationId: selected, userId: profile.id };
      setCurrentSession(next);
      freshProfilesRef.current += 1;
      setUser(profile);
      setOrganizations(orgs);
      setOffline(false);
      const scope = savedScope(next);
      if (scope)
        void offlineStore()
          .account(scope)
          .saveProfile(profile, orgs)
          .catch(() => undefined);
      if (next !== current) {
        const write = saveQueueRef.current.then(async () => {
          if (
            generation === generationRef.current &&
            sessionRef.current === next
          )
            await storage.save(next);
        });
        saveQueueRef.current = write.catch(() => undefined);
        await write;
      }
    },
    [setCurrentSession, setOffline],
  );

  /**
   * Show what this device saved for the session's account while the server
   * cannot be reached. False when nothing is saved (or the server answered
   * meanwhile); the caller then reports the failure.
   */
  const openOffline = useCallback(
    async (authSession: NativeAuthSession, generation: number) => {
      const scope = savedScope(sessionRef.current ?? authSession);
      if (!scope) return false;
      const fresh = freshProfilesRef.current;
      const saved = await offlineStore()
        .account(scope)
        .loadProfile()
        .catch(() => null);
      if (
        !saved ||
        generation !== generationRef.current ||
        freshProfilesRef.current !== fresh
      )
        return false;
      setUser(saved.user);
      setOrganizations(saved.organizations);
      setError(null);
      setOffline(true);
      return true;
    },
    [setOffline],
  );

  const authenticateExisting = useCallback(
    async (authSession: NativeAuthSession, generation: number) => {
      setCurrentSession(authSession);
      const instance = makeClient(authSession, generation);
      // Saved chats show while a slow server answers; it takes over then.
      const fallback = setTimeout(() => {
        void openOffline(authSession, generation).then((opened) => {
          if (opened && generation === generationRef.current) setBooting(false);
        });
      }, OFFLINE_AFTER_MS);
      try {
        await readProfile(instance, generation);
        if (generation === generationRef.current) {
          instance.realtime.connect();
          setError(null);
        }
      } catch (cause) {
        if (generation === generationRef.current && !offlineRef.current) {
          // Only the shared client's confirmed rejected-refresh callback expires
          // local auth. Any profile/refresh transport error remains retryable.
          if (
            !isUnreachable(cause) ||
            !(await openOffline(authSession, generation))
          )
            setError(
              messageOf(cause, "Could not reach this OpenCuria server."),
            );
        }
      } finally {
        clearTimeout(fallback);
        if (generation === generationRef.current) setBooting(false);
      }
    },
    [makeClient, openOffline, readProfile, setCurrentSession],
  );

  /** Offline: check whether the server answers again and go back online. */
  const reconnect = useCallback(async () => {
    const instance = clientRef.current;
    if (!offlineRef.current || !instance) return;
    if (probeRef.current) return probeRef.current;
    const generation = generationRef.current;
    setReconnecting(true);
    const probe = (async () => {
      try {
        await readProfile(instance, generation);
        if (generation === generationRef.current) instance.realtime.connect();
      } catch {
        /* Still offline; the next attempt follows. */
      }
    })();
    probeRef.current = probe;
    try {
      await probe;
    } finally {
      if (probeRef.current === probe) probeRef.current = null;
      setReconnecting(false);
    }
  }, [readProfile]);
  reconnectRef.current = reconnect;

  useEffect(() => {
    let alive = true;
    const generation = ++generationRef.current;
    void storage
      .load()
      .then(async (saved) => {
        if (!alive || generation !== generationRef.current) return;
        if (!saved) {
          setBooting(false);
          return;
        }
        let normalized: NativeAuthSession;
        try {
          const config = normalizeServerUrl(saved.serverUrl);
          normalized = { ...saved, serverUrl: config.serverUrl };
        } catch {
          if (alive && generation === generationRef.current) {
            setError(
              "The saved server URL is invalid. Sign in with a valid server URL.",
            );
            setBooting(false);
          }
          return;
        }
        await authenticateExisting(normalized, generation);
      })
      .catch((cause: unknown) => {
        if (alive && generation === generationRef.current) {
          setError(messageOf(cause, "Could not read your secure session."));
          setBooting(false);
        }
      });
    return () => {
      alive = false;
    };
  }, [authenticateExisting]);

  useEffect(() => {
    const handleAppState = (next: AppStateStatus) => {
      const activeClient = clientRef.current;
      if (!activeClient) return;
      if (next === "active") {
        activeClient.realtime.setActive(true);
        if (offlineRef.current) {
          void reconnectRef.current();
          return;
        }
        for (const refresh of [...foregroundRefreshersRef.current]) {
          try {
            void Promise.resolve(refresh()).catch(() => undefined);
          } catch {
            /* Retry on next foreground. */
          }
        }
      } else if (next === "background" || next === "inactive") {
        activeClient.realtime.setActive(false);
      }
    };
    const subscription = AppState.addEventListener("change", handleAppState);
    return () => subscription.remove();
  }, []);

  // Offline: reconnect after a while, as soon as the network returns or the
  // live connection gets through.
  useEffect(() => {
    if (!offline || !client) return;
    let active = true;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      if (timer) clearTimeout(timer);
      const delay =
        RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
      timer = setTimeout(() => {
        attempt += 1;
        void reconnectRef.current().finally(() => {
          if (active && offlineRef.current) schedule();
        });
      }, delay);
    };
    schedule();
    const now = () => {
      attempt = 0;
      void reconnectRef.current();
      schedule();
    };
    const stopNetwork = watchNetwork((connected) => {
      if (connected) now();
    });
    const stopConnect = client.realtime.on("connect", now);
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
      stopNetwork();
      stopConnect();
    };
  }, [offline, client]);

  // Online: losing the network, or a live connection that stays down while
  // the server no longer answers, switches to what this device saved.
  useEffect(() => {
    if (offline || !client) return;
    const generation = generationRef.current;
    const goOffline = () => {
      if (
        generation === generationRef.current &&
        userRef.current &&
        !offlineRef.current
      )
        setOffline(true);
    };
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      if (
        AppState.currentState !== "active" ||
        client.realtime.isConnected ||
        !userRef.current
      )
        return;
      try {
        await client.auth.me();
      } catch (cause) {
        if (isUnreachable(cause) && !client.realtime.isConnected) goOffline();
      }
    };
    const stopNetwork = watchNetwork((connected) => {
      if (!connected) goOffline();
    });
    const detach = [
      client.realtime.on("disconnect", () => {
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => void check(), DISCONNECT_GRACE_MS);
      }),
      client.realtime.on("connect", () => {
        if (timer) clearTimeout(timer);
      }),
    ];
    return () => {
      if (timer) clearTimeout(timer);
      stopNetwork();
      detach.forEach((off) => off());
    };
  }, [offline, client, setOffline]);

  const login = useCallback(
    async (serverUrl: string, email: string, password: string) => {
      let config: ReturnType<typeof normalizeServerUrl>;
      try {
        config = normalizeServerUrl(serverUrl);
      } catch (cause) {
        setError(messageOf(cause, "Enter a valid server URL."));
        return;
      }
      const generation = ++generationRef.current;
      disposeScope();
      setSessionExpired(false);
      setUser(null);
      setOrganizations([]);
      setOffline(false);
      setError(null);
      setSessionExpired(false);
      setBooting(true);
      const initial: NativeAuthSession = {
        serverUrl: config.serverUrl,
        accessToken: "",
        refreshToken: "",
        organizationId: null,
      };
      setCurrentSession(initial);
      const instance = makeClient(initial, generation);
      setBooting(true);
      try {
        await instance.auth.login(email.trim(), password);
        if (generation !== generationRef.current) return;
        await readProfile(instance, generation);
        if (generation === generationRef.current) {
          instance.realtime.connect();
          setError(null);
        }
      } catch (cause) {
        if (generation === generationRef.current) {
          setError(
            messageOf(
              cause,
              "Sign in failed. Check the server address and your credentials.",
            ),
          );
        }
        // Credentials are cleared only if login itself did not establish a token.
        if (
          generation === generationRef.current &&
          !sessionRef.current?.accessToken
        ) {
          disposeScope();
          setCurrentSession(null);
        }
      } finally {
        if (generation === generationRef.current) setBooting(false);
      }
    },
    [disposeScope, makeClient, readProfile, setCurrentSession, setOffline],
  );

  const retry = useCallback(async () => {
    const current = sessionRef.current;
    const activeClient = clientRef.current;
    if (!current) {
      setError(null);
      return;
    }
    if (offlineRef.current) {
      await reconnect();
      return;
    }
    const generation = generationRef.current;
    setBooting(true);
    setError(null);
    try {
      if (!activeClient) {
        await authenticateExisting(current, generation);
        return;
      }
      try {
        await readProfile(activeClient, generation);
      } catch (cause) {
        if (generation === generationRef.current && isUnauthorized(cause)) {
          await expireAuthentication();
          return;
        }
        throw cause;
      }
      if (generation === generationRef.current) {
        activeClient.realtime.connect();
        setError(null);
      }
    } catch (cause) {
      if (
        generation === generationRef.current &&
        !(isUnreachable(cause) && (await openOffline(current, generation)))
      )
        setError(messageOf(cause, "Could not reach this OpenCuria server."));
    } finally {
      if (generation === generationRef.current) setBooting(false);
    }
  }, [
    authenticateExisting,
    expireAuthentication,
    openOffline,
    readProfile,
    reconnect,
  ]);

  const signOut = useCallback(async () => {
    if (!(await revokeDevices())) return;
    ++generationRef.current;
    disposeScope();
    setSessionExpired(false);
    setCurrentSession(null);
    setUser(null);
    setOrganizations([]);
    setOffline(false);
    setError(null);
    setBooting(false);
    // Chats saved for offline use leave the device with the sign-in.
    void offlineStore()
      .clear()
      .catch(() => undefined);
    try {
      await storage.clear();
    } catch (cause) {
      setError(messageOf(cause, "Could not clear the saved session."));
    }
  }, [disposeScope, revokeDevices, setCurrentSession, setOffline]);

  /** Switches the session to `id`; the fresh profile must list it. */
  const switchOrganization = useCallback(
    async (id: string) => {
      const current = sessionRef.current;
      if (!current || current.organizationId === id) return;
      if (!(await revokeDevices())) return;
      const generation = ++generationRef.current;
      disposeScope();
      const next = { ...current, organizationId: id };
      setCurrentSession(next);
      setOffline(false);
      setError(null);
      setBooting(true);
      try {
        const write = saveQueueRef.current.then(async () => {
          if (
            generation === generationRef.current &&
            sessionRef.current === next
          )
            await storage.save(next);
        });
        saveQueueRef.current = write.catch(() => undefined);
        await write;
        if (generation !== generationRef.current) return;
        const instance = makeClient(next, generation);
        await readProfile(instance, generation);
        if (generation === generationRef.current) instance.realtime.connect();
      } catch (cause) {
        if (
          generation === generationRef.current &&
          !(isUnreachable(cause) && (await openOffline(next, generation)))
        )
          setError(messageOf(cause, "Could not change organization."));
      } finally {
        if (generation === generationRef.current) setBooting(false);
      }
    },
    [
      disposeScope,
      makeClient,
      openOffline,
      readProfile,
      revokeDevices,
      setCurrentSession,
      setOffline,
    ],
  );

  const selectOrganization = useCallback(
    async (id: string) => {
      if (!organizations.some((organization) => organization.id === id)) return;
      await switchOrganization(id);
    },
    [organizations, switchOrganization],
  );

  const createOrganization = useCallback(
    async (name: string) => {
      const active = clientRef.current;
      if (!active) throw new Error("Sign in to create an organization.");
      const created = await active.organizations.create(name);
      await switchOrganization(created.id);
    },
    [switchOrganization],
  );

  const clearError = useCallback(() => setError(null), []);
  const value = useMemo<SessionContextValue>(
    () => ({
      booting,
      session,
      client,
      user,
      organizations,
      error,
      sessionExpired,
      offline,
      reconnecting,
      login,
      retry,
      signOut,
      selectOrganization,
      createOrganization,
      registerResource,
      registerSignOutCleanup,
      registerForegroundRefresh,
      clearError,
    }),
    [
      booting,
      session,
      client,
      user,
      organizations,
      error,
      sessionExpired,
      offline,
      reconnecting,
      login,
      retry,
      signOut,
      selectOrganization,
      createOrganization,
      registerResource,
      registerSignOutCleanup,
      registerForegroundRefresh,
      clearError,
    ],
  );
  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value)
    throw new Error("useSession must be used inside SessionProvider.");
  return value;
}

function isUnauthorized(error: unknown): boolean {
  return Boolean(
    error &&
    typeof error === "object" &&
    "status" in error &&
    (error as { status?: number }).status === 401,
  );
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
