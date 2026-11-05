/**
 * Settings routes. A route is "<page>" or "<page>:<argument>", e.g.
 * "skill:new" or "provider-account:openrouter:<id>"; arguments may contain
 * further colons.
 */
export function route(page: string, ...args: string[]): string {
  return [page, ...args].join(":");
}

/** Splits a route into its page and its argument (everything after the first colon). */
export function parseRoute(value: string): { page: string; argument: string } {
  const index = value.indexOf(":");
  return index < 0
    ? { page: value, argument: "" }
    : { page: value.slice(0, index), argument: value.slice(index + 1) };
}
