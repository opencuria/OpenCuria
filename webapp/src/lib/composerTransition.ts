/**
 * composerTransition — handoff state for the home → chat composer morph.
 *
 * ChatHomeView arms the composer's bounding rect right before navigating to
 * the workspace chat; HarnessChatPanel consumes it on mount to FLIP-animate
 * the composer from its centered home position to the bottom chat position.
 * Module-level state is enough: the handoff happens synchronously around a
 * single router.push.
 */

/** Duration of the home exit animation (greeting/logo/picker fade). */
export const HOME_EXIT_MS = 180
/** Duration of the composer morph on the chat side. */
export const COMPOSER_MORPH_MS = 280

let pendingRect: DOMRect | null = null

/**
 * Whether the user prefers reduced motion (defensive: jsdom has no
 * matchMedia).
 */
export function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/**
 * Arm a composer morph from the given rect. No-op when reduced motion is
 * requested — the chat view then renders without any transition.
 */
export function armComposerTransition(rect: DOMRect): void {
  if (prefersReducedMotion()) return
  pendingRect = rect
}

/**
 * Consume the armed rect (read + clear). Returns null when no transition
 * was armed.
 */
export function consumeComposerTransition(): DOMRect | null {
  const rect = pendingRect
  pendingRect = null
  return rect
}

/**
 * Whether a composer morph is currently armed, without consuming it.
 */
export function isComposerTransitionPending(): boolean {
  return pendingRect !== null
}

/**
 * Clear any armed transition (e.g. when navigation is aborted).
 */
export function clearComposerTransition(): void {
  pendingRect = null
}
