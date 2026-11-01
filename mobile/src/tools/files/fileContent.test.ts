import { describe, expect, it } from "vitest";
import type { OpenCuriaClient } from "@opencuria/client";
import {
  BinaryFileError,
  delimiterFor,
  isVideoPath,
  parseDelimited,
  readWorkspaceText,
} from "./fileContent";

const clientReturning = (
  content: Buffer,
  extra: Record<string, unknown> = {},
) =>
  ({
    files: {
      readFile: async () => ({
        content: content.toString("base64"),
        size: content.length,
        truncated: false,
        ...extra,
      }),
    },
  }) as unknown as OpenCuriaClient;

it("recognizes workspace videos while keeping TypeScript files as source", () => {
  expect(isVideoPath("/workspace/recording.MP4")).toBe(true);
  expect(isVideoPath("/workspace/demo.mov")).toBe(true);
  expect(isVideoPath("/workspace/index.ts")).toBe(false);
  expect(isVideoPath("/workspace/index.TS")).toBe(false);
  expect(isVideoPath("/workspace/index.tsx")).toBe(false);
});

describe("readWorkspaceText", () => {
  it("decodes UTF-8 and drops a BOM", async () => {
    const result = await readWorkspaceText(
      clientReturning(Buffer.from("﻿Grüße\n", "utf8")),
      "ws",
      "/workspace/a.txt",
    );
    expect(result).toMatchObject({ text: "Grüße\n", truncated: false });
  });

  it("rejects binary content", async () => {
    await expect(
      readWorkspaceText(
        clientReturning(Buffer.from([0x89, 0x50, 0, 1])),
        "ws",
        "/workspace/a.bin",
      ),
    ).rejects.toBeInstanceOf(BinaryFileError);
  });

  it("reports a truncated preview", async () => {
    const result = await readWorkspaceText(
      clientReturning(Buffer.from("abc"), { size: 5_000_000, truncated: true }),
      "ws",
      "/workspace/big.log",
    );
    expect(result.truncated).toBe(true);
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
