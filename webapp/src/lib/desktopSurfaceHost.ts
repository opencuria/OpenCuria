/**
 * Host element registry for the persistent desktop surface.
 *
 * SidePanelDesktop and the WorkspaceDesktop modal register their host
 * containers here via callback refs. DesktopSurface keeps its iframe at one
 * stable fixed position and aligns that placement with the active host; it
 * never moves the iframe browsing context between these containers.
 */
import { shallowRef } from 'vue'

export const sidebarDesktopHost = shallowRef<HTMLElement | null>(null)
export const modalDesktopHost = shallowRef<HTMLElement | null>(null)
