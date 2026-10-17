import React from "react";
import { act, render, waitFor } from "@testing-library/react-native";
import { AppState } from "react-native";
import { createClient } from "@opencuria/client";
import type { CreateClientOptions } from "@opencuria/client";
import * as SecureStore from "expo-secure-store";
import { SessionProvider, useSession } from "./SessionContext";

jest.mock("@opencuria/client", () => ({ createClient: jest.fn() }));
jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn().mockResolvedValue(null),
  setItemAsync: jest.fn().mockResolvedValue(undefined),
  deleteItemAsync: jest.fn().mockResolvedValue(undefined),
}));

const mockCreateClient = createClient as jest.MockedFunction<
  typeof createClient
>;
const clients: Array<ReturnType<typeof makeClient>> = [];

const orgA = {
  id: "org-a",
  name: "A",
  slug: "a",
  role: "owner",
  created_at: "now",
};
const orgB = { ...orgA, id: "org-b", name: "B", slug: "b" };
const profile = {
  id: 7,
  email: "user@example.test",
  first_name: "User",
  last_name: "",
  organizations: [orgA, orgB],
};

function makeClient(overrides: Record<string, unknown> = {}) {
  const client = {
    auth: {
      login: jest.fn().mockResolvedValue({
        access_token: "access",
        refresh_token: "refresh",
      }),
      me: jest.fn().mockResolvedValue(profile),
      ...(overrides.auth as object | undefined),
    },
    api: {
      refreshAccessToken: jest.fn().mockResolvedValue(false),
      ...(overrides.api as object | undefined),
    },
    realtime: {
      connect: jest.fn(),
      setActive: jest.fn(),
      dispose: jest.fn(),
      ...(overrides.realtime as object | undefined),
    },
  };
  return client as unknown as ReturnType<typeof createClient>;
}

function Harness({
  onValue,
}: {
  onValue: (value: ReturnType<typeof useSession>) => void;
}): React.JSX.Element {
  const value = useSession();
  React.useEffect(() => onValue(value), [value, onValue]);
  return <></>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("SessionProvider", () => {
  afterEach(() => jest.restoreAllMocks());

  beforeEach(() => {
    jest.clearAllMocks();
    jest.mocked(SecureStore.getItemAsync).mockResolvedValue(null);
    jest.mocked(SecureStore.setItemAsync).mockResolvedValue(undefined);
    jest.mocked(SecureStore.deleteItemAsync).mockResolvedValue(undefined);
    clients.length = 0;
    mockCreateClient.mockImplementation((options: CreateClientOptions) => {
      const instance = clients.shift();
      if (!instance) throw new Error("No mock client provided");
      (instance.auth.login as jest.Mock).mockImplementation(async () => {
        const tokens = { access_token: "access", refresh_token: "refresh" };
        await options.setTokens(tokens);
        return tokens;
      });
      (instance.api.refreshAccessToken as jest.Mock).mockImplementation(
        async () => {
          if ((instance.api as { expireOnRefresh?: boolean }).expireOnRefresh) {
            options.onAuthenticationExpired();
          }
          return false;
        },
      );
      return instance;
    });
  });

  it("logs in, persists tokens, selects the first organization, and disposes on org change/sign out", async () => {
    const client = makeClient();
    clients.push(client);
    const current = jest.fn();
    render(
      <SessionProvider>
        <Harness onValue={current} />
      </SessionProvider>,
    );
    await waitFor(() => expect(current.mock.lastCall?.[0].booting).toBe(false));

    await act(async () => {
      await current.mock.lastCall![0].login(
        "https://curia.example/api/v1/",
        "user@example.test",
        "secret",
      );
    });
    expect(client.auth.login).toHaveBeenCalledWith(
      "user@example.test",
      "secret",
    );
    expect(client.auth.me).toHaveBeenCalledTimes(1);
    expect(SecureStore.setItemAsync).toHaveBeenCalledWith(
      "opencuria.auth-session.v1",
      expect.stringContaining('\"accessToken\":\"access\"'),
    );
    expect(current.mock.lastCall?.[0].user.email).toBe("user@example.test");
    expect(client.realtime.connect).toHaveBeenCalled();

    const secondClient = makeClient();
    clients.push(secondClient);
    await act(async () => {
      await current.mock.lastCall![0].selectOrganization("org-b");
    });
    expect(client.realtime.dispose).toHaveBeenCalledTimes(1);
    expect(current.mock.lastCall?.[0].session.organizationId).toBe("org-b");
    expect(secondClient.realtime.connect).toHaveBeenCalled();

    await act(async () => {
      await current.mock.lastCall![0].signOut();
    });
    expect(secondClient.realtime.dispose).toHaveBeenCalledTimes(1);
    expect(current.mock.lastCall?.[0].session).toBeNull();
    expect(SecureStore.deleteItemAsync).toHaveBeenCalledWith(
      "opencuria.auth-session.v1",
    );
  });

  it("preserves a saved session when profile returns 401 but refresh is unavailable offline", async () => {
    const client = makeClient({
      auth: {
        me: jest
          .fn()
          .mockRejectedValue({ status: 401, message: "Unauthorized" }),
      },
      api: { refreshAccessToken: jest.fn().mockResolvedValue(false) },
    });
    clients.push(client);
    jest.mocked(SecureStore.getItemAsync).mockResolvedValue(
      JSON.stringify({
        serverUrl: "https://curia.example",
        accessToken: "a",
        refreshToken: "r",
        organizationId: "org-a",
      }),
    );
    const current = jest.fn();
    render(
      <SessionProvider>
        <Harness onValue={current} />
      </SessionProvider>,
    );
    await waitFor(() => expect(current.mock.lastCall?.[0].booting).toBe(false));
    expect(current.mock.lastCall?.[0].session?.refreshToken).toBe("r");
    expect(current.mock.lastCall?.[0].sessionExpired).toBe(false);
    expect(current.mock.lastCall?.[0].error).toMatch(/retry/i);
    expect(SecureStore.deleteItemAsync).not.toHaveBeenCalled();
  });

  it("preserves a saved session when offline profile restore fails and retries without clearing it", async () => {
    jest.mocked(SecureStore.getItemAsync).mockResolvedValue(
      JSON.stringify({
        serverUrl: "https://curia.example",
        accessToken: "a",
        refreshToken: "r",
        organizationId: "org-a",
      }),
    );
    const client = makeClient({
      auth: {
        me: jest
          .fn()
          .mockRejectedValueOnce(new TypeError("Network request failed"))
          .mockResolvedValue(profile),
      },
    });
    clients.push(client);
    const current = jest.fn();
    render(
      <SessionProvider>
        <Harness onValue={current} />
      </SessionProvider>,
    );
    await waitFor(() => expect(current.mock.lastCall?.[0].booting).toBe(false));
    expect(current.mock.lastCall?.[0].session?.refreshToken).toBe("r");
    expect(current.mock.lastCall?.[0].error).toMatch(/Network request failed/);
    expect(SecureStore.deleteItemAsync).not.toHaveBeenCalled();

    await act(async () => {
      await current.mock.lastCall![0].retry();
    });
    expect(current.mock.lastCall?.[0].user.email).toBe("user@example.test");
    expect(client.realtime.connect).toHaveBeenCalled();
    expect(SecureStore.deleteItemAsync).not.toHaveBeenCalled();
  });

  it("ignores a stale profile response after sign out and disposes the client on unmount", async () => {
    jest.mocked(SecureStore.getItemAsync).mockResolvedValue(
      JSON.stringify({
        serverUrl: "https://curia.example",
        accessToken: "a",
        refreshToken: "r",
        organizationId: "org-a",
      }),
    );
    const me = deferred<typeof profile>();
    const client = makeClient({ auth: { me: jest.fn(() => me.promise) } });
    clients.push(client);
    const current = jest.fn();
    const screen = render(
      <SessionProvider>
        <Harness onValue={current} />
      </SessionProvider>,
    );
    await waitFor(() => expect(client.auth.me).toHaveBeenCalled());
    await act(async () => {
      await current.mock.lastCall![0].signOut();
    });
    await act(async () => {
      me.resolve(profile);
      await me.promise;
    });
    expect(current.mock.lastCall?.[0].session).toBeNull();
    screen.unmount();
    expect(client.realtime.dispose).toHaveBeenCalled();
  });

  it("returns to login when an explicit refresh rejection expires authentication", async () => {
    const client = makeClient({
      auth: {
        me: jest
          .fn()
          .mockRejectedValue({ status: 401, message: "Unauthorized" }),
      },
      api: { expireOnRefresh: true },
    });
    clients.push(client);
    jest.mocked(SecureStore.getItemAsync).mockResolvedValue(
      JSON.stringify({
        serverUrl: "https://curia.example",
        accessToken: "a",
        refreshToken: "r",
        organizationId: "org-a",
      }),
    );
    const current = jest.fn();
    render(
      <SessionProvider>
        <Harness onValue={current} />
      </SessionProvider>,
    );
    await waitFor(() => expect(current.mock.lastCall?.[0].booting).toBe(false));
    expect(current.mock.lastCall?.[0].sessionExpired).toBe(true);
    expect(current.mock.lastCall?.[0].session).toBeNull();
    expect(SecureStore.deleteItemAsync).toHaveBeenCalledWith(
      "opencuria.auth-session.v1",
    );
  });

  it("reactivates realtime and refreshes registered foreground work on resume", async () => {
    const client = makeClient();
    clients.push(client);
    const listeners: Array<(state: string) => void> = [];
    jest
      .spyOn(AppState, "addEventListener")
      .mockImplementation((_event, callback) => {
        listeners.push(callback as (state: string) => void);
        return { remove: jest.fn() } as never;
      });
    const current = jest.fn();
    render(
      <SessionProvider>
        <Harness onValue={current} />
      </SessionProvider>,
    );
    await waitFor(() => expect(current.mock.lastCall?.[0].booting).toBe(false));
    await act(async () => {
      await current.mock.lastCall![0].login(
        "https://curia.example",
        "user@example.test",
        "secret",
      );
    });
    const refresh = jest.fn().mockResolvedValue(undefined);
    current.mock.lastCall![0].registerForegroundRefresh(refresh);
    await act(async () => {
      listeners[0]?.("active");
    });
    expect(client.realtime.setActive).toHaveBeenCalledWith(true);
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });
});
