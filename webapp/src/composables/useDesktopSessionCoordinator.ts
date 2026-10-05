import { reactive } from 'vue'
import * as api from '@/services/workspaces.api'

const UUID_RE = /^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/

function uuidFromHex(hex: string): string {
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}

/** RFC 4122 UUID. Safari and non-secure pages omit crypto.randomUUID. */
export function createViewerClientId(): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.()
    if (typeof uuid === 'string' && UUID_RE.test(uuid)) return uuid
  } catch {
    // randomUUID throws in some non-secure contexts; use getRandomValues.
  }
  try {
    const bytes = globalThis.crypto?.getRandomValues?.(new Uint8Array(16))
    if (bytes?.length === 16) {
      bytes[6] = (bytes[6]! & 0x0f) | 0x40
      bytes[8] = (bytes[8]! & 0x3f) | 0x80
      const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
      return uuidFromHex(hex)
    }
  } catch {
    // Fall through so module init never throws.
  }
  const nibbles = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16))
  nibbles[12] = '4'
  nibbles[16] = ((Number.parseInt(nibbles[16]!, 16) & 0x3) | 0x8).toString(16)
  return uuidFromHex(nibbles.join(''))
}

// Deliberately not persisted: another browser tab must never inherit this intent.
export const desktopViewerClientId = createViewerClientId()
const RENEW_MS = 45_000
const POLL_MS = 1_000
const STARTUP_MS = 30_000
const MAX_START_ATTEMPTS = 3
/** Navigation unmounts a surface before its replacement retains the intent. */
export const RELEASE_GRACE_MS = 1_500

interface DeferredRelease {
  timer: ReturnType<typeof setTimeout>
  resolve: (released: boolean) => void
}

export interface ViewerSession {
  revision: number
  epoch: string | null
  leaseState: string
  wanted: boolean
  connecting: boolean
  error: string | null
  status: api.DesktopStatus | null
  owners: Set<object>
  pending: Promise<void> | null
  observation: number
  startupDeadline: number
  startAttempts: number
  timer: ReturnType<typeof setTimeout> | null
  release: DeferredRelease | null
}
const sessions = new Map<string, ViewerSession>()

export function desktopViewerSession(workspaceId: string): ViewerSession {
  let session = sessions.get(workspaceId)
  if (!session) {
    session = reactive<ViewerSession>({
      revision: 0,
      epoch: null,
      leaseState: 'unknown',
      wanted: false,
      connecting: false,
      error: null,
      status: null,
      owners: new Set(),
      pending: null,
      timer: null,
      observation: 0,
      startupDeadline: 0,
      startAttempts: 0,
      release: null,
    })
    sessions.set(workspaceId, session)
  }
  return session
}

function intent(session: ViewerSession): api.DesktopViewerIntent {
  return { viewer_client_id: desktopViewerClientId, intent_revision: session.revision }
}

function cancelTimer(session: ViewerSession): void {
  if (session.timer !== null) clearTimeout(session.timer)
  session.timer = null
}

function current(session: ViewerSession, revision: number): boolean {
  return session.wanted && session.owners.size > 0 && session.revision === revision
}

function schedule(workspaceId: string, session: ViewerSession, delay: number): void {
  cancelTimer(session)
  if (!current(session, session.revision)) return
  session.timer = setTimeout(() => {
    session.timer = null
    void refreshDesktopViewer(workspaceId, true)
  }, delay)
}

/** Query own lease, never infer ownership from globally active desktop events. */
export async function refreshDesktopViewer(workspaceId: string, renew = false): Promise<void> {
  const session = desktopViewerSession(workspaceId)
  const revision = session.revision
  const wanted = session.wanted
  const observation = ++session.observation
  const isLatest = () =>
    session.observation === observation &&
    session.revision === revision &&
    session.wanted === wanted
  try {
    if (renew && current(session, revision) && session.leaseState === 'held') {
      // Renew is only allowed for a confirmed existing intent. Failed renewal
      // is followed by status, not by an implicit start.
      await api.renewDesktop(workspaceId, intent(session)).catch(() => undefined)
      if (!isLatest()) return
    }
    const status = await api.getDesktopStatus(workspaceId, desktopViewerClientId)
    if (!isLatest()) return
    session.status = status
    const remoteRevision = status.revision ?? status.intent_revision
    const state = status.viewer_lease_state ?? 'unknown'
    const epochChanged = Boolean(session.epoch && status.epoch && session.epoch !== status.epoch)
    session.epoch = status.epoch ?? null
    if (remoteRevision !== undefined && remoteRevision > session.revision) {
      session.revision = remoteRevision
    }
    // A global active/viewer_held summary is explicitly not our membership.
    session.leaseState = remoteRevision === revision ? state : 'unknown'
    if (!current(session, session.revision)) return
    const terminal = ['expired', 'released'].includes(state)
    const olderEnded = remoteRevision !== undefined && remoteRevision < revision && terminal
    // A 202 start is only dispatch acknowledgement. It may have been rejected
    // while the previous revision was closing. Retry only after its terminal
    // tombstone is conclusively observed, with a newer revision and a bound.
    if (session.startupDeadline && Date.now() >= session.startupDeadline) {
      failStartup(session)
      return
    }
    if (epochChanged || olderEnded || ['expired', 'released'].includes(session.leaseState)) {
      session.pending = null
      session.leaseState = 'unknown'
      await acquireDesktopViewer(workspaceId, true)
      return
    }
    session.connecting = session.leaseState !== 'held'
    if (!session.connecting) {
      session.startupDeadline = 0
      session.startAttempts = 0
    }
    session.error = null
    schedule(workspaceId, session, session.connecting ? POLL_MS : RENEW_MS)
  } catch (error) {
    if (!isLatest() || !current(session, revision)) return
    if (session.startupDeadline && Date.now() >= session.startupDeadline) {
      failStartup(session)
      return
    }
    session.error = error instanceof Error ? error.message : String(error)
    // Outages must not resurrect or claim membership. Retry observation only.
    schedule(workspaceId, session, RENEW_MS)
  }
}

function cancelDeferredRelease(session: ViewerSession): void {
  const release = session.release
  if (!release) return
  session.release = null
  clearTimeout(release.timer)
  release.resolve(true)
}

export function retainDesktopViewer(workspaceId: string, owner: object): void {
  const session = desktopViewerSession(workspaceId)
  cancelDeferredRelease(session)
  session.owners.add(owner)
}

function failStartup(session: ViewerSession): void {
  cancelTimer(session)
  session.connecting = false
  session.error = 'Desktop startup could not be confirmed. Close and try again.'
}

export function acquireDesktopViewer(workspaceId: string, recovery = false): Promise<void> {
  const session = desktopViewerSession(workspaceId)
  if (session.owners.size === 0) return Promise.resolve()
  if (session.pending) return session.pending
  if (session.wanted && session.leaseState === 'held') return Promise.resolve()
  cancelTimer(session)
  if (!recovery || !session.startupDeadline) {
    session.startupDeadline = Date.now() + STARTUP_MS
    session.startAttempts = 0
  }
  if (session.startAttempts >= MAX_START_ATTEMPTS || Date.now() >= session.startupDeadline) {
    failStartup(session)
    return Promise.resolve()
  }
  session.startAttempts += 1
  session.observation += 1
  session.revision += 1
  const revision = session.revision
  session.wanted = true
  session.connecting = true
  session.leaseState = 'unknown'
  session.error = null
  const payload = intent(session)
  const request = (async () => {
    try {
      await api.startDesktop(workspaceId, payload)
      if (!current(session, revision)) return
      await refreshDesktopViewer(workspaceId)
    } catch (error) {
      if (!current(session, revision)) return
      session.error = error instanceof Error ? error.message : String(error)
      session.connecting = false
      // The POST may have reached the runner. Preserve intent until explicit
      // release, and observe rather than sending another start after outage.
      schedule(workspaceId, session, RENEW_MS)
    } finally {
      if (session.revision === revision) session.pending = null
    }
  })()
  session.pending = request
  return request
}

/** Final release immediately sends a tombstone, even while start is in flight. */
export async function closeDesktopViewer(workspaceId: string): Promise<boolean> {
  const session = desktopViewerSession(workspaceId)
  cancelDeferredRelease(session)
  if (!session.wanted) return true
  const payload = intent(session)
  session.wanted = false
  session.status = null
  session.connecting = false
  session.leaseState = 'released'
  session.pending = null
  cancelTimer(session)
  try {
    await api.stopDesktop(workspaceId, payload)
    if (session.revision === payload.intent_revision && !session.wanted) {
      await refreshDesktopViewer(workspaceId)
    }
    return true
  } catch (error) {
    if (session.revision !== payload.intent_revision || session.wanted) return false
    session.error = error instanceof Error ? error.message : String(error)
    // Retry the same tombstone during outages, never a newer intent.
    session.timer = setTimeout(() => {
      session.timer = null
      if (session.revision !== payload.intent_revision || session.wanted) return
      session.wanted = true
      void closeDesktopViewer(workspaceId)
    }, RENEW_MS)
    return false
  }
}

/** Last surface gone: release after a grace period unless a surface re-retains. */
export function releaseDesktopViewer(workspaceId: string, owner: object): Promise<boolean> {
  const session = desktopViewerSession(workspaceId)
  session.owners.delete(owner)
  if (session.owners.size > 0 || !session.wanted) return Promise.resolve(true)
  cancelDeferredRelease(session)
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      session.release = null
      if (session.owners.size > 0) return resolve(true)
      void closeDesktopViewer(workspaceId).then(resolve)
    }, RELEASE_GRACE_MS)
    session.release = { timer, resolve }
  })
}
