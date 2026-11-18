import { describe, expect, it } from "vitest";

import { shouldMarkViewedSessionRead } from "@opencuria/chat-core/harnessReadState";

describe("shouldMarkViewedSessionRead", () => {
  it("marks a finished unread session read", () => {
    expect(shouldMarkViewedSessionRead({ status: "idle", unread: true })).toBe(
      true,
    );
  });

  it("keeps a deliberate manual-unread marker", () => {
    expect(
      shouldMarkViewedSessionRead({
        status: "idle",
        unread: true,
        manual_unread: true,
      }),
    ).toBe(false);
  });

  it("waits for a running session and skips read or unknown ones", () => {
    expect(shouldMarkViewedSessionRead({ status: "busy", unread: true })).toBe(
      false,
    );
    expect(shouldMarkViewedSessionRead({ status: "idle", unread: false })).toBe(
      false,
    );
    expect(shouldMarkViewedSessionRead(undefined)).toBe(false);
  });
});
