/**
 * Hand-off of a sent prompt's start position from the composer to its row.
 *
 * The composer arms an anchor that reports where its text sits; the prompt's
 * row claims it when it mounts and asks it once the row is shown, so the
 * flight starts where the composer is then (the keyboard closes on send).
 * One slot is enough: one prompt is sent at a time; an unclaimed anchor
 * expires, and a claim only serves the mount that made it.
 */

/** Reports the composer text's window position, or null when it is gone. */
export type PromptFlightAnchor = (report: (y: number | null) => void) => void;

const ARM_TTL_MS = 3000;
/** Covers a strict-mode replay of the mount, never a later remount. */
const CLAIM_TTL_MS = 1000;
const CLAIM_LIMIT = 50;

let armed: { anchor: PromptFlightAnchor; at: number } | null = null;
const claims = new Map<string, { anchor: PromptFlightAnchor; at: number }>();

export function armAnchor(anchor: PromptFlightAnchor, now = Date.now()): void {
  armed = { anchor, at: now };
}

export function disarmAnchor(): void {
  armed = null;
}

/** The armed anchor for the row `key`, once; null when none or expired. */
export function claimAnchor(
  key: string,
  now = Date.now(),
): PromptFlightAnchor | null {
  const claim = claims.get(key);
  if (claim) return now - claim.at <= CLAIM_TTL_MS ? claim.anchor : null;
  const current = armed;
  armed = null;
  if (!current || now - current.at > ARM_TTL_MS) return null;
  claims.set(key, { anchor: current.anchor, at: now });
  if (claims.size > CLAIM_LIMIT) claims.delete(claims.keys().next().value!);
  return current.anchor;
}
