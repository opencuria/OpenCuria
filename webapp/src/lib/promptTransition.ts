/**
 * promptTransition — a sent prompt flies out of the composer into the chat.
 *
 * The sender arms the rect of the composer text right before the prompt
 * reaches the transcript; the prompt's bubble consumes it when it mounts and
 * animates from there to its place (FLIP). One slot is enough: one prompt is
 * sent at a time, and an armed rect nobody claims expires.
 */
import { prefersReducedMotion } from './composerTransition'

/** Shortest flight from the composer to the bubble's place. */
export const PROMPT_FLIGHT_MS = 320
/** A long flight (the first prompt of a new chat) takes longer, up to this. */
const PROMPT_FLIGHT_MAX_MS = 560
const ARM_TTL_MS = 3000

let armed: { rect: DOMRect; at: number } | null = null

/** Arm a flight from `rect`; no-op without a rect or with reduced motion. */
export function armPromptTransition(rect: DOMRect | null | undefined): void {
  armed = rect && !prefersReducedMotion() ? { rect, at: Date.now() } : null
}

/** Take the armed rect (read + clear); null when none is armed or it expired. */
export function consumePromptTransition(): DOMRect | null {
  const current = armed
  armed = null
  return current && Date.now() - current.at <= ARM_TTL_MS ? current.rect : null
}

/**
 * Fly `el` from `from` to where it is laid out. It stays hidden until the
 * next frame, after the transcript scrolled to its new end, then rises.
 */
export function playPromptTransition(el: HTMLElement, from: DOMRect): void {
  if (typeof el.animate !== 'function') return
  el.style.opacity = '0'
  requestAnimationFrame(() => {
    el.style.opacity = ''
    if (!el.isConnected) return
    const to = el.getBoundingClientRect()
    const dy = from.top - to.top
    if (Math.abs(dy) < 1) return
    el.style.transformOrigin = 'right bottom'
    el.animate(
      [
        { transform: `translateY(${dy}px) scale(0.97)`, opacity: 0 },
        { opacity: 1, offset: 0.3 },
        { transform: 'none', opacity: 1 },
      ],
      {
        duration: Math.min(
          PROMPT_FLIGHT_MAX_MS,
          Math.max(PROMPT_FLIGHT_MS, 260 + Math.abs(dy) / 3),
        ),
        easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
    )
  })
}
