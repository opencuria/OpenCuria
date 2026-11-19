import { describe, expect, it } from "vitest";
import {
  FILE_TRANSFER_MAX_BYTES,
  MEDIA_AUTOLOAD_MAX_BYTES,
  canTransferFile,
  fileTransferLimitMessage,
  mediaLoadMode,
} from "@opencuria/chat-core/workspaceFiles";

describe("workspace file policy", () => {
  it("loads small chat media by itself and larger media on request", () => {
    expect(mediaLoadMode({ size: 0 })).toBe("auto");
    expect(mediaLoadMode({ size: MEDIA_AUTOLOAD_MAX_BYTES })).toBe("auto");
    expect(mediaLoadMode({ size: MEDIA_AUTOLOAD_MAX_BYTES + 1 })).toBe("tap");
    expect(mediaLoadMode({ size: MEDIA_AUTOLOAD_MAX_BYTES + 1 }, true)).toBe("auto");
    expect(mediaLoadMode({ size: FILE_TRANSFER_MAX_BYTES })).toBe("tap");
    expect(mediaLoadMode({ size: FILE_TRANSFER_MAX_BYTES + 1 }, true)).toBe(
      "too_large",
    );
  });

  it("bounds transfers at 500 MB", () => {
    expect(canTransferFile(FILE_TRANSFER_MAX_BYTES)).toBe(true);
    expect(canTransferFile(FILE_TRANSFER_MAX_BYTES + 1)).toBe(false);
    expect(canTransferFile(-1)).toBe(false);
    expect(fileTransferLimitMessage()).toBe("Files up to 500 MB can be transferred.");
  });
});
