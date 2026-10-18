export interface ServerConfig {
  serverUrl: string;
  apiBaseUrl: string;
  wsBaseUrl: string;
}

/**
 * Normalize a server origin/path, rejecting secrets and URL modifiers.
 *
 * `https://` is required except for local-network hosts (see
 * {@link isLocalNetworkHost}), which may use plain `http://`. A scheme-less
 * local host defaults to `http://`, everything else to `https://`.
 */
export function normalizeServerUrl(value: string): ServerConfig {
  const input = value.trim();
  if (!input) throw new Error("Enter your OpenCuria server URL.");
  if (/[?#]/.test(input))
    throw new Error("The server URL cannot contain a query or fragment.");

  let parsed: URL;
  try {
    parsed = new URL(input.includes("://") ? input : `https://${input}`);
    if (!input.includes("://") && isLocalNetworkHost(parsed.hostname)) {
      parsed = new URL(`http://${input}`);
    }
  } catch {
    throw new Error("Enter a valid server URL, such as https://curia.example.");
  }
  if (
    parsed.protocol !== "https:" &&
    !(parsed.protocol === "http:" && isLocalNetworkHost(parsed.hostname))
  ) {
    throw new Error(
      parsed.protocol === "http:"
        ? "Use https:// — plain http:// is only allowed for local network servers."
        : "Use an https:// server URL.",
    );
  }
  if (!parsed.hostname || parsed.username || parsed.password) {
    throw new Error("The server URL must not contain credentials.");
  }
  if (parsed.search || parsed.hash)
    throw new Error("The server URL cannot contain a query or fragment.");

  let path = parsed.pathname.replace(/\/+$/, "");
  if (path === "/api/v1") path = "";
  else if (path.endsWith("/api/v1")) path = path.slice(0, -7);
  const serverUrl = `${parsed.origin}${path}`;
  return { serverUrl, apiBaseUrl: `${serverUrl}/api/v1`, wsBaseUrl: serverUrl };
}

/** Like {@link normalizeServerUrl}, but returns `null` instead of throwing. */
export function tryNormalizeServerUrl(value: string): ServerConfig | null {
  try {
    return normalizeServerUrl(value);
  } catch {
    return null;
  }
}

/** Short, human-readable server label: `https://` is implied and omitted. */
export function describeServerUrl(serverUrl: string): string {
  return serverUrl.replace(/^https:\/\//, "");
}

/**
 * Whether a host is reachable only inside a local network, so plain HTTP is
 * acceptable: loopback, private/link-local IP literals, `localhost`, `.local`
 * (mDNS) and unqualified names such as `http://nas:8000`. This is the same set
 * iOS allows through `NSAllowsLocalNetworking`. Android cannot scope cleartext
 * traffic to IP ranges, so this check is what keeps public hosts on HTTPS.
 */
export function isLocalNetworkHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  if (host.startsWith("[") && host.endsWith("]")) {
    const ipv6 = host.slice(1, -1);
    return (
      ipv6 === "::1" ||
      /^f[cd][0-9a-f]{2}:/.test(ipv6) ||
      /^fe[89ab]/.test(ipv6)
    );
  }
  const ipv4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (ipv4) {
    const [a, b] = [Number(ipv4[1]), Number(ipv4[2])];
    return (
      a === 10 ||
      a === 127 ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168)
    );
  }
  return (
    !host.includes(".") ||
    host.endsWith(".local") ||
    host.endsWith(".localhost")
  );
}
