import { describe, expect, it } from "vitest";

import {
  FILE_CHUNK_B64_SIZE,
  UPLOAD_MAX_BYTES,
  planFilesUpload,
} from "../src/fileChunks";

function base64ForRawBytes(bytes: number): string {
  return "AAAA".repeat(Math.floor(bytes / 3)) + ["", "AA==", "AAA="][bytes % 3];
}

describe("planFilesUpload", () => {
  it.each([0, 1, 3, 300 * 1024, UPLOAD_MAX_BYTES])(
    "plans %i bytes through one protocol",
    (bytes) => {
      const content = base64ForRawBytes(bytes);
      const plan = planFilesUpload(
        "ws",
        "req",
        "/workspace",
        "a.bin",
        content,
        true,
      );
      const chunks = plan.slice(1, -1);
      expect(plan[0]).toEqual({
        event: "frontend:files_upload_start",
        payload: {
          workspace_id: "ws",
          request_id: "req",
          path: "/workspace",
          filename: "a.bin",
          is_directory: true,
          total_chunks: chunks.length,
        },
      });
      expect(plan.at(-1)).toEqual({
        event: "frontend:files_upload_finish",
        payload: { workspace_id: "ws", request_id: "req", path: "/workspace" },
      });
      expect(chunks.map(({ payload }) => payload.content).join("")).toBe(
        content,
      );
      chunks.forEach(({ event, payload }, index) => {
        expect(event).toBe("frontend:files_upload_chunk");
        expect(payload).toMatchObject({ index, total_chunks: chunks.length });
        expect((payload.content as string).length).toBeLessThanOrEqual(
          FILE_CHUNK_B64_SIZE,
        );
      });
    },
  );

  it("normalizes base64 whitespace", () => {
    expect(
      planFilesUpload("ws", "req", "/workspace", "a.txt", "aGVs\nbG8=  ")[1]
        .payload.content,
    ).toBe("aGVsbG8=");
  });

  it.each([
    ["!!!not-base64!!!", /invalid base64/],
    [base64ForRawBytes(UPLOAD_MAX_BYTES + 1), /exceeds.*10 MB/],
  ])(
    "rejects invalid or oversized content before planning",
    (content, error) => {
      expect(() =>
        planFilesUpload("ws", "req", "/workspace", "a.bin", content as string),
      ).toThrow(error);
    },
  );
});
