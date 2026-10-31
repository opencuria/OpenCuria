import { describe, expect, it } from "vitest";
import {
  base64ToBytes,
  bytesToBase64,
  createUtf8StreamDecoder,
  encodeTerminalInput,
  utf8Encode,
} from "./utf8";

const nodeBase64 = (text: string) =>
  Buffer.from(text, "utf8").toString("base64");

describe("terminal UTF-8 codec", () => {
  it("encodes input like Node's base64 UTF-8", () => {
    for (const text of ["ls -la\r", "äöü €", "🚀 done", "\u001b[A", ""])
      expect(encodeTerminalInput(text)).toBe(nodeBase64(text));
  });

  it("round-trips bytes through base64", () => {
    const bytes = utf8Encode("Grüße 🌍");
    expect(Array.from(base64ToBytes(bytesToBase64(bytes)))).toEqual(
      Array.from(bytes),
    );
  });

  it("keeps multi-byte characters split across output chunks", () => {
    const decoder = createUtf8StreamDecoder();
    const bytes = utf8Encode("a€🚀b");
    const parts = [
      bytes.subarray(0, 2),
      bytes.subarray(2, 5),
      bytes.subarray(5),
    ];
    expect(parts.map((part) => decoder.decode(part)).join("")).toBe("a€🚀b");
  });

  it("replaces invalid bytes instead of stalling", () => {
    const decoder = createUtf8StreamDecoder();
    expect(decoder.decode(Uint8Array.from([0x61, 0xff, 0x62]))).toBe("a�b");
  });
});
