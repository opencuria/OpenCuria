/**
 * Host element registry for the persistent desktop surface.
 *
 * SidePanelDesktop and the WorkspaceDesktop modal register their host
 * containers here via callback refs. DesktopSurface teleports the single
 * KasmVNC iframe into the active host, so opening/closing the modal moves
 * the existing iframe instead of reloading it.
 */
import { shallowRef } from 'vue'

export const sidebarDesktopHost = shallowRef<HTMLElement | null>(null)
export const modalDesktopHost = shallowRef<HTMLElement | null>(null)
