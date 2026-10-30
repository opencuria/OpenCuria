import { describe, expect, it, vi } from "vitest";
import {
  ApiClient,
  ApiRequestError,
  createClient,
  FileTransferError,
  type RealtimeSocket,
  type SocketFactory,
} from "../src/index";
import type { FileFindResultEvent } from "../src/types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}

class MockSocket implements RealtimeSocket {
  connected = false;
  active?: boolean;
  handlers = new Map<string, Set<(...args: any[]) => void>>();
  emitted: Array<{ event: string; data: any }> = [];

  on(event: string, callback: (...args: any[]) => void): this {
    const callbacks = this.handlers.get(event) ?? new Set();
    callbacks.add(callback);
    this.handlers.set(event, callbacks);
    return this;
  }

  off(event: string, callback?: (...args: any[]) => void): this {
    if (!callback) this.handlers.delete(event);
    else this.handlers.get(event)?.delete(callback);
    return this;
  }

  emit(event: string, data?: any): this {
    this.emitted.push({ event, data });
    return this;
  }

  connect(): this {
    this.connected = true;
    this.trigger("connect");
    return this;
  }

  disconnect(): this {
    const wasConnected = this.connected;
    this.connected = false;
    if (wasConnected) this.trigger("disconnect", "io client disconnect");
    return this;
  }

  trigger(event: string, ...args: any[]): void {
    for (const callback of [...(this.handlers.get(event) ?? [])])
      callback(...args);
  }
}

function mockSocketFactory(
  sockets: MockSocket[],
  seenOptions: unknown[] = [],
): SocketFactory {
  return (uri, options) => {
    expect(uri).toBe("https://api.example/frontend");
    expect(options.path).toBe("/ws/runner");
    seenOptions.push(options);
    const socket = new MockSocket();
    sockets.push(socket);
    return socket;
  };
}

function clientOptions(overrides: Record<string, unknown> = {}) {
  let access = "old-access";
  let refresh = "old-refresh";
  let organization = "org-a";
  const setTokens = vi.fn(
    (tokens: { access_token: string; refresh_token: string }) => {
      access = tokens.access_token;
      refresh = tokens.refresh_token;
    },
  );
  const onAuthenticationExpired = vi.fn();
  const create = (
    fetch: typeof globalThis.fetch,
    extra: Record<string, unknown> = {},
  ) =>
    createClient({
      apiBaseUrl: "https://api.example/api/v1",
      wsBaseUrl: "https://api.example",
      getAccessToken: () => access,
      getRefreshToken: () => refresh,
      setTokens,
      getOrganizationId: () => organization,
      onAuthenticationExpired,
      fetch,
      ...extra,
      ...overrides,
    } as any);
  return {
    create,
    setOrganization: (value: string | null) => {
      organization = value ?? "";
    },
    access: () => access as string,
    refresh: () => refresh as string,
    setTokens,
    onAuthenticationExpired,
  };
}

describe("API authentication and errors", () => {
  it("refreshes only once for concurrent 401s and retries each request with fresh bearer token", async () => {
    const tokenReady = deferred<Response>();
    let refreshCalls = 0;
    const fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/auth/refresh/")) {
          refreshCalls += 1;
          return tokenReady.promise;
        }
        const headers = init?.headers as Record<string, string>;
        return headers.Authorization === "Bearer new-access"
          ? jsonResponse({ ok: true })
          : jsonResponse({ detail: "expired", code: "token_expired" }, 401);
      },
    ) as typeof globalThis.fetch;
    const setup = clientOptions();
    const client = setup.create(fetch);
    const one = client.api.get("/workspaces/");
    const two = client.api.get("/skills/");
    await Promise.resolve();
    await Promise.resolve();
    tokenReady.resolve(
      jsonResponse({
        access_token: "new-access",
        refresh_token: "new-refresh",
      }),
    );
    await expect(Promise.all([one, two])).resolves.toEqual([
      { ok: true },
      { ok: true },
    ]);
    expect(refreshCalls).toBe(1);
    expect(setup.setTokens).toHaveBeenCalledTimes(1);
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([url]) =>
          String(url).endsWith("/auth/refresh/"),
        )[0][1],
    ).toMatchObject({
      credentials: "omit",
      method: "POST",
      body: JSON.stringify({ refresh_token: "old-refresh" }),
    });
  });

  it("does not log out on refresh network or server failure, only authoritative rejection", async () => {
    const setup = clientOptions();
    let refreshStatus = 503;
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/auth/refresh/"))
        return jsonResponse(
          { detail: "temporarily unavailable" },
          refreshStatus,
        );
      return jsonResponse({ detail: "expired" }, 401);
    }) as typeof globalThis.fetch;
    const client = setup.create(fetch);
    await expect(client.api.get("/workspaces/")).rejects.toMatchObject({
      status: 401,
    });
    expect(setup.onAuthenticationExpired).not.toHaveBeenCalled();
    refreshStatus = 403;
    await expect(client.api.get("/workspaces/")).rejects.toMatchObject({
      status: 401,
    });
    expect(setup.onAuthenticationExpired).toHaveBeenCalledOnce();
  });

  it("surfaces explicit backend errors and preserves raw payload/status", async () => {
    const error = new ApiRequestError(409, "Conflict", "conflict", {
      detail: "Conflict",
      marker: 2,
    });
    expect(error).toMatchObject({
      name: "ApiRequestError",
      status: 409,
      code: "conflict",
      data: { marker: 2 },
    });
    const setup = clientOptions();
    const client = setup.create(
      vi.fn(async () =>
        jsonResponse({ detail: "conflict", code: "conflict" }, 409),
      ) as any,
    );
    await expect(client.api.get("/thing")).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 409,
      code: "conflict",
      message: "conflict",
      data: { code: "conflict" },
    });
  });

  it("does not refresh auth endpoints and expires auth once after refresh failure", async () => {
    let refreshCalls = 0;
    const refreshGate = deferred<Response>();
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/auth/refresh/")) {
        refreshCalls += 1;
        return refreshGate.promise;
      }
      return jsonResponse({ detail: "unauthorized" }, 401);
    }) as typeof globalThis.fetch;
    const setup = clientOptions();
    const client = setup.create(fetch);
    await expect(
      client.auth.login("bad@example.test", "bad"),
    ).rejects.toMatchObject({ status: 401 });
    expect(refreshCalls).toBe(0);
    const workspaceRequest = client.api.get("/workspaces/");
    const skillsRequest = client.api.get("/skills/");
    await Promise.resolve();
    await Promise.resolve();
    refreshGate.resolve(jsonResponse({ detail: "bad refresh" }, 401));
    await expect(
      Promise.all([workspaceRequest, skillsRequest]),
    ).rejects.toMatchObject({ status: 401 });
    expect(refreshCalls).toBe(1);
    expect(setup.onAuthenticationExpired).toHaveBeenCalledTimes(1);
  });

  it("does not let a stale successful refresh write tokens after the organization changes", async () => {
    const gate = deferred<Response>();
    const started = deferred<void>();
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/auth/refresh/")) {
        started.resolve();
        return gate.promise;
      }
      return jsonResponse({ detail: "expired" }, 401);
    }) as typeof globalThis.fetch;
    const setup = clientOptions();
    const client = setup.create(fetch);
    const request = client.api.get("/workspaces/");
    await started.promise;
    setup.setOrganization("org-new");
    gate.resolve(
      jsonResponse({
        access_token: "stale-access",
        refresh_token: "stale-refresh",
      }),
    );
    await expect(request).rejects.toBeInstanceOf(ApiRequestError);
    expect(setup.setTokens).not.toHaveBeenCalled();
    expect(setup.access()).toBe("old-access");
  });

  it("retries a late old-token 401 with the already-refreshed token without refreshing again", async () => {
    const delayed = deferred<Response>();
    let oldRequestCount = 0;
    let refreshCalls = 0;
    const fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/auth/refresh/")) {
          refreshCalls += 1;
          return jsonResponse({
            access_token: "new-access",
            refresh_token: "new-refresh",
          });
        }
        if (url.endsWith("/slow/")) {
          oldRequestCount += 1;
          return oldRequestCount === 1
            ? delayed.promise
            : jsonResponse({ ok: true });
        }
        return (init?.headers as Record<string, string>).Authorization ===
          "Bearer new-access"
          ? jsonResponse({ ok: true })
          : jsonResponse({ detail: "expired" }, 401);
      },
    ) as typeof globalThis.fetch;
    const setup = clientOptions();
    const client = setup.create(fetch);
    const late401 = client.api.get("/slow/");
    await Promise.resolve();
    await Promise.resolve();
    await expect(client.api.get("/refresh-trigger/")).resolves.toEqual({
      ok: true,
    });
    delayed.resolve(jsonResponse({ detail: "old token expired" }, 401));
    await expect(late401).resolves.toEqual({ ok: true });
    expect(refreshCalls).toBe(1);
    expect(setup.onAuthenticationExpired).not.toHaveBeenCalled();
  });

  it("does not let an old refresh overwrite a changed organization context", async () => {
    const gate = deferred<Response>();
    const refreshStarted = deferred<void>();
    let refreshCalls = 0;
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/auth/refresh/")) {
        refreshCalls += 1;
        refreshStarted.resolve();
        return gate.promise;
      }
      return jsonResponse({ detail: "expired" }, 401);
    }) as typeof globalThis.fetch;
    const setup = clientOptions();
    const client = setup.create(fetch);
    const request = client.api.get("/workspaces/");
    await refreshStarted.promise;
    setup.setOrganization("org-b");
    gate.resolve(
      jsonResponse({
        access_token: "wrong-org-token",
        refresh_token: "wrong-org-refresh",
      }),
    );
    await expect(request).rejects.toBeInstanceOf(ApiRequestError);
    expect(refreshCalls).toBe(1);
    expect(setup.setTokens).not.toHaveBeenCalled();
    expect(setup.onAuthenticationExpired).not.toHaveBeenCalled();
  });

  it("rejects absolute and protocol-relative paths before attaching auth", async () => {
    const fetch = vi.fn(async () =>
      jsonResponse({}),
    ) as typeof globalThis.fetch;
    const client = clientOptions().create(fetch);
    await expect(
      client.api.get("https://attacker.example/steal"),
    ).rejects.toMatchObject({ code: "invalid_path" });
    await expect(
      client.api.get("//attacker.example/steal"),
    ).rejects.toMatchObject({ code: "invalid_path" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("retries a 401 once and does not refresh again after a failed retry", async () => {
    let calls = 0;
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls += 1;
      if (String(input).endsWith("/auth/refresh/"))
        return jsonResponse({ detail: "invalid refresh" }, 401);
      return jsonResponse({ detail: "still expired" }, 401);
    }) as typeof globalThis.fetch;
    const setup = clientOptions();
    const client = setup.create(fetch);
    await expect(client.api.get("/workspaces/")).rejects.toMatchObject({
      status: 401,
    });
    expect(calls).toBe(2);
    expect(setup.onAuthenticationExpired).toHaveBeenCalledTimes(1);
  });
});

describe("REST endpoint wrappers", () => {
  it("keeps requestWithStatus non-throwing for endpoint 404 responses", async () => {
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({ status: "not found" }, 404)) as any,
    );
    await expect(
      client.api.requestWithStatus(
        "GET",
        "/provider-config/providers/chatgpt/oauth/status/",
      ),
    ).resolves.toEqual({
      ok: false,
      status: 404,
      data: { status: "not found" },
    });
  });

  it("uses authoritative routes and gate payloads", async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    const fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          url: String(input),
          method: init?.method ?? "GET",
          body: init?.body as string | undefined,
        });
        return jsonResponse([]);
      },
    ) as typeof globalThis.fetch;
    const client = clientOptions().create(fetch);
    await client.auth.me();
    await client.workspaces.list("runner x");
    await client.harness.listSessions("workspace-id");
    await client.harness.resolvePermission(
      "session-id",
      {
        request_id: "gate-id",
        session_id: "session-id",
        workspace_id: "workspace-id",
        tool: "shell",
        pattern: "",
        title: "",
      },
      "once",
    );
    await client.harness.resolveQuestion("session-id", "question-id", ["yes"]);
    await client.harness.timeline("session-id");
    await client.harness.steer("session-id", "Use pnpm");
    await client.harness.withdraw("session-id", "steer-id");
    expect(
      calls.map(({ url, method }) => [
        url.replace("https://api.example/api/v1", ""),
        method,
      ]),
    ).toEqual([
      ["/auth/me/", "GET"],
      ["/workspaces/?runner_id=runner%20x", "GET"],
      ["/workspaces/workspace-id/harness/sessions/", "GET"],
      ["/harness/sessions/session-id/permissions/gate-id", "POST"],
      ["/harness/sessions/session-id/questions/question-id", "POST"],
      ["/harness/sessions/session-id/timeline", "GET"],
      ["/harness/sessions/session-id/steer", "POST"],
      ["/harness/sessions/session-id/steer/steer-id", "DELETE"],
    ]);
    expect(JSON.parse(calls[3].body!)).toEqual({ response: "once" });
    expect(JSON.parse(calls[4].body!)).toEqual({
      answers: ["yes"],
      reject: false,
    });
    expect(JSON.parse(calls[6].body!)).toEqual({ prompt: "Use pnpm" });
  });
});

describe("shared harness actions", () => {
  it("keeps engine selection, inclusive forks and lazy part details on one client", async () => {
    const fetch = vi
      .fn()
      .mockImplementation(async () => jsonResponse({ id: "answer" }));
    const client = clientOptions().create(fetch);
    const signal = new AbortController().signal;
    const input = {
      prompt: "hello",
      harness_id: "codex" as const,
      model: "chatgpt/gpt-6.1-sol",
      reasoning_effort: "ultra",
    };
    await client.harness.createSession("workspace/id", input, signal);
    expect(fetch).toHaveBeenLastCalledWith(
      "https://api.example/api/v1/workspaces/workspace%2Fid/harness/sessions/",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
        credentials: "omit",
        signal,
      }),
    );
    await client.harness.fork("session/id", {
      message_id: "answer",
    });
    expect(fetch).toHaveBeenLastCalledWith(
      "https://api.example/api/v1/harness/sessions/session%2Fid/fork",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ message_id: "answer" }),
      }),
    );
    await expect(
      client.harness.getPart("session/id", "part/id"),
    ).resolves.toEqual({ id: "answer" });
    expect(fetch.mock.lastCall?.[0]).toBe(
      "https://api.example/api/v1/harness/sessions/session%2Fid/parts/part%2Fid",
    );
  });
});

describe("realtime and file transfers", () => {
  it("does not double bind listeners on repeated connect and reconnects after auth refusal", async () => {
    const sockets: MockSocket[] = [];
    let refreshCalls = 0;
    const fetch = vi.fn(async () =>
      jsonResponse({
        access_token: "new-access",
        refresh_token: "new-refresh",
      }),
    ) as typeof globalThis.fetch;
    const client = clientOptions().create(fetch, {
      socketFactory: mockSocketFactory(sockets),
      fetch,
    });
    let count = 0;
    let queuedSteer = "";
    client.realtime.on("harness.part_updated", () => {
      count += 1;
    });
    client.realtime.on("harness.steer_queue", (event) => {
      queuedSteer = event.steers[0]?.content ?? "";
    });
    client.realtime.connect();
    client.realtime.connect();
    expect(sockets).toHaveLength(1);
    const socket = sockets[0];
    socket.connect();
    socket.trigger("harness.part_updated", {});
    socket.trigger("harness.steer_queue", {
      workspace_id: "ws-1",
      session_id: "session-1",
      steers: [{ id: "steer-1", content: "Use pnpm", created_at: "now" }],
    });
    expect(count).toBe(1);
    expect(queuedSteer).toBe("Use pnpm");
    // Replace the instance's refresh implementation with a deterministic successful refresh.
    const realtime = client.realtime as any;
    realtime.options.refreshAccessToken = async () => {
      refreshCalls += 1;
      return true;
    };
    socket.active = false;
    socket.trigger("connect_error", new Error("Invalid or expired token"));
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    expect(socket.connected).toBe(true);
    expect(sockets).toHaveLength(1);
    socket.trigger("harness.part_updated", {});
    expect(count).toBe(2);
    client.realtime.dispose();
  });

  it("clears old workspace subscriptions on org/token context reset", () => {
    const sockets: MockSocket[] = [];
    let organization = "org-a";
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: (
          _uri: string,
          _options: Parameters<SocketFactory>[1],
        ) => {
          const socket = new MockSocket();
          sockets.push(socket);
          return socket;
        },
        getOrganizationId: () => organization,
      } as any,
    );
    client.realtime.connect();
    sockets[0].connect();
    client.realtime.subscribeWorkspace("private-workspace");
    organization = "org-b";
    client.realtime.connect();
    expect(sockets).toHaveLength(2);
    sockets[1].connect();
    expect(
      sockets[1].emitted.filter(
        ({ event }) => event === "frontend:subscribe_workspace",
      ),
    ).toHaveLength(0);
    client.realtime.dispose();
  });

  it("does not resume an old org workspace room when an old release handle fires", () => {
    const sockets: MockSocket[] = [];
    let organization = "org-a";
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: (
          _uri: string,
          _options: Parameters<SocketFactory>[1],
        ) => {
          const socket = new MockSocket();
          sockets.push(socket);
          return socket;
        },
        getOrganizationId: () => organization,
      } as any,
    );
    client.realtime.connect();
    sockets[0].connect();
    const releaseOldOrg =
      client.realtime.subscribeWorkspace("private-workspace");
    organization = "org-b";
    client.realtime.connect();
    sockets[1].connect();
    releaseOldOrg();
    expect(
      sockets[1].emitted.filter(
        ({ event }) => event === "frontend:subscribe_workspace",
      ),
    ).toHaveLength(0);
    client.realtime.dispose();
  });

  it("retains logical event and room subscriptions across ordinary reconnects", () => {
    const sockets: MockSocket[] = [];
    const seenOptions: any[] = [];
    const fetch = vi.fn(async () =>
      jsonResponse({}),
    ) as typeof globalThis.fetch;
    const client = clientOptions().create(fetch, {
      socketFactory: mockSocketFactory(sockets, seenOptions),
    });
    let eventCount = 0;
    let reconnectCount = 0;
    client.realtime.on("harness.part_updated", () => {
      eventCount += 1;
    });
    client.realtime.onReconnect(() => {
      reconnectCount += 1;
    });
    const release = client.realtime.subscribeWorkspace("ws-1");
    const releaseSecond = client.realtime.subscribeWorkspace("ws-1");
    client.realtime.connect();
    const socket = sockets[0];
    socket.connect();
    socket.trigger("harness.part_updated", { workspace_id: "ws-1" });
    socket.disconnect();
    socket.connect();
    socket.trigger("harness.part_updated", { workspace_id: "ws-1" });
    expect(eventCount).toBe(2);
    expect(reconnectCount).toBe(1);
    expect(
      socket.emitted.filter(
        (item) => item.event === "frontend:subscribe_workspace",
      ),
    ).toHaveLength(2);
    expect(client.realtime.isConnected).toBe(true);
    expect(client.realtime.status).toBe("connected");
    seenOptions[0].auth((data: Record<string, string>) =>
      expect(data.token).toBe("old-access"),
    );
    release();
    expect(socket.emitted.at(-1)?.event).toBe("frontend:subscribe_workspace");
    release();
    releaseSecond();
    expect(socket.emitted.at(-1)).toEqual({
      event: "frontend:unsubscribe_workspace",
      data: { workspace_id: "ws-1" },
    });
    client.realtime.dispose();
  });

  it("normalizes the actual runner find DTO object entries to trusted full workspace paths", async () => {
    const sockets: MockSocket[] = [];
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: mockSocketFactory(sockets),
        createFileRequestId: () => "find-dto",
      },
    );
    client.realtime.connect();
    const socket = sockets[0];
    socket.connect();
    const find = client.files.findFiles("ws-1", "hello.py");
    const request = socket.emitted.find(
      ({ event }) => event === "frontend:files_find",
    )!.data;
    const actualRunnerPayload: FileFindResultEvent = {
      workspace_id: "ws-1",
      request_id: request.request_id,
      query: "hello.py",
      paths: [
        { name: "hello.py", path: "/workspace/src/hello.py" },
        { name: "escape", path: "/tmp/private" },
        { name: "traversal", path: "/workspace/../../secret" },
        { name: "missing-path" } as any,
        "/workspace/legacy.py",
      ],
      truncated: false,
    };
    socket.trigger("files:find_result", actualRunnerPayload);
    await expect(find).resolves.toMatchObject({
      paths: ["/workspace/src/hello.py", "/workspace/legacy.py"],
    });
    client.realtime.dispose();
  });

  it("rejects mismatched workspace/path results and ignores late results", async () => {
    const sockets: MockSocket[] = [];
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: mockSocketFactory(sockets),
        createFileRequestId: () => "req-1",
      },
    );
    client.realtime.connect();
    const socket = sockets[0];
    socket.connect();
    const read = client.files.readFile("ws-1", "/workspace/a.txt");
    const requestId = socket.emitted.find(
      (item) => item.event === "frontend:files_read",
    )!.data.request_id;
    socket.trigger("files:content_result", {
      workspace_id: "ws-2",
      request_id: requestId,
      path: "/workspace/a.txt",
      content: "x",
      size: 1,
      truncated: false,
    });
    await expect(read).rejects.toBeInstanceOf(FileTransferError);
    socket.trigger("files:content_result", {
      workspace_id: "ws-1",
      request_id: requestId,
      path: "/workspace/a.txt",
      content: "late",
      size: 4,
      truncated: false,
    });
    client.realtime.dispose();
  });

  it("rejects wrong-path chunks and ignores late chunks after a failed transfer", async () => {
    const sockets: MockSocket[] = [];
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: mockSocketFactory(sockets),
        createFileRequestId: () => "path-read",
      },
    );
    client.realtime.connect();
    const socket = sockets[0];
    socket.connect();
    const read = client.files.readFile("ws-1", "/workspace/expected.txt");
    socket.trigger("files:content_chunk", {
      workspace_id: "ws-1",
      request_id: "path-read",
      path: "/workspace/wrong.txt",
      index: 0,
      total_chunks: 1,
      content: "YWJj",
    });
    await expect(read).rejects.toMatchObject({ code: "correlation_error" });
    socket.trigger("files:content_chunk", {
      workspace_id: "ws-1",
      request_id: "path-read",
      path: "/workspace/expected.txt",
      index: 0,
      total_chunks: 1,
      content: "YWJj",
    });
    socket.trigger("files:content_result", {
      workspace_id: "ws-1",
      request_id: "path-read",
      path: "/workspace/expected.txt",
      content: "",
      size: 0,
      truncated: false,
      chunked: true,
      total_chunks: 1,
    });
    client.realtime.dispose();
  });

  it.each([
    ["read", false],
    ["read", true],
    ["download", false],
    ["download", true],
  ] as const)(
    "cancels %s and releases its room (already aborted: %s)",
    async (kind, alreadyAborted) => {
      const sockets: MockSocket[] = [];
      const client = clientOptions().create(
        vi.fn(async () => jsonResponse({})) as any,
        {
          socketFactory: mockSocketFactory(sockets),
          createFileRequestId: () => "cancel-read",
        },
      );
      client.realtime.connect();
      const socket = sockets[0];
      socket.connect();
      const controller = new AbortController();
      if (alreadyAborted) controller.abort();
      const read =
        kind === "read"
          ? client.files.readFile(
              "ws-1",
              "/workspace/image.png",
              undefined,
              controller.signal,
            )
          : client.files.downloadFile(
              "ws-1",
              "/workspace/image.png",
              controller.signal,
            );
      if (!alreadyAborted) {
        socket.trigger(
          `files:${kind === "read" ? "content" : "download"}_chunk`,
          {
            workspace_id: "ws-1",
            request_id: "cancel-read",
            path: "/workspace/image.png",
            index: 0,
            total_chunks: 1,
            content: "YWJj",
          },
        );
        controller.abort();
        expect(socket.emitted.at(-1)?.event).toBe(
          "frontend:unsubscribe_workspace",
        );
      } else {
        expect(socket.emitted).toHaveLength(0);
      }
      await expect(read).rejects.toMatchObject({ code: "cancelled" });
      socket.trigger(
        `files:${kind === "read" ? "content" : "download"}_result`,
        {
          workspace_id: "ws-1",
          request_id: "cancel-read",
          path: "/workspace/image.png",
          content: "",
          size: 3,
          truncated: false,
          chunked: true,
          total_chunks: 1,
        },
      );
      client.realtime.dispose();
    },
  );

  it("reassembles chunked content with strict correlation", async () => {
    const sockets: MockSocket[] = [];
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: mockSocketFactory(sockets),
        createFileRequestId: () => "read-1",
      },
    );
    client.realtime.connect();
    const socket = sockets[0];
    socket.connect();
    const read = client.files.readFile("ws-1", "/workspace/a.txt");
    socket.trigger("files:content_chunk", {
      workspace_id: "ws-1",
      request_id: "read-1",
      path: "/workspace/a.txt",
      index: 1,
      total_chunks: 2,
      content: "ZGVm",
    });
    socket.trigger("files:content_chunk", {
      workspace_id: "ws-1",
      request_id: "read-1",
      path: "/workspace/a.txt",
      index: 0,
      total_chunks: 2,
      content: "YWJj",
    });
    socket.trigger("files:content_result", {
      workspace_id: "ws-1",
      request_id: "read-1",
      path: "/workspace/a.txt",
      content: "",
      size: 6,
      truncated: false,
      chunked: true,
      total_chunks: 2,
    });
    await expect(read).resolves.toMatchObject({ content: "YWJjZGVm", size: 6 });
    client.realtime.dispose();
  });

  describe.each([false, true])("file payloads (chunked: %s)", (chunked) => {
    it.each([
      ["read", true, 10, true],
      ["read", true, 3, false],
      ["read", false, 3, true],
      ["read", false, 4, false],
      ["download", false, 3, true],
      ["download", false, 4, false],
      ["download", false, 100 * 1024 * 1024 + 1, false],
      ["download", false, 3.5, false],
      ["download", false, -1, false],
    ] as const)(
      "%s truncated=%s size=%s accepted=%s",
      async (kind, truncated, size, accepted) => {
        const sockets: MockSocket[] = [];
        const client = clientOptions().create(
          vi.fn(async () => jsonResponse({})) as any,
          {
            socketFactory: mockSocketFactory(sockets),
            createFileRequestId: () => "payload",
          },
        );
        client.realtime.connect();
        const socket = sockets[0];
        socket.connect();
        const pending =
          kind === "read"
            ? client.files.readFile("ws-1", "/workspace/a.txt")
            : client.files.downloadFile("ws-1", "/workspace/a.txt");
        const identity = {
          workspace_id: "ws-1",
          request_id: "payload",
          path: "/workspace/a.txt",
        };
        const event = kind === "read" ? "content" : "download";
        if (chunked)
          socket.trigger(`files:${event}_chunk`, {
            ...identity,
            index: 0,
            total_chunks: 1,
            content: "YWJj",
          });
        socket.trigger(`files:${event}_result`, {
          ...identity,
          content: chunked ? "" : "YWJj",
          size,
          truncated,
          filename: "a.txt",
          is_archive: false,
          chunked,
          total_chunks: chunked ? 1 : undefined,
        });
        if (accepted)
          await expect(pending).resolves.toMatchObject({
            content: "YWJj",
            size,
          });
        else await expect(pending).rejects.toBeInstanceOf(FileTransferError);
        client.realtime.dispose();
      },
    );
  });

  it("fails pending transfer on disconnect, rejects disconnected uploads, and bounds upload size", async () => {
    const sockets: MockSocket[] = [];
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: mockSocketFactory(sockets),
        createFileRequestId: () => "file-1",
        fileTransferTimeoutMs: 100,
      },
    );
    client.realtime.connect();
    const socket = sockets[0];
    socket.connect();
    const pending = client.files.listFiles("ws-1", "/workspace");
    socket.disconnect();
    await expect(pending).rejects.toMatchObject({ code: "disconnected" });
    await expect(
      client.files.uploadFile("ws-1", {
        directory: "/workspace",
        filename: "a.txt",
        base64: "YQ==",
      }),
    ).rejects.toMatchObject({ code: "disconnected" });
    socket.connect();
    const oversized = "A".repeat(14 * 1024 * 1024 + 4);
    await expect(
      client.files.uploadFile("ws-1", {
        directory: "/workspace",
        filename: "big.bin",
        base64: oversized,
      }),
    ).rejects.toMatchObject({ code: "size_limit" });
    client.realtime.dispose();
  });

  it.each([0, 4, 256 * 1024 + 4])(
    "uploads %i base64 characters through start/chunks/finish",
    async (length) => {
      const sockets: MockSocket[] = [];
      const client = clientOptions().create(
        vi.fn(async () => jsonResponse({})) as any,
        {
          socketFactory: mockSocketFactory(sockets),
          createFileRequestId: () => "upload-1",
        },
      );
      client.realtime.connect();
      const socket = sockets[0];
      socket.connect();
      const content = "A".repeat(length);
      const upload = client.files.uploadFile("ws-1", {
        directory: "/workspace",
        filename: "a.bin",
        base64: content,
      });
      const events = socket.emitted.filter(({ event }) =>
        event.startsWith("frontend:files_upload"),
      );
      expect(events.map(({ event }) => event)).toEqual([
        "frontend:files_upload_start",
        ...Array(Math.ceil(length / (256 * 1024))).fill(
          "frontend:files_upload_chunk",
        ),
        "frontend:files_upload_finish",
      ]);
      expect(events[0].data.total_chunks).toBe(
        Math.ceil(length / (256 * 1024)),
      );
      socket.trigger("files:upload_result", {
        workspace_id: "ws-1",
        request_id: "upload-1",
        path: "/workspace",
        filename: "a.bin",
        status: "success",
      });
      await expect(upload).resolves.toMatchObject({
        status: "success",
        path: "/workspace/a.bin",
      });
      client.realtime.dispose();
    },
  );

  it("bounds pending transfer time and provides deterministic connection status", async () => {
    const sockets: MockSocket[] = [];
    const client = clientOptions().create(
      vi.fn(async () => jsonResponse({})) as any,
      {
        socketFactory: mockSocketFactory(sockets),
        fileTransferTimeoutMs: 5,
      },
    );
    expect(client.realtime.status).toBe("disconnected");
    client.realtime.connect();
    const socket = sockets[0];
    socket.connect();
    const pending = client.files.listFiles("ws-1", "/workspace");
    await expect(pending).rejects.toMatchObject({ code: "timeout" });
    client.realtime.dispose();
    expect(client.realtime.status).toBe("disposed");
  });
});

describe("reverse-proxy server prefixes", () => {
  it("keeps the frontend namespace at the origin and prefixes the Engine.IO transport path", () => {
    const socket = new MockSocket();
    const factory = vi.fn(() => socket);
    const client = clientOptions().create(vi.fn(), {
      wsBaseUrl: "https://api.example/curia",
      socketFactory: factory,
    });
    client.realtime.connect();
    expect(factory).toHaveBeenCalledWith(
      "https://api.example/frontend",
      expect.objectContaining({ path: "/curia/ws/runner" }),
    );
    client.realtime.dispose();
  });
});

describe("provider model defaults", () => {
  it("reads and saves defaults independently of provider credentials", async () => {
    const defaults = {
      small_model: "openrouter/small",
      small_effort: "low",
    };
    const fetch = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse(defaults),
    );
    const client = clientOptions().create(fetch);
    await expect(client.harness.getDefaultConfig()).resolves.toEqual(defaults);
    await expect(client.harness.saveDefaultConfig(defaults)).resolves.toEqual(
      defaults,
    );
    expect(fetch.mock.calls[1]).toMatchObject([
      "https://api.example/api/v1/provider-config/",
      { method: "PUT", body: JSON.stringify(defaults) },
    ]);
  });
});

describe("mobile notification API", () => {
  it("uses the current auth, organization and proxy prefix for registration and revocation", async () => {
    const fetch = vi
      .fn()
      .mockImplementation(async (_input, init) =>
        init?.method === "DELETE"
          ? new Response(null, { status: 204 })
          : jsonResponse({}),
      );
    const client = clientOptions({
      apiBaseUrl: "https://api.example/curia/api/v1",
    }).create(fetch);
    const registration = {
      platform: "ios" as const,
      app_id: "com.opencuria.mobile",
      apns_environment: "production" as const,
      push_token: "native-token",
      notifications_enabled: true,
      live_activities_enabled: true,
    };
    await client.notifications.capabilities();
    await client.notifications.registerDevice("device/id", registration);
    await client.notifications.registerActivity(
      "device/id",
      "activity-id",
      "activity-token",
    );
    await client.notifications.activity("device/id");
    await client.notifications.unregisterDevice("device/id");
    expect(fetch.mock.calls[1]).toMatchObject([
      "https://api.example/curia/api/v1/notifications/devices/device%2Fid/",
      { method: "PUT", body: JSON.stringify(registration) },
    ]);
    expect(fetch.mock.calls[2]).toMatchObject([
      "https://api.example/curia/api/v1/notifications/devices/device%2Fid/activity/",
      {
        method: "PUT",
        body: JSON.stringify({
          activity_id: "activity-id",
          push_token: "activity-token",
        }),
      },
    ]);
    expect(fetch.mock.calls[4]?.[1]?.method).toBe("DELETE");
    expect(
      new Headers(fetch.mock.calls[1]?.[1]?.headers).get("X-Organization-Id"),
    ).toBe("org-a");
    expect(
      new Headers(fetch.mock.calls[1]?.[1]?.headers).get("Authorization"),
    ).toBe("Bearer old-access");
    client.realtime.dispose();
  });
});

describe("engine catalog API", () => {
  it("loads every engine catalog with the current authorization context", async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse([]));
    const options = clientOptions();
    const client = options.create(fetch);
    for (const engine of ["native", "claude", "codex"]) {
      await client.harness.listEngineModels(engine);
      expect(fetch.mock.lastCall?.[0]).toContain(
        `/harness/engines/${engine}/models/`,
      );
    }
    expect(
      new Headers(fetch.mock.calls[0]?.[1]?.headers).get("X-Organization-Id"),
    ).toBe("org-a");
  });
});
