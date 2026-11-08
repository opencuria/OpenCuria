/**
 * Vue binding of the shared desktop viewer coordinator
 * (`@opencuria/chat-core/desktopViewer`). Sessions are wrapped in `reactive`
 * so components and stores watch them directly; lease rules live in the
 * shared package and are identical on mobile.
 */
import { reactive } from 'vue'
import {
  createDesktopViewerCoordinator,
  createViewerClientId,
  type ViewerSession,
} from '@opencuria/chat-core/desktopViewer'
import * as api from '@/services/workspaces.api'

// Deliberately not persisted: another browser tab must never inherit this intent.
export const desktopViewerClientId = createViewerClientId()

const coordinator = createDesktopViewerCoordinator({
  viewerClientId: desktopViewerClientId,
  track: (session) => reactive(session) as ViewerSession,
  // Resolve the REST functions per call so module mocks and spies apply.
  transport: {
    start: (workspaceId, intent, signal) => api.startDesktop(workspaceId, intent, signal),
    stop: (workspaceId, intent) => api.stopDesktop(workspaceId, intent),
    renew: (workspaceId, intent, signal) => api.renewDesktop(workspaceId, intent, signal),
    status: (workspaceId, viewerClientId, signal) =>
      api.getDesktopStatus(workspaceId, viewerClientId, signal),
  },
})

export const desktopViewerSession = coordinator.session
export const refreshDesktopViewer = coordinator.refresh
export const retainDesktopViewer = coordinator.retain
export const acquireDesktopViewer = coordinator.acquire
export const closeDesktopViewer = coordinator.close
export const releaseDesktopViewer = coordinator.release
export const reconnectDesktopViewer = coordinator.reconnect
