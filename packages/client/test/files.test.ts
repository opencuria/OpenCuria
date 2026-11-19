import { afterEach, describe, expect, it, vi } from "vitest";
import { createClient, FileTransferError } from "../src/index";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const NOW = 1_800_000_000;

function linkEntry(path: string, extra: Record<string, unknown> = {}) {
  const name = path.split("/").pop()!;
  return {
    path,
    name,
    type: "file",
    size: 1234,
    modified_at: 1700,
    kind: "image",
    mime_type: "image/png",
    content_url: `/workspace-files/content/tok/${name}?v=1234-1700`,
    download_url: `/workspace-files/content/tok/${name}?v=1234-1700&download=1`,
    thumbnail_url: `/workspace-files/thumbnail/tok?v=1234-1700`,
    error: "",
    ...extra,
  };
}

function makeClient(fetch: typeof globalThis.fetch, organization = { id: "org-a" }) {
  return createClient({
    apiBaseUrl: "https://api.example/api/v1",
    wsBaseUrl: "https://api.example",
    getAccessToken: () => "access",
    getRefreshToken: () => "refresh",
    setTokens: () => undefined,
    getOrganizationId: () => organization.id,
    onAuthenticationExpired: () => undefined,
    fetch,
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("workspace file links", () => {
  it("batches same-tick requests into one call and resolves absolute URLs", async () => {
    vi.useFakeTimers({ now: NOW * 1000 });
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      const { paths } = JSON.parse(String(init?.body)) as { paths: string[] };
      return jsonResponse({
        files: paths.map((path) =>
          path.endsWith("missing.png")
            ? { ...linkEntry(path), error: "file_not_found" }
            : linkEntry(path),
        ),
        expires_at: NOW + 7200,
      });
    });
    const client = makeClient(fetch as any);
    const first = client.files.link("ws-1", "/workspace/a.png");
    const again = client.files.link("ws-1", "/workspace/a.png");
    const missing = client.files.link("ws-1", "/workspace/missing.png");
    const missingRefused = expect(missing).rejects.toMatchObject({ code: "file_not_found" });
    await vi.advanceTimersByTimeAsync(0);

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("https://api.example/api/v1/workspaces/ws-1/files/links/");
    expect(JSON.parse(String(init?.body))).toEqual({
      paths: ["/workspace/a.png", "/workspace/missing.png"],
    });
    const link = await first;
    expect(await again).toBe(link);
    expect(link).toMatchObject({
      size: 1234,
      kind: "image",
      expiresAt: NOW + 7200,
      contentUrl:
        "https://api.example/api/v1/workspace-files/content/tok/a.png?v=1234-1700",
      thumbnailUrl: "https://api.example/api/v1/workspace-files/thumbnail/tok?v=1234-1700",
    });
    await missingRefused;

    // Cached until shortly before expiry, then renewed.
    expect(client.files.peekLink("ws-1", "/workspace/a.png")).toBe(link);
    await expect(client.files.link("ws-1", "/workspace/a.png")).resolves.toBe(link);
    expect(fetch).toHaveBeenCalledTimes(1);
    vi.setSystemTime((NOW + 7200 - 60) * 1000);
    expect(client.files.peekLink("ws-1", "/workspace/a.png")).toBeUndefined();
    const renewed = client.files.link("ws-1", "/workspace/a.png");
    await vi.advanceTimersByTimeAsync(0);
    await renewed;
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("forgets links on request, on context change and rejects failed batches", async () => {
    vi.useFakeTimers({ now: NOW * 1000 });
    let fail = false;
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      if (fail) return jsonResponse({ detail: "Workspace not found.", code: "workspace_not_found" }, 404);
      const { paths } = JSON.parse(String(init?.body)) as { paths: string[] };
      return jsonResponse({ files: paths.map((p) => linkEntry(p)), expires_at: NOW + 7200 });
    });
    const client = makeClient(fetch as any);
    const pending = client.files.link("ws-1", "/workspace/a.png");
    await vi.advanceTimersByTimeAsync(0);
    await pending;
    client.files.forget("ws-1", "/workspace/a.png");
    expect(client.files.peekLink("ws-1", "/workspace/a.png")).toBeUndefined();

    fail = true;
    const refused = client.files.link("ws-1", "/workspace/b.png");
    const failure = refused.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    const error = await failure;
    expect(error).toBeInstanceOf(FileTransferError);
    expect(error).toMatchObject({ code: "workspace_not_found" });
  });

  it("lets one caller abort without cancelling the shared batch", async () => {
    vi.useFakeTimers({ now: NOW * 1000 });
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      const { paths } = JSON.parse(String(init?.body)) as { paths: string[] };
      return jsonResponse({ files: paths.map((p) => linkEntry(p)), expires_at: NOW + 7200 });
    });
    const client = makeClient(fetch as any);
    const controller = new AbortController();
    const aborted = client.files.link("ws-1", "/workspace/a.png", controller.signal);
    const kept = client.files.link("ws-1", "/workspace/a.png");
    controller.abort();
    await expect(aborted).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(0);
    await expect(kept).resolves.toMatchObject({ path: "/workspace/a.png" });
  });
});

describe("workspace file reads and uploads", () => {
  it("reads only the first bytes of a file with a Range request", async () => {
    vi.useFakeTimers({ now: NOW * 1000 });
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/files/links/"))
        return jsonResponse({
          files: [linkEntry("/workspace/notes.txt", { kind: "text", size: 10 })],
          expires_at: NOW + 7200,
        });
      expect(new Headers(init?.headers).get("Range")).toBe("bytes=0-3");
      return new Response("abcd", { status: 206 });
    });
    const client = makeClient(fetch as any);
    const read = client.files.readBytes("ws-1", "/workspace/notes.txt", { maxBytes: 4 });
    await vi.advanceTimersByTimeAsync(0);
    const result = await read;
    expect(new TextDecoder().decode(result.bytes)).toBe("abcd");
    expect(result.truncated).toBe(true);
  });

  it("signs an upload, sends the bytes and reports server refusals", async () => {
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/files/uploads/")) {
        expect(JSON.parse(String(init?.body))).toEqual({
          directory: "/workspace/in",
          filename: "a.txt",
          size: 3,
          conflict: "rename",
        });
        return jsonResponse({
          upload_url: "/workspace-files/upload/tok",
          expires_at: NOW,
          max_bytes: 500 * 1024 * 1024,
        });
      }
      expect(url).toBe("https://api.example/api/v1/workspace-files/upload/tok");
      expect(init?.method).toBe("PUT");
      return jsonResponse({ path: "/workspace/in/a_1.txt", size: 3 }, 201);
    });
    vi.stubGlobal("XMLHttpRequest", undefined);
    const client = makeClient(fetch as any);
    await expect(
      client.files.upload("ws-1", {
        directory: "/workspace/in",
        filename: "a.txt",
        body: new Blob(["abc"]),
      }),
    ).resolves.toEqual({ path: "/workspace/in/a_1.txt", size: 3 });

    await expect(
      client.files.createUpload("ws-1", {
        directory: "/workspace",
        filename: "huge.bin",
        size: 501 * 1024 * 1024,
      }),
    ).rejects.toMatchObject({ code: "file_too_large" });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("workspace file link context", () => {
  it("drops cached links when the REST client is replaced", async () => {
    vi.useFakeTimers({ now: NOW * 1000 });
    const { ApiClient } = await import("../src/api");
    const { createWorkspaceFilesApi } = await import("../src/files");
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      const { paths } = JSON.parse(String(init?.body)) as { paths: string[] };
      return jsonResponse({ files: paths.map((p) => linkEntry(p)), expires_at: NOW + 7200 });
    });
    const make = (base: string) =>
      new ApiClient({
        apiBaseUrl: base,
        getAccessToken: () => "access",
        getRefreshToken: () => "refresh",
        setTokens: () => undefined,
        getOrganizationId: () => "org",
        onAuthenticationExpired: () => undefined,
        fetch: fetch as any,
      });
    let client = make("https://a.example/api/v1");
    const files = createWorkspaceFilesApi(() => client);
    const first = files.link("ws-1", "/workspace/a.png");
    await vi.advanceTimersByTimeAsync(0);
    expect((await first).contentUrl.startsWith("https://a.example/")).toBe(true);
    client = make("https://b.example/api/v1");
    expect(files.peekLink("ws-1", "/workspace/a.png")).toBeUndefined();
  });
});
