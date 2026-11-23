import { afterEach, describe, expect, it, vi } from "vitest";
import {
  File,
  UploadType,
  __directories,
  __downloads,
  __files,
  __setDownload,
  __setUpload,
  __uploads,
} from "./test-mocks/expo-file-system";
import { __setNextResult } from "./test-mocks/expo-image-picker";
import { classifyWorkspaceFile } from "@opencuria/chat-core/workspaceFileRefs";
import {
  FILE_TRANSFER_MAX_BYTES,
  MEBIBYTE,
  type WorkspaceFileLink,
} from "@opencuria/chat-core/workspaceFiles";
import { FileTransferError } from "@opencuria/client";
import { NativeSessionStorage } from "./storage";
import { MediaCache, NativeMediaError } from "./media";
import {
  clearPickedPhotoCache,
  pickDocuments,
  pickPhoto,
  uploadAttachments,
} from "./pickers";

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function fileLink(
  path: string,
  size: number,
  extra: Partial<WorkspaceFileLink> = {},
): WorkspaceFileLink {
  const kind = classifyWorkspaceFile(path);
  return {
    path,
    name: path.split("/").pop()!,
    type: "file",
    size,
    modifiedAt: 1,
    kind: kind === "binary" ? "binary" : kind,
    mimeType: kind === "video" ? "video/mp4" : "image/png",
    contentUrl: `https://curia.test/content${path}`,
    downloadUrl: `https://curia.test/download${path}`,
    thumbnailUrl: `https://curia.test/thumbnail${path}`,
    expiresAt: 4_000_000_000,
    ...extra,
  };
}

/** A client whose links have `size` bytes; downloads serve `body`. */
function filesClient(size: number, body = "x".repeat(size)) {
  __setDownload(() => body);
  const link = vi.fn(async (_workspace: string, path: string) =>
    fileLink(path, size),
  );
  const forget = vi.fn();
  return { client: { files: { link, forget } } as never, link, forget };
}

afterEach(() => {
  vi.restoreAllMocks();
  __files.clear();
  __directories.clear();
  __downloads.length = 0;
  __uploads.length = 0;
  clearPickedPhotoCache();
  __setNextResult({ canceled: true, assets: null });
});

describe("workspace path validation", () => {
  it.each([
    "/workspace/../private.png",
    "/workspace/./private.png",
    "https://attacker.test/a.png",
    "//attacker.test/a.png",
    "/etc/passwd",
    "/workspace/a\\..\\b.png",
    "/workspace/a\u0000.png",
  ])("rejects unsafe path %s", async (path) => {
    const { client, link } = filesClient(3);
    const media = new MediaCache(client, "workspace-secret", "scope-secret");
    await expect(media.resolvePinned(path)).rejects.toBeInstanceOf(
      NativeMediaError,
    );
    expect(link).not.toHaveBeenCalled();
    await media.dispose();
  });

  it("requests file paths literally, without decoding them again", async () => {
    const { client, link } = filesClient(3);
    const media = new MediaCache(client, "workspace-secret", "scope-secret");
    for (const path of [
      "/workspace/shots/my shot.png",
      "/workspace/shots/100% #1?.png",
      "/workspace/shots/a%20b.png",
    ]) {
      await media.describe(path);
      expect(link).toHaveBeenLastCalledWith("workspace-secret", path);
    }
    await media.dispose();
  });
});

describe("MediaCache", () => {
  it("downloads images into a .part file, moves them into place, coalesces paths and disposes them", async () => {
    const { client, link } = filesClient(3, "img");
    const media = new MediaCache(client, "workspace-secret", "scope-secret");
    const [first, second] = await Promise.all([
      media.resolvePinned("/workspace/image.png"),
      media.resolvePinned("/workspace/image.png"),
    ]);
    expect(first.uri).toBe(second.uri);
    expect(first.uri).toContain("/opencuria/");
    expect(first.uri).not.toContain("workspace-secret");
    expect(link).toHaveBeenCalledWith(
      "workspace-secret",
      "/workspace/image.png",
    );
    expect(__downloads).toHaveLength(1);
    const [url, destination, options] = __downloads[0]!;
    expect(url).toBe("https://curia.test/content/workspace/image.png");
    expect(destination).toBe(`${first.uri}.part`);
    expect(options).toMatchObject({ idempotent: true });
    expect(new File(first.uri).exists).toBe(true);
    expect(new File(destination).exists).toBe(false);
    await media.dispose();
    expect(__files.size).toBe(0);
  });

  it("deletes partial and incomplete downloads and signs the link again", async () => {
    const { client, forget } = filesClient(3);
    const media = new MediaCache(client, "workspace", "scope");
    // Android streams into the destination and leaves what arrived.
    __setDownload((_url, destination) => {
      __files.set(destination, { data: "im", encoding: "utf8" });
      throw new Error("Connection reset");
    });
    await expect(media.resolvePinned("/workspace/a.png")).rejects.toMatchObject(
      { code: "download_failed", retryable: true },
    );
    expect(__files.size).toBe(0);
    expect(forget).toHaveBeenCalledWith("workspace", "/workspace/a.png");
    __setDownload(() => "im");
    await expect(media.resolvePinned("/workspace/a.png")).rejects.toMatchObject(
      { code: "download_incomplete", retryable: true },
    );
    expect(__files.size).toBe(0);
    await media.dispose();
  });

  it("streams videos from their link without downloading them", async () => {
    const { client } = filesClient(80 * MEBIBYTE);
    const media = new MediaCache(client, "workspace", "scope");
    const video = await media.resolvePinned("/workspace/clip.mp4");
    expect(video.uri).toBe("https://curia.test/content/workspace/clip.mp4");
    video.release();
    expect(__downloads).toHaveLength(0);
    await media.dispose();
  });

  it("pins Git blob links by object id and file links like their path", async () => {
    const { client, link } = filesClient(3, "img");
    const media = new MediaCache(client, "workspace", "scope");
    const blob = (objectId: string) =>
      fileLink("/workspace/repo/logo.png", 3, {
        modifiedAt: 0,
        objectId,
        contentUrl: `https://curia.test/blob/${objectId}`,
      });
    const staged = await media.pinLink(blob("a".repeat(40)));
    const again = await media.pinLink(blob("a".repeat(40)));
    const committed = await media.pinLink(blob("c".repeat(40)));
    expect(again.uri).toBe(staged.uri);
    expect(committed.uri).not.toBe(staged.uri);
    expect(__downloads.map(([url]) => url)).toEqual([
      `https://curia.test/blob/${"a".repeat(40)}`,
      `https://curia.test/blob/${"c".repeat(40)}`,
    ]);
    expect(link).not.toHaveBeenCalled();

    // The worktree version shares the cached copy of its path.
    const onDisk = await media.pinLink(fileLink("/workspace/repo/logo.png", 3));
    const resolved = await media.resolvePinned("/workspace/repo/logo.png");
    expect(resolved.uri).toBe(onDisk.uri);
    expect(__downloads).toHaveLength(3);
    for (const pinned of [staged, again, committed, onDisk, resolved])
      pinned.release();
    await media.dispose();
    expect(__files.size).toBe(0);
  });

  it("refuses links to non-images, unsafe paths and oversized files", async () => {
    const { client } = filesClient(3);
    const media = new MediaCache(client, "workspace", "scope");
    await expect(
      media.pinLink(fileLink("/workspace/repo/notes.txt", 3)),
    ).rejects.toMatchObject({ code: "unsupported_preview" });
    await expect(
      media.pinLink(fileLink("/etc/logo.png", 3)),
    ).rejects.toMatchObject({ code: "invalid_path" });
    await expect(
      media.pinLink(
        fileLink("/workspace/huge.png", FILE_TRANSFER_MAX_BYTES + 1, {
          objectId: "b".repeat(40),
        }),
      ),
    ).rejects.toMatchObject({ code: "size_limit" });
    expect(__downloads).toHaveLength(0);
    await media.dispose();
  });

  it("describes files and refuses those above the transfer limit", async () => {
    const { client } = filesClient(FILE_TRANSFER_MAX_BYTES + 1);
    const media = new MediaCache(client, "workspace", "scope");
    await expect(media.describe("/workspace/huge.png")).resolves.toMatchObject({
      size: FILE_TRANSFER_MAX_BYTES + 1,
      thumbnailUrl: "https://curia.test/thumbnail/workspace/huge.png",
    });
    await expect(
      media.resolvePinned("/workspace/huge.png"),
    ).rejects.toMatchObject({ code: "size_limit", retryable: false });
    await expect(media.openFile("/workspace/huge.zip")).rejects.toMatchObject({
      code: "size_limit",
    });
    expect(__downloads).toHaveLength(0);
    await media.dispose();
  });

  it("keeps the codes of link failures", async () => {
    const link = vi
      .fn()
      .mockRejectedValueOnce(
        new FileTransferError("The file does not exist.", "file_not_found"),
      )
      .mockRejectedValueOnce(
        new FileTransferError(
          "This file type cannot be opened.",
          "unsupported_file",
        ),
      );
    const media = new MediaCache(
      { files: { link, forget: vi.fn() } } as never,
      "workspace",
      "scope",
    );
    await expect(media.describe("/workspace/new.png")).rejects.toMatchObject({
      code: "file_not_found",
      retryable: true,
    });
    await expect(media.describe("/workspace/odd.png")).rejects.toMatchObject({
      code: "unsupported_file",
      retryable: false,
    });
    await media.dispose();
  });

  it("limits active downloads to two and cancels them on clear", async () => {
    const { client } = filesClient(3);
    __setDownload(() => new Promise<string>(() => {}));
    const media = new MediaCache(client, "workspace", "scope");
    const pending = ["a", "b", "c"].map((name) =>
      media.resolvePinned(`/workspace/${name}.png`),
    );
    await tick();
    expect(__downloads).toHaveLength(2);
    await media.clear();
    expect(__downloads.every(([, , options]) => options?.signal?.aborted)).toBe(
      true,
    );
    const results = await Promise.allSettled(pending);
    expect(results.map((result) => result.status)).toEqual([
      "rejected",
      "rejected",
      "rejected",
    ]);
    expect(__files.size).toBe(0);
    await media.dispose();
  });

  it("keeps the LRU cache below 32 MiB while retaining newly resolved media", async () => {
    const { client } = filesClient(17 * MEBIBYTE);
    const media = new MediaCache(client, "workspace", "scope");
    (await media.resolvePinned("/workspace/first.png")).release();
    (await media.resolvePinned("/workspace/second.png")).release();
    await tick();
    expect(__files.size).toBe(1);
    await media.dispose();
  });

  it("keeps all visible media beyond the LRU budget and trims it once released", async () => {
    const { client } = filesClient(17 * MEBIBYTE);
    const media = new MediaCache(client, "workspace", "scope");
    const first = await media.resolvePinned("/workspace/first.png");
    const second = await media.resolvePinned("/workspace/second.png");
    await tick();
    const firstFile = new File(first.uri);
    const secondFile = new File(second.uri);
    expect(firstFile.exists).toBe(true);
    expect(secondFile.exists).toBe(true);
    first.release();
    await tick();
    expect(firstFile.exists).toBe(false);
    expect(secondFile.exists).toBe(true);
    second.release();
    await media.dispose();
  });

  it("lets concurrent visible reads pin their files before the LRU trims", async () => {
    const { client } = filesClient(4);
    const media = new MediaCache(client, "workspace", "scope", {
      lruBytes: 4,
      maxBytes: 64,
    });
    const pinned = await Promise.all(
      ["a", "b", "c"].map((name) =>
        media.resolvePinned(`/workspace/${name}.png`),
      ),
    );
    await tick();
    expect(pinned.every(({ uri }) => new File(uri).exists)).toBe(true);
    expect(__downloads).toHaveLength(3);
    await media.dispose();
  });

  it("refuses new media above the hard cap with a retryable error before downloading", async () => {
    const { client } = filesClient(4);
    const media = new MediaCache(client, "workspace", "scope", {
      lruBytes: 4,
      maxBytes: 8,
    });
    const first = await media.resolvePinned("/workspace/a.png");
    await media.resolvePinned("/workspace/b.png");
    const refused = await media
      .resolvePinned("/workspace/c.png")
      .catch((error: NativeMediaError) => error);
    expect(refused).toMatchObject({ code: "cache_full", retryable: true });
    expect(__downloads).toHaveLength(2);
    first.release();
    await tick();
    await expect(
      media.resolvePinned("/workspace/c.png"),
    ).resolves.toMatchObject({ uri: expect.any(String) });
    await media.dispose();
  });

  it("marks only failures that a new load cannot fix as permanent", () => {
    expect(new NativeMediaError("x", "size_limit").retryable).toBe(false);
    expect(new NativeMediaError("x", "unsupported_file").retryable).toBe(false);
    expect(new NativeMediaError("x", "file_not_found").retryable).toBe(true);
    expect(new NativeMediaError("x", "download_failed").retryable).toBe(true);
    expect(new NativeMediaError("x", "scope_changed").retryable).toBe(true);
  });

  it("downloads opened documents once and keeps only the last one", async () => {
    const { client, link } = filesClient(3, "pdf");
    link.mockImplementation(async (_workspace: string, path: string) =>
      fileLink(path, 3, { mimeType: "application/pdf" }),
    );
    const media = new MediaCache(client, "workspace", "scope", {
      lruBytes: 2,
      maxBytes: 64,
    });
    const report = await media.openFile("/workspace/docs/report.pdf");
    expect(report).toMatchObject({
      name: "report.pdf",
      mimeType: "application/pdf",
    });
    expect(__downloads[0]![0]).toBe(
      "https://curia.test/content/workspace/docs/report.pdf",
    );
    await tick();
    expect(new File(report.uri).exists).toBe(true);
    // Sharing the open document reuses its download.
    await expect(
      media.openFile("/workspace/docs/report.pdf"),
    ).resolves.toMatchObject({ uri: report.uri });
    expect(__downloads).toHaveLength(1);
    const video = await media.openFile("/workspace/clip.mp4");
    await tick();
    expect(new File(report.uri).exists).toBe(false);
    expect(new File(video.uri).exists).toBe(true);
    await media.dispose();
    expect(__files.size).toBe(0);
  });

  it("saves and shares only image/PDF data URLs", async () => {
    const media = new MediaCache({ files: {} } as never, "workspace", "scope");
    const uri = await media.saveAttachment(
      `data:image/png;base64,${btoa("tool image")}`,
    );
    expect(new File(uri).exists).toBe(true);
    await expect(
      media.saveAttachment("https://example.test/private"),
    ).rejects.toMatchObject({ code: "invalid_payload" });
    await expect(
      media.saveAttachment(`data:text/html;base64,${btoa("<p>")}`),
    ).rejects.toMatchObject({ code: "unsupported_attachment" });
    await media.dispose();
  });
});

describe("native picker upload adapter", () => {
  function uploadClient(path = "/workspace/.opencuria/user-uploaded/x") {
    const createUpload = vi.fn(async () => ({
      uploadUrl: "https://curia.test/api/v1/files/upload/token",
      expiresAt: 4_000_000_000,
      maxBytes: FILE_TRANSFER_MAX_BYTES,
    }));
    const forget = vi.fn();
    __setUpload(() => ({
      status: 201,
      body: JSON.stringify({ path, size: 5 }),
      headers: {},
    }));
    return {
      client: { files: { createUpload, forget } } as never,
      createUpload,
      forget,
    };
  }

  it("returns an empty list when document picker is cancelled", async () => {
    await expect(pickDocuments()).resolves.toEqual([]);
  });

  it("converts HEIC photos to JPEG, keeps others as picked and deletes adapter copies", async () => {
    const source = new File("file:///cache/IMG.HEIC");
    source.create();
    source.write("x".repeat(20));
    __setNextResult({
      canceled: false,
      assets: [
        {
          uri: source.uri,
          fileName: "IMG.HEIC",
          fileSize: source.size,
          mimeType: "image/heic",
          width: 4096,
          type: "image",
        },
      ],
    });
    const heic = await pickPhoto("library");
    expect(heic[0]).toMatchObject({
      name: "IMG.jpg",
      mimeType: "image/jpeg",
      size: 3,
      kind: "image",
    });
    const convertedUri = heic[0]!.uri;
    expect(new File(convertedUri).exists).toBe(true);
    clearPickedPhotoCache();
    expect(new File(convertedUri).exists).toBe(false);
    expect(source.exists).toBe(true);

    const large = new File("file:///cache/large.png");
    large.create();
    large.write("x".repeat(64));
    __setNextResult({
      canceled: false,
      assets: [
        {
          uri: large.uri,
          fileName: "large.png",
          fileSize: large.size,
          mimeType: "image/png",
          width: 4000,
          type: "image",
        },
      ],
    });
    const picked = await pickPhoto("library");
    expect(picked[0]).toMatchObject({
      uri: large.uri,
      name: "large.png",
      mimeType: "image/png",
      size: 64,
    });
  });

  it("uploads each file with one PUT and returns the path the server chose", async () => {
    const file = new File("file:///cache/source.txt");
    file.create();
    file.write("small");
    const saved = "/workspace/.opencuria/user-uploaded/source_file_1.txt";
    const { client, createUpload, forget } = uploadClient(saved);
    const paths = await uploadAttachments(client, "workspace", [
      { id: "a", uri: file.uri, name: "source file.txt" },
    ]);
    expect(createUpload).toHaveBeenCalledWith("workspace", {
      directory: "/workspace/.opencuria/user-uploaded",
      filename: "source_file.txt",
      size: 5,
      conflict: "rename",
    });
    expect(__uploads).toEqual([
      [
        "https://curia.test/api/v1/files/upload/token",
        file.uri,
        {
          httpMethod: "PUT",
          uploadType: UploadType.BINARY_CONTENT,
          headers: { "Content-Type": "application/octet-stream" },
        },
      ],
    ]);
    expect(paths).toEqual([saved]);
    expect(forget).toHaveBeenCalledWith("workspace", saved);
  });

  it("uploads an empty document", async () => {
    const file = new File("file:///cache/empty.txt");
    file.create();
    const saved = "/workspace/.opencuria/user-uploaded/empty.txt";
    const { client, createUpload } = uploadClient(saved);
    await expect(
      uploadAttachments(client, "workspace", [
        { id: "empty", uri: file.uri, name: "empty.txt" },
      ]),
    ).resolves.toEqual([saved]);
    expect(createUpload).toHaveBeenCalledWith(
      "workspace",
      expect.objectContaining({ size: 0 }),
    );
  });

  it("refuses files above the transfer limit before any upload", async () => {
    const small = new File("file:///cache/small.txt");
    small.create();
    small.write("small");
    const large = new File("file:///cache/large.bin");
    large.create();
    const sizeOf = Object.getOwnPropertyDescriptor(
      File.prototype,
      "size",
    )!.get!;
    vi.spyOn(File.prototype, "size", "get").mockImplementation(function (
      this: File,
    ) {
      return this.uri === large.uri
        ? FILE_TRANSFER_MAX_BYTES + 1
        : sizeOf.call(this);
    });
    const { client, createUpload } = uploadClient();
    await expect(
      uploadAttachments(client, "workspace", [
        { id: "a", uri: small.uri, name: "small.txt" },
        { id: "b", uri: large.uri, name: "large.bin" },
      ]),
    ).rejects.toMatchObject({
      code: "size_limit",
      message: "large.bin is too large. Files up to 500 MB can be transferred.",
    });
    expect(createUpload).not.toHaveBeenCalled();
    expect(__uploads).toHaveLength(0);
  });

  it("reports refused uploads with the server's message", async () => {
    const file = new File("file:///cache/source.txt");
    file.create();
    file.write("small");
    const { client } = uploadClient();
    __setUpload(() => ({
      status: 507,
      body: JSON.stringify({
        detail: "The workspace disk is full.",
        code: "disk_full",
      }),
      headers: {},
    }));
    await expect(
      uploadAttachments(client, "workspace", [
        { id: "a", uri: file.uri, name: "source.txt" },
      ]),
    ).rejects.toMatchObject({
      code: "disk_full",
      message: "The workspace disk is full.",
    });
    __setUpload(() => ({ status: 502, body: "Bad gateway", headers: {} }));
    await expect(
      uploadAttachments(client, "workspace", [
        { id: "a", uri: file.uri, name: "source.txt" },
      ]),
    ).rejects.toMatchObject({
      code: "upload_failed",
      message: "The upload failed (502).",
    });
  });
});

describe("NativeSessionStorage", () => {
  it("round-trips one secure session record and clears older queued saves on logout", async () => {
    const values = new Map<string, string>();
    const store = {
      getItemAsync: vi.fn(async (key: string) => values.get(key) ?? null),
      setItemAsync: vi.fn(async (key: string, value: string) => {
        values.set(key, value);
      }),
      deleteItemAsync: vi.fn(async (key: string) => {
        values.delete(key);
      }),
    };
    const storage = new NativeSessionStorage(store);
    const session = {
      accessToken: "access",
      refreshToken: "refresh",
      serverUrl: "https://server",
      organizationId: "org",
    };
    await storage.save(session);
    expect(await storage.load()).toEqual(session);
    const oldSave = storage.save({ ...session, accessToken: "stale" });
    const logout = storage.clear();
    await Promise.all([oldSave, logout]);
    expect(await storage.load()).toBeNull();
    expect(store.deleteItemAsync).toHaveBeenCalledOnce();
  });

  it("keeps the account id for offline apps and loads sessions saved without it", async () => {
    const values = new Map<string, string>();
    const storage = new NativeSessionStorage({
      getItemAsync: async (key: string) => values.get(key) ?? null,
      setItemAsync: async (key: string, value: string) => {
        values.set(key, value);
      },
      deleteItemAsync: async () => undefined,
    });
    const session = {
      accessToken: "access",
      refreshToken: "refresh",
      serverUrl: "https://server",
      organizationId: "org",
    };
    await storage.save({ ...session, userId: 7 });
    expect(await storage.load()).toEqual({ ...session, userId: 7 });
    await storage.save(session);
    expect(await storage.load()).toEqual(session);
    await expect(
      storage.save({ ...session, userId: "7" as unknown as number }),
    ).rejects.toThrow("Invalid native auth session");
  });

  it("propagates SecureStore errors without logging or masking them", async () => {
    const storage = new NativeSessionStorage({
      getItemAsync: async () => {
        throw new Error("secure failure");
      },
      setItemAsync: async () => undefined,
      deleteItemAsync: async () => undefined,
    });
    await expect(storage.load()).rejects.toThrow("secure failure");
  });
});
