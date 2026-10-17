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
import {
  NativeSessionStorage,
  type NativeAuthSession,
} from "../platform/storage";
import { normalizeServerUrl } from "./serverConfig";

const storage = new NativeSessionStorage();

type RegisterResource = (dispose: () => void) => () => void;
interface SessionContextValue {
  booting: boolean;
  session: NativeAuthSession | null;
  client: OpenCuriaClient | null;
  user: AuthUser | null;
  organizations: Organization[];
  error: string | null;
  sessionExpired: boolean;
  login: (serverUrl: string, email: string, password: string) => Promise<void>;
  retry: () => Promise<void>;
  signOut: () => Promise<void>;
  selectOrganization: (id: string) => Promise<void>;
  registerResource: RegisterResource;
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
  const sessionRef = useRef<NativeAuthSession | null>(null);
  const clientRef = useRef<OpenCuriaClient | null>(null);
  const generationRef = useRef(0);
  const lifecycleRef = useRef(0);
  const resourcesRef = useRef(new Set<() => void>());
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

  const expireAuthentication = useCallback(async () => {
    const generation = ++generationRef.current;
    disposeScope();
    setCurrentSession(null);
    setUser(null);
    setOrganizations([]);
    setError("Your session expired. Sign in again.");
    setSessionExpired(true);
    setBooting(false);
    try {
      await storage.clear();
    } catch (cause) {
      if (generation === generationRef.current)
        setError(messageOf(cause, "Could not clear the saved session."));
    }
  }, [disposeScope, setCurrentSession]);

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
            throw new Error(
              "Could not verify your saved session. Check the connection and retry.",
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
      const next = { ...current, organizationId: selected };
      setCurrentSession(next);
      setUser(profile);
      setOrganizations(orgs);
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
    [setCurrentSession],
  );

  const authenticateExisting = useCallback(
    async (authSession: NativeAuthSession, generation: number) => {
      setCurrentSession(authSession);
      const instance = makeClient(authSession, generation);
      try {
        await readProfile(instance, generation);
        if (generation === generationRef.current) {
          instance.realtime.connect();
          setError(null);
        }
      } catch (cause) {
        if (generation === generationRef.current) {
          // Only the shared client's confirmed rejected-refresh callback expires
          // local auth. Any profile/refresh transport error remains retryable.
          setError(messageOf(cause, "Could not reach this OpenCuria server."));
        }
      } finally {
        if (generation === generationRef.current) setBooting(false);
      }
    },
    [expireAuthentication, makeClient, readProfile, setCurrentSession],
  );

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
    [disposeScope, makeClient, readProfile, setCurrentSession],
  );

  const retry = useCallback(async () => {
    const current = sessionRef.current;
    const activeClient = clientRef.current;
    if (!current) {
      setError(null);
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
      if (generation === generationRef.current)
        setError(messageOf(cause, "Could not reach this OpenCuria server."));
    } finally {
      if (generation === generationRef.current) setBooting(false);
    }
  }, [authenticateExisting, expireAuthentication, readProfile]);

  const signOut = useCallback(async () => {
    ++generationRef.current;
    disposeScope();
    setSessionExpired(false);
    setCurrentSession(null);
    setUser(null);
    setOrganizations([]);
    setError(null);
    setBooting(false);
    try {
      await storage.clear();
    } catch (cause) {
      setError(messageOf(cause, "Could not clear the saved session."));
    }
  }, [disposeScope, setCurrentSession]);

  const selectOrganization = useCallback(
    async (id: string) => {
      const current = sessionRef.current;
      if (
        !current ||
        !organizations.some((organization) => organization.id === id) ||
        current.organizationId === id
      )
        return;
      const generation = ++generationRef.current;
      disposeScope();
      const next = { ...current, organizationId: id };
      setCurrentSession(next);
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
        if (generation === generationRef.current)
          setError(messageOf(cause, "Could not change organization."));
      } finally {
        if (generation === generationRef.current) setBooting(false);
      }
    },
    [disposeScope, makeClient, organizations, readProfile, setCurrentSession],
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
      login,
      retry,
      signOut,
      selectOrganization,
      registerResource,
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
      login,
      retry,
      signOut,
      selectOrganization,
      registerResource,
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
