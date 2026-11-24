import { describe, expect, it, vi } from "vitest";
import {
  availabilityMessage,
  createPushProxy,
  proxyOrigin,
  pushAvailability,
  type ProxyRegistration,
} from "./pushProxy";

const input: ProxyRegistration = {
  platform: "ios",
  app_id: "com.opencuria.mobile",
  environment: "production",
  kind: "alert",
  token: "ab".repeat(32),
  server_key: "server-key",
};

function respond(status: number, body: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

describe("push proxy registration", () => {
  it("posts the native token only to the built-in proxy and returns its handle", async () => {
    const fetch = respond(201, { handle: "sealed_handle-1" });
    const proxy = createPushProxy("https://Push.Example.test/", fetch);
    await expect(proxy.register(input)).resolves.toBe("sealed_handle-1");
    expect(fetch).toHaveBeenCalledWith(
      "https://push.example.test/v1/registrations",
      expect.objectContaining({ method: "POST", body: JSON.stringify(input) }),
    );
  });

  it.each([
    [
      respond(422, { error: "unsupported_app" }),
      /does not support this app build/,
    ],
    [respond(429, { error: "rate_limited" }), /busy/],
    [respond(201, { handle: "not a handle" }), /could not register/],
    [respond(500, {}), /could not register/],
    [
      vi.fn(async () => {
        throw new TypeError("Network request failed");
      }),
      /Could not reach the push service at push\.example\.test/,
    ],
  ])(
    "explains failures without exposing the token (%#)",
    async (fetch, message) => {
      const proxy = createPushProxy("https://push.example.test", fetch);
      const error = await proxy.register(input).catch((cause: Error) => cause);
      expect(error.message).toMatch(message);
      expect(error.message).not.toContain(input.token);
    },
  );

  it("refuses to register without a valid built-in proxy URL", async () => {
    const fetch = respond(201, { handle: "h" });
    await expect(createPushProxy("", fetch).register(input)).rejects.toThrow(
      /no valid push service URL/,
    );
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("push proxy availability", () => {
  it("compares proxy origins, not spelling", () => {
    expect(proxyOrigin("https://PUSH.opencuria.dev/")).toBe(
      "https://push.opencuria.dev",
    );
    expect(proxyOrigin("ftp://push.opencuria.dev")).toBe("");
    expect(
      pushAvailability(
        { proxy_url: "https://push.opencuria.dev", server_key: "k" },
        "https://Push.OpenCuria.dev/",
      ),
    ).toBe("available");
    expect(
      pushAvailability(
        { proxy_url: "https://push.example.com", server_key: "k" },
        "https://push.opencuria.dev",
      ),
    ).toBe("mismatch");
    expect(
      pushAvailability(
        { proxy_url: null, server_key: null },
        "https://push.opencuria.dev",
      ),
    ).toBe("off");
  });

  it("names both proxies when they differ", () => {
    expect(
      availabilityMessage(
        "mismatch",
        { proxy_url: "https://push.example.com", server_key: "k" },
        "https://push.opencuria.dev",
      ),
    ).toBe(
      "This server sends notifications through push.example.com, but this app uses push.opencuria.dev.",
    );
    expect(availabilityMessage("available", null, "")).toBeNull();
  });
});
