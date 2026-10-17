import { describe, expect, it } from "vitest";
import { normalizeServerUrl } from "./serverConfig";

describe("normalizeServerUrl", () => {
  it("normalizes host, API prefix and trailing slash", () => {
    expect(
      normalizeServerUrl(" https://curia.example/api/v1/ ", false),
    ).toEqual({
      serverUrl: "https://curia.example",
      apiBaseUrl: "https://curia.example/api/v1",
      wsBaseUrl: "https://curia.example",
    });
  });
  it("preserves a reverse proxy path", () => {
    expect(
      normalizeServerUrl("https://example.test/curia///", false).apiBaseUrl,
    ).toBe("https://example.test/curia/api/v1");
  });
  it.each([
    "https://user:secret@example.test",
    "https://example.test?token=x",
    "https://example.test#frag",
  ])("rejects credential or query URL %s", (url) => {
    expect(() => normalizeServerUrl(url, false)).toThrow();
  });
  it("allows HTTP only in development", () => {
    expect(normalizeServerUrl("http://localhost:8000", true).apiBaseUrl).toBe(
      "http://localhost:8000/api/v1",
    );
    expect(() => normalizeServerUrl("http://example.test", false)).toThrow(
      /https/i,
    );
  });
});
