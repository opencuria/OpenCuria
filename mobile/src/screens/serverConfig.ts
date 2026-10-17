export interface ServerConfig {
  serverUrl: string;
  apiBaseUrl: string;
  wsBaseUrl: string;
}

/** Normalize a server origin/path, rejecting secrets and URL modifiers. */
export function normalizeServerUrl(
  value: string,
  development = isDevelopment(),
): ServerConfig {
  const input = value.trim();
  if (!input) throw new Error("Enter your OpenCuria server URL.");
  if (/[?#]/.test(input))
    throw new Error("The server URL cannot contain a query or fragment.");

  let parsed: URL;
  try {
    parsed = new URL(input.includes("://") ? input : `https://${input}`);
  } catch {
    throw new Error("Enter a valid server URL, such as https://curia.example.");
  }
  if (
    parsed.protocol !== "https:" &&
    !(development && parsed.protocol === "http:")
  ) {
    throw new Error(
      development
        ? "Use an https:// server URL (http:// is allowed only in development)."
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

function isDevelopment(): boolean {
  return typeof __DEV__ !== "undefined" && __DEV__;
}
