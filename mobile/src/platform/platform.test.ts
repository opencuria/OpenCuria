import { afterEach, describe, expect, it, vi } from "vitest";
import { File, __directories, __files } from "./test-mocks/expo-file-system";
import { __setNextSize } from "./test-mocks/expo-image-manipulator";
import { __setNextResult } from "./test-mocks/expo-image-picker";
import { UPLOAD_MAX_BYTES } from "@opencuria/chat-core/fileChunks";
import { FileTransferError } from "@opencuria/client";
import { NativeSessionStorage } from "./storage";
import {
  MediaCache,
  NativeMediaError,
  MEDIA_CACHE_MAX_BYTES,
  MEDIA_PREVIEW_MAX_BYTES,
} from "./media";
import {
  clearPickedPhotoCache,
  existingUploadNames,
  pickDocuments,
  pickPhoto,
  uploadAttachments,
} from "./pickers";

function readResult(path: string, content = btoa("img"), mime = "image/png") {
  return {
    workspace_id: "workspace-secret",
    request_id: "request",
    path,
    content,
    size: atob(content).length,
    mime_type: mime,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  __files.clear();
  __directories.clear();
  clearPickedPhotoCache();
  __setNextResult({ canceled: true, assets: null });
  __setNextSize(3);
});

describe("workspace path validation", () => {
  it.each([
    "/workspace/../private.png",
    "/workspace/%2e%2e/private.png",
    "https://attacker.test/a.png",
    "//attacker.test/a.png",
    "/etc/passwd",
    "/workspace/a\\..\\b.png",
    "/workspace/a%00.png",
  ])("rejects unsafe path %s", async (path) => {
    const media = new MediaCache(
      { files: { readFile: vi.fn() } } as never,
      "workspace-secret",
      "scope-secret",
    );
    await expect(media.resolve(path)).rejects.toBeInstanceOf(NativeMediaError);
    await media.dispose();
  });
});

describe("MediaCache", () => {
  it("uses authenticated client reads, coalesces duplicate paths, and disposes private files", async () => {
    const readFile = vi.fn(async (_workspace: string, path: string) =>
      readResult(path),
    );
    const media = new MediaCache(
      { files: { readFile } } as never,
      "workspace-secret",
      "scope-secret",
    );
    const [uri, sameUri] = await Promise.all([
      media.resolve("/workspace/image.png"),
      media.resolve("/workspace/image.png"),
    ]);
    expect(uri).toBe(sameUri);
    expect(uri).toContain("/opencuria/");
    expect(uri).not.toContain("workspace-secret");
    expect(readFile).toHaveBeenCalledOnce();
    expect(readFile).toHaveBeenCalledWith(
      "workspace-secret",
      "/workspace/image.png",
      MEDIA_PREVIEW_MAX_BYTES + 1,
    );
    expect(new File(uri).exists).toBe(true);
    await media.dispose();
    expect(__files.size).toBe(0);
  });

  it("rejects truncated reads and validates base64 byte count", async () => {
    const readFile = vi.fn(async (_workspace: string, path: string) => ({
      ...readResult(path),
      size: MEDIA_PREVIEW_MAX_BYTES + 1,
      truncated: true,
    }));
    const media = new MediaCache(
      { files: { readFile } } as never,
      "workspace",
      "scope",
    );
    await expect(media.resolve("/workspace/large.png")).rejects.toMatchObject({
      code: "size_limit",
    });
    await media.dispose();

    const malformed = new MediaCache(
      {
        files: {
          readFile: async (_workspace: string, path: string) => ({
            ...readResult(path),
            size: 100,
          }),
        },
      } as never,
      "workspace",
      "scope",
    );
    await expect(malformed.resolve("/workspace/bad.png")).rejects.toMatchObject(
      { code: "invalid_payload" },
    );
    await malformed.dispose();
  });

  it("limits active media reads to two and ignores results after clear", async () => {
    let release!: () => void;
    const barrier = new Promise<void>((resolve) => {
      release = resolve;
    });
    let active = 0;
    let maxActive = 0;
    const readFile = vi.fn(async (_workspace: string, path: string) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await barrier;
      active -= 1;
      return readResult(path);
    });
    const media = new MediaCache(
      { files: { readFile } } as never,
      "workspace",
      "scope",
    );
    const pending = [
      media.resolve("/workspace/a.png"),
      media.resolve("/workspace/b.png"),
      media.resolve("/workspace/c.png"),
    ];
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(readFile).toHaveBeenCalledTimes(2);
    expect(maxActive).toBe(2);
    const clearing = media.clear();
    release();
    await clearing;
    await expect(Promise.all(pending)).rejects.toMatchObject({
      code: "scope_changed",
    });
    expect(__files.size).toBe(0);
    await media.dispose();
  });

  it("keeps the LRU cache below 32 MiB while retaining newly resolved media", async () => {
    const content = btoa("x".repeat(17 * 1024 * 1024));
    const readFile = vi.fn(async (_workspace: string, path: string) =>
      readResult(path, content),
    );
    const media = new MediaCache(
      { files: { readFile } } as never,
      "workspace",
      "scope",
    );
    await media.resolve("/workspace/first.png");
    await media.resolve("/workspace/second.png");
    expect(__files.size).toBe(1);
    expect(
      [...__files.values()].reduce(
        (sum, item) =>
          sum +
          (item.encoding === "base64"
            ? Math.floor((item.data.length * 3) / 4)
            : item.data.length),
        0,
      ),
    ).toBeLessThanOrEqual(MEDIA_CACHE_MAX_BYTES);
    await media.dispose();
  });

  it("keeps pinned visible media out of LRU eviction until its release handle is called", async () => {
    const content = btoa("x".repeat(17 * 1024 * 1024));
    const readFile = vi.fn(async (_workspace: string, path: string) =>
      readResult(path, content),
    );
    const media = new MediaCache(
      { files: { readFile } } as never,
      "workspace",
      "scope",
    );
    const pinned = await media.resolvePinned("/workspace/visible.png");
    const pinnedFile = new File(pinned.uri);
    await expect(media.resolve("/workspace/other.png")).rejects.toMatchObject({
      code: "size_limit",
    });
    expect(pinnedFile.exists).toBe(true);
    pinned.release();
    const nextUri = await media.resolve("/workspace/other.png");
    expect(nextUri).not.toBe(pinned.uri);
    expect(pinnedFile.exists).toBe(false);
    await media.dispose();
  });

  it("reads bounded text and explicitly downloads bounded binary files", async () => {
    const textBase64 = btoa("hello native");
    const readFile = vi.fn(async (_workspace: string, path: string) => ({
      ...readResult(path, textBase64, "text/plain"),
      size: 12,
    }));
    const downloadFile = vi.fn(async (_workspace: string, path: string) => ({
      ...readResult(path, btoa("pdf"), "application/pdf"),
      size: 3,
      filename: "report.pdf",
      is_archive: false,
    }));
    const media = new MediaCache(
      { files: { readFile, downloadFile } } as never,
      "workspace",
      "scope",
    );
    const text = await media.openFile("/workspace/readme.txt");
    expect(text.text).toBe("hello native");
    expect(readFile).toHaveBeenCalledWith(
      "workspace",
      "/workspace/readme.txt",
      MEDIA_PREVIEW_MAX_BYTES + 1,
    );
    const document = await media.openFile("/workspace/report.pdf");
    expect(document.name).toBe("report.pdf");
    expect(new File(document.uri).exists).toBe(true);
    expect(downloadFile).toHaveBeenCalledOnce();
    await media.dispose();
    expect(__files.size).toBe(0);
  });

  it("saves and shares only bounded image/PDF data URLs", async () => {
    const media = new MediaCache({ files: {} } as never, "workspace", "scope");
    const uri = await media.saveAttachment(
      `data:image/png;base64,${btoa("tool image")}`,
    );
    expect(new File(uri).exists).toBe(true);
    await expect(
      media.saveAttachment("https://example.test/private"),
    ).rejects.toMatchObject({ code: "invalid_payload" });
    await media.dispose();
  });
});

describe("native picker upload adapter", () => {
  it("returns an empty list when document picker is cancelled", async () => {
    await expect(pickDocuments()).resolves.toEqual([]);
  });

  it("normalizes HEIC and oversized raster photos to bounded JPEG and deletes adapter copies", async () => {
    const source = new File("file:///cache/IMG.HEIC");
    source.create();
    source.write("x".repeat(UPLOAD_MAX_BYTES + 4));
    __setNextSize(3);
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

    const oversized = new File("file:///cache/large.png");
    oversized.create();
    oversized.write("x".repeat(UPLOAD_MAX_BYTES + 4));
    __setNextResult({
      canceled: false,
      assets: [
        {
          uri: oversized.uri,
          fileName: "large.png",
          fileSize: oversized.size,
          mimeType: "image/png",
          width: 4000,
          type: "image",
        },
      ],
    });
    const resized = await pickPhoto("library");
    expect(resized[0]).toMatchObject({
      name: "large.jpg",
      mimeType: "image/jpeg",
      size: 3,
    });
    expect(new File(resized[0]!.uri).exists).toBe(true);
  });

  it("allows an empty document to be uploaded without treating it as unreadable", async () => {
    const file = new File("file:///cache/empty.txt");
    file.create();
    const uploadFile = vi.fn(async () => ({ status: "success" }));
    await expect(
      uploadAttachments(
        {
          files: {
            listFiles: async () => ({ entries: [], error: undefined }),
            uploadFile,
          },
        } as never,
        "workspace",
        [{ id: "empty", uri: file.uri, name: "empty.txt" }],
      ),
    ).resolves.toEqual([expect.stringMatching(/empty\.txt$/)]);
    expect(uploadFile).toHaveBeenCalledOnce();
  });

  it("does not read base64 for files above the 10 MiB cap", async () => {
    const file = new File("file:///cache/large.bin");
    file.create();
    file.write("x".repeat(UPLOAD_MAX_BYTES + 1));
    const uploadFile = vi.fn();
    await expect(
      uploadAttachments(
        { files: { listFiles: vi.fn(), uploadFile } } as never,
        "workspace",
        [{ id: "a", uri: file.uri, name: "large.bin" }],
      ),
    ).rejects.toMatchObject({ code: "size_limit" });
    expect(uploadFile).not.toHaveBeenCalled();
  });

  it("lists before upload, uses shared names, and returns uploaded workspace paths", async () => {
    const file = new File("file:///cache/source.txt");
    file.create();
    file.write(btoa("small"), { encoding: "base64" });
    const listFiles = vi.fn(async () => ({
      entries: [
        {
          name: "existing.txt",
          path: "/workspace/.opencuria/user-uploaded/existing.txt",
          type: "file",
          size: 2,
        },
      ],
      error: undefined,
    }));
    const uploadFile = vi.fn(async () => ({ status: "success" }));
    const paths = await uploadAttachments(
      { files: { listFiles, uploadFile } } as never,
      "workspace",
      [{ id: "a", uri: file.uri, name: "source.txt" }],
    );
    expect(listFiles).toHaveBeenCalledOnce();
    expect(uploadFile).toHaveBeenCalledOnce();
    expect(uploadFile.mock.calls[0]![1].filename).toMatch(/_source\.txt$/);
    expect(paths[0]).toBe(
      `/workspace/.opencuria/user-uploaded/${uploadFile.mock.calls[0]![1].filename}`,
    );
  });

  it("ignores only explicit missing upload directory and fails safely for other listing errors", async () => {
    const missing = await existingUploadNames(
      {
        files: {
          listFiles: async () => ({
            entries: [],
            error:
              "Failed to list files: find: '/workspace/.opencuria/user-uploaded': No such file or directory",
          }),
        },
      } as never,
      "workspace",
    );
    expect(missing.size).toBe(0);
    const uploadFile = vi.fn();
    const listFiles = vi.fn(async () => {
      throw new Error("socket disconnected");
    });
    const file = new File("file:///cache/source.txt");
    file.create();
    file.write(btoa("x"), { encoding: "base64" });
    await expect(
      uploadAttachments(
        { files: { listFiles, uploadFile } } as never,
        "workspace",
        [{ id: "a", uri: file.uri, name: "source.txt" }],
      ),
    ).rejects.toThrow("socket disconnected");
    expect(uploadFile).not.toHaveBeenCalled();
  });

  it("uploads into a fresh workspace when the runner reports the missing upload directory", async () => {
    // The shared client rejects with the runner's raw `find` stderr, which
    // ends with a newline.
    const listFiles = vi.fn(async () => {
      throw new FileTransferError(
        "Failed to list files: find: ‘/workspace/.opencuria/user-uploaded’: No such file or directory\n",
      );
    });
    const uploadFile = vi.fn(async () => ({ status: "success" }));
    const file = new File("file:///cache/source.txt");
    file.create();
    file.write(btoa("x"), { encoding: "base64" });
    const paths = await uploadAttachments(
      { files: { listFiles, uploadFile } } as never,
      "workspace",
      [{ id: "a", uri: file.uri, name: "source.txt" }],
    );
    expect(uploadFile).toHaveBeenCalledOnce();
    expect(paths[0]).toMatch(
      /^\/workspace\/\.opencuria\/user-uploaded\/[^/]+_source\.txt$/,
    );
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
