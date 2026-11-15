import { describe, expect, it } from "vitest";
import { notificationLink, notificationTarget, targetFromLink } from "./target";

const target = {
  deviceId: "11111111-1111-4111-8111-111111111111",
  organizationId: "22222222-2222-4222-8222-222222222222",
  workspaceId: "33333333-3333-4333-8333-333333333333",
  sessionId: "44444444-4444-4444-8444-444444444444",
};

describe("native notification targets", () => {
  it("round trips bounded, validated native links", () => {
    expect(targetFromLink(notificationLink(target))).toEqual(target);
  });
  it("rejects arbitrary origins, paths, malformed IDs and objects", () => {
    for (const input of [
      null,
      [],
      { ...target, sessionId: "../../other" },
      { ...target, deviceId: 42 },
    ])
      expect(notificationTarget(input)).toBeNull();
    for (const url of [
      "https://evil.example/",
      "opencuria://evil.example/?sessionId=x",
      "opencuria:///settings",
      "javascript:alert(1)",
      "opencuria:///?sessionId=x",
    ])
      expect(targetFromLink(url)).toBeNull();
  });
});
