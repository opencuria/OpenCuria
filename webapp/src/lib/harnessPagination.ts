/** Maximum number of chat timeline rows mounted by one paginated list. */
export const HARNESS_PAGE_SIZE = 80

/**
 * Whether a changed block list represents replacement/reordering rather than
 * streamed growth or truncation. Appending streamed blocks must not reset the
 * reader's current page.
 */
export function shouldResetHarnessPage(previous: string[], next: string[]): boolean {
  if (next.length < previous.length) return true
  const sharedLength = Math.min(previous.length, next.length)
  for (let index = 0; index < sharedLength; index += 1) {
    if (previous[index] !== next[index]) return true
  }
  return false
}

/** Last valid page index for a timeline block count. */
export function lastHarnessPage(totalItems: number): number {
  return Math.max(0, Math.ceil(totalItems / HARNESS_PAGE_SIZE) - 1)
}
