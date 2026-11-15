import { describe, expect, it } from "vitest";
import { notificationLink, targetFromLink } from "../notifications/target";
import { APPS_LINK, appLink, parseAppLink } from "./appLinks";

describe("app links", () => {
  it("round-trips links to an app and to the Apps list", () => {
    expect(appLink("app_1-x")).toBe("opencuria:///?app=app_1-x");
    expect(parseAppLink(appLink("app_1-x"))).toEqual({
      kind: "app",
      appId: "app_1-x",
    });
    expect(parseAppLink(APPS_LINK)).toEqual({ kind: "apps" });
  });

  it.each([
    "opencuria:///?app=../../escape",
    "opencuria:///?app=",
    "opencuria://host/?app=abc",
    "opencuria:///apps/abc",
    "https://curia.example/?app=abc",
    "opencuria:///",
    "not a url",
  ])("rejects %s", (url) => {
    expect(parseAppLink(url)).toBeNull();
  });

  it("does not collide with notification links", () => {
    const target = {
      deviceId: "00000000-0000-4000-8000-000000000001",
      organizationId: "00000000-0000-4000-8000-000000000002",
      workspaceId: "00000000-0000-4000-8000-000000000003",
      sessionId: "00000000-0000-4000-8000-000000000004",
    };
    expect(parseAppLink(notificationLink(target))).toBeNull();
    expect(targetFromLink(appLink("abc"))).toBeNull();
    expect(targetFromLink(APPS_LINK)).toBeNull();
  });
});
