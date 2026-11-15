import { describe, expect, it } from "vitest";
import {
  describeServerUrl,
  isLocalNetworkHost,
  normalizeServerUrl,
  tryNormalizeServerUrl,
} from "./serverConfig";

describe("normalizeServerUrl", () => {
  it("normalizes host, API prefix and trailing slash", () => {
    expect(normalizeServerUrl(" https://curia.example/api/v1/ ")).toEqual({
      serverUrl: "https://curia.example",
      apiBaseUrl: "https://curia.example/api/v1",
      wsBaseUrl: "https://curia.example",
    });
  });
  it("preserves a reverse proxy path", () => {
    expect(normalizeServerUrl("https://example.test/curia///").apiBaseUrl).toBe(
      "https://example.test/curia/api/v1",
    );
  });
  it.each([
    "https://user:secret@example.test",
    "https://example.test?token=x",
    "https://example.test#frag",
  ])("rejects credential or query URL %s", (url) => {
    expect(() => normalizeServerUrl(url)).toThrow();
  });

  it.each([
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://10.0.2.2:8000",
    "http://192.168.1.20:8000",
    "http://172.16.0.5",
    "http://curia.local",
    "http://nas:8000",
    "http://[::1]:8000",
  ])("allows plain HTTP on the local network: %s", (url) => {
    expect(normalizeServerUrl(url).serverUrl).toBe(url);
  });
  it.each([
    "http://example.test",
    "http://8.8.8.8",
    "http://172.32.0.1",
    "http://192.169.1.1",
    "http://10.0.0.1.example.test",
    "http://localhost.example.test",
  ])("rejects plain HTTP on public hosts: %s", (url) => {
    expect(() => normalizeServerUrl(url)).toThrow(/https/i);
  });
  it("defaults to http for local hosts and https for everything else", () => {
    expect(normalizeServerUrl("192.168.1.20:8000").apiBaseUrl).toBe(
      "http://192.168.1.20:8000/api/v1",
    );
    expect(normalizeServerUrl("localhost:8000").serverUrl).toBe(
      "http://localhost:8000",
    );
    expect(normalizeServerUrl("curia.example").serverUrl).toBe(
      "https://curia.example",
    );
  });
  it("still accepts explicit https on local hosts", () => {
    expect(normalizeServerUrl("https://192.168.1.20").serverUrl).toBe(
      "https://192.168.1.20",
    );
  });
});

describe("isLocalNetworkHost", () => {
  it.each([
    "127.0.0.1",
    "169.254.1.1",
    "172.31.255.255",
    "[fd12:3456::1]",
    "[fe80::1]",
  ])("treats %s as local", (host) =>
    expect(isLocalNetworkHost(host)).toBe(true),
  );
  it.each(["example.test", "1.1.1.1", "172.15.0.1", "[2001:db8::1]"])(
    "treats %s as public",
    (host) => expect(isLocalNetworkHost(host)).toBe(false),
  );
});

describe("server URL helpers", () => {
  it("returns null instead of throwing", () => {
    expect(tryNormalizeServerUrl("http://example.test")).toBeNull();
    expect(tryNormalizeServerUrl("")).toBeNull();
    expect(tryNormalizeServerUrl("curia.example")?.serverUrl).toBe(
      "https://curia.example",
    );
  });
  it("hides the implied https scheme in labels", () => {
    expect(describeServerUrl("https://curia.example/app")).toBe(
      "curia.example/app",
    );
    expect(describeServerUrl("http://192.168.1.20:8000")).toBe(
      "http://192.168.1.20:8000",
    );
  });
});
