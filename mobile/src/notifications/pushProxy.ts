import type { PushCapabilities } from "@opencuria/client";

/** What the push proxy needs to seal a native token for one server. */
export interface ProxyRegistration {
  platform: "ios" | "android";
  app_id: string;
  environment: "sandbox" | "production";
  kind: "alert" | "activity";
  token: string;
  server_key: string;
}

/**
 * The push proxy built into this app. It holds the app's APNs/FCM credentials,
 * so a server can only reach this app through the same proxy; it only ever
 * relays ciphertext the app decrypts on the device.
 */
export interface PushProxy {
  readonly url: string;
  /** Exchange a native token for an opaque handle bound to one server key. */
  register(input: ProxyRegistration): Promise<string>;
}

export type PushAvailability = "available" | "off" | "mismatch";

/** Origin of a proxy URL, or "" if it is not a valid http(s) URL. */
export function proxyOrigin(url: string | null | undefined): string {
  if (!url) return "";
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return "";
    return `${parsed.protocol}//${parsed.host}`.toLowerCase();
  } catch {
    return "";
  }
}

function host(url: string | null | undefined): string {
  return (
    proxyOrigin(url).replace(/^https?:\/\//, "") || "an unknown push service"
  );
}

export function pushAvailability(
  capabilities: PushCapabilities,
  appProxyUrl: string,
): PushAvailability {
  if (!capabilities.proxy_url || !capabilities.server_key) return "off";
  const server = proxyOrigin(capabilities.proxy_url);
  return server && server === proxyOrigin(appProxyUrl)
    ? "available"
    : "mismatch";
}

/** Why notifications cannot be turned on, or null when they can. */
export function availabilityMessage(
  availability: PushAvailability | null,
  capabilities: PushCapabilities | null,
  appProxyUrl: string,
): string | null {
  if (availability === "off")
    return "This server has turned off mobile notifications.";
  if (availability === "mismatch")
    return `This server sends notifications through ${host(capabilities?.proxy_url)}, but this app uses ${host(appProxyUrl)}.`;
  return null;
}

const HANDLE = /^[A-Za-z0-9_-]{1,8192}$/;

export function createPushProxy(
  url: string,
  fetchImpl: typeof fetch = fetch,
  timeoutMs = 15_000,
): PushProxy {
  return {
    url,
    async register(input) {
      const origin = proxyOrigin(url);
      if (!origin)
        throw new Error("This app build has no valid push service URL.");
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), timeoutMs);
      let response: Response;
      try {
        response = await fetchImpl(`${origin}/v1/registrations`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
          signal: abort.signal,
        });
      } catch {
        throw new Error(`Could not reach the push service at ${host(url)}.`);
      } finally {
        clearTimeout(timer);
      }
      const body = (await response.json().catch(() => ({}))) as {
        handle?: unknown;
        error?: unknown;
      };
      if (
        response.status === 201 &&
        typeof body.handle === "string" &&
        HANDLE.test(body.handle)
      )
        return body.handle;
      if (body.error === "unsupported_app")
        throw new Error(
          `The push service at ${host(url)} does not support this app build.`,
        );
      if (response.status === 429)
        throw new Error("The push service is busy. Try again in a minute.");
      throw new Error(
        `The push service at ${host(url)} could not register this device.`,
      );
    },
  };
}
