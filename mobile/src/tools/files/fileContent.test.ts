import { describe, expect, it, vi } from "vitest";
import { TEXT_PREVIEW_MAX_BYTES } from "@opencuria/chat-core/workspaceFiles";
import type { OpenCuriaClient } from "@opencuria/client";
import {
  BinaryFileError,
  delimiterFor,
  isVideoPath,
  parseDelimited,
  readWorkspaceText,
} from "./fileContent";

const clientReturning = (content: Buffer, size = content.length) => {
  const readBytes = vi.fn(async () => ({
    link: { size },
    bytes: new Uint8Array(content),
    truncated: size > content.length,
  }));
  return {
    client: { files: { readBytes } } as unknown as OpenCuriaClient,
    readBytes,
  };
};

it("recognizes workspace videos while keeping TypeScript files as source", () => {
  expect(isVideoPath("/workspace/recording.MP4")).toBe(true);
  expect(isVideoPath("/workspace/demo.mov")).toBe(true);
  expect(isVideoPath("/workspace/index.ts")).toBe(false);
  expect(isVideoPath("/workspace/index.TS")).toBe(false);
  expect(isVideoPath("/workspace/index.tsx")).toBe(false);
});

describe("readWorkspaceText", () => {
  it("reads the preview range, decodes UTF-8 and drops a BOM", async () => {
    const { client, readBytes } = clientReturning(
      Buffer.from("\uFEFFGrüße\n", "utf8"),
    );
    const result = await readWorkspaceText(client, "ws", "/workspace/a.txt");
    expect(result).toMatchObject({ text: "Grüße\n", truncated: false });
    expect(readBytes).toHaveBeenCalledWith("ws", "/workspace/a.txt", {
      maxBytes: TEXT_PREVIEW_MAX_BYTES,
    });
  });

  it("rejects binary content", async () => {
    await expect(
      readWorkspaceText(
        clientReturning(Buffer.from([0x89, 0x50, 0, 1])).client,
        "ws",
        "/workspace/a.bin",
      ),
    ).rejects.toBeInstanceOf(BinaryFileError);
  });

  it("reports a truncated preview with the file size", async () => {
    const result = await readWorkspaceText(
      clientReturning(Buffer.from("abc"), 5_000_000).client,
      "ws",
      "/workspace/big.log",
    );
    expect(result).toMatchObject({ truncated: true, size: 5_000_000 });
  });

  it("passes link failures on", async () => {
    const readBytes = vi.fn().mockRejectedValue(new Error("Permission denied"));
    await expect(
      readWorkspaceText(
        { files: { readBytes } } as unknown as OpenCuriaClient,
        "ws",
        "/workspace/private.txt",
      ),
    ).rejects.toThrow("Permission denied");
  });
});

describe("parseDelimited", () => {
  it("handles quotes, escaped quotes and CRLF", () => {
    expect(parseDelimited('a,"b,c","say ""hi"""\r\n1,2,3\n', ",")).toEqual([
      ["a", "b,c", 'say "hi"'],
      ["1", "2", "3"],
    ]);
    expect(delimiterFor("data/x.TSV")).toBe("\t");
  });
});
