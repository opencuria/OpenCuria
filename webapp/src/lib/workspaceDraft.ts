export const WORKSPACE_DRAFT_TTL_MS = 60 * 60 * 1000
const DRAFT_PREFIX = 'opencuria:workspace-draft:'
const ACTIVE_DRAFT_KEY = 'opencuria:workspace-draft-active'

export interface WorkspaceDraftFields {
  mode: 'create' | 'edit'
  name: string
  credentialIds: string[]
  pluginIds: string[]
  workspaceId?: string
  repos?: string[]
  runnerId?: string
  runtimeType?: 'docker' | 'qemu'
  qemuVcpus?: number
  qemuMemoryMb?: number
  qemuDiskSizeGb?: number
  desktopWidth?: number
  desktopHeight?: number
  imageValue?: string
  imageArtifactId?: string
}

export interface WorkspaceDraft {
  version: 1
  id: string
  userId: string
  organizationId: string
  createdAt: number
  returnPath: '/' | '/workspaces' | `/workspaces/${string}`
  fields: WorkspaceDraftFields
}

export type DraftReadResult =
  | { status: 'ok'; draft: WorkspaceDraft }
  | { status: 'missing' | 'invalid' | 'expired' | 'identity-mismatch' | 'storage-error' }

const DRAFT_KEYS = new Set([
  'version',
  'id',
  'userId',
  'organizationId',
  'createdAt',
  'returnPath',
  'fields',
])
const FIELD_KEYS = new Set([
  'mode',
  'name',
  'credentialIds',
  'pluginIds',
  'workspaceId',
  'repos',
  'runnerId',
  'runtimeType',
  'qemuVcpus',
  'qemuMemoryMb',
  'qemuDiskSizeGb',
  'desktopWidth',
  'desktopHeight',
  'imageValue',
  'imageArtifactId',
])
const NUMBER_FIELDS = [
  'qemuVcpus',
  'qemuMemoryMb',
  'qemuDiskSizeGb',
  'desktopWidth',
  'desktopHeight',
] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.length <= 1000 &&
    value.every((entry) => typeof entry === 'string' && entry.length <= 512)
  )
}

function isSafeReturnPath(value: unknown): value is WorkspaceDraft['returnPath'] {
  return (
    value === '/' ||
    value === '/workspaces' ||
    (typeof value === 'string' && /^\/workspaces\/[\w-]+$/.test(value))
  )
}

function isWorkspaceDraftFields(value: unknown): value is WorkspaceDraftFields {
  if (!isRecord(value) || Object.keys(value).some((key) => !FIELD_KEYS.has(key))) return false
  if (
    (value.mode !== 'create' && value.mode !== 'edit') ||
    typeof value.name !== 'string' ||
    value.name.length > 255 ||
    !isStringArray(value.credentialIds) ||
    !isStringArray(value.pluginIds) ||
    new Set(value.credentialIds).size !== value.credentialIds.length ||
    new Set(value.pluginIds).size !== value.pluginIds.length
  )
    return false
  if (value.mode === 'edit' && (typeof value.workspaceId !== 'string' || !value.workspaceId))
    return false
  if (value.workspaceId !== undefined && typeof value.workspaceId !== 'string') return false
  if (value.repos !== undefined && (!isStringArray(value.repos) || value.repos.length > 100))
    return false
  if (value.runnerId !== undefined && typeof value.runnerId !== 'string') return false
  if (
    value.runtimeType !== undefined &&
    value.runtimeType !== 'docker' &&
    value.runtimeType !== 'qemu'
  )
    return false
  if (
    value.imageValue !== undefined &&
    (typeof value.imageValue !== 'string' ||
      value.imageValue.length > 300 ||
      !/^(captured|definition):[\w-]+$/.test(value.imageValue))
  )
    return false
  if (value.imageArtifactId !== undefined && typeof value.imageArtifactId !== 'string') return false
  for (const key of NUMBER_FIELDS) {
    const number = value[key]
    if (number !== undefined && (typeof number !== 'number' || !Number.isFinite(number)))
      return false
  }
  const qemuVcpus = value.qemuVcpus
  const qemuMemoryMb = value.qemuMemoryMb
  const qemuDiskSizeGb = value.qemuDiskSizeGb
  const desktopWidth = value.desktopWidth
  const desktopHeight = value.desktopHeight
  if (typeof qemuVcpus === 'number' && (qemuVcpus < 1 || qemuVcpus > 256)) return false
  if (typeof qemuMemoryMb === 'number' && (qemuMemoryMb < 512 || qemuMemoryMb > 1_048_576))
    return false
  if (typeof qemuDiskSizeGb === 'number' && (qemuDiskSizeGb < 10 || qemuDiskSizeGb > 10_000))
    return false
  if (
    typeof desktopWidth === 'number' &&
    (desktopWidth < 800 || desktopWidth > 3840 || desktopWidth % 2 !== 0)
  )
    return false
  if (
    typeof desktopHeight === 'number' &&
    (desktopHeight < 600 || desktopHeight > 2160 || desktopHeight % 2 !== 0)
  )
    return false
  return true
}

function createSecureDraftId(): string | null {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
    if (!globalThis.crypto?.getRandomValues) return null
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6]! & 0x0f) | 0x40
    bytes[8] = (bytes[8]! & 0x3f) | 0x80
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  } catch {
    return null
  }
}

function isDraft(value: unknown): value is WorkspaceDraft {
  if (!isRecord(value) || Object.keys(value).some((key) => !DRAFT_KEYS.has(key))) return false
  return (
    value.version === 1 &&
    typeof value.id === 'string' &&
    typeof value.userId === 'string' &&
    typeof value.organizationId === 'string' &&
    typeof value.createdAt === 'number' &&
    Number.isFinite(value.createdAt) &&
    isSafeReturnPath(value.returnPath) &&
    isWorkspaceDraftFields(value.fields) &&
    (value.fields.mode !== 'edit' || value.returnPath === `/workspaces/${value.fields.workspaceId}`)
  )
}

/** Persist only validated configuration metadata (never credential values or OAuth tokens). */
export function saveWorkspaceDraft(
  fields: WorkspaceDraftFields,
  identity: { userId: string | number; organizationId: string },
  returnPath: string,
): { id: string; error?: string } {
  if (!isWorkspaceDraftFields(fields))
    return { id: '', error: 'Workspace draft contains invalid configuration fields.' }
  if (
    !isSafeReturnPath(returnPath) ||
    (fields.mode === 'edit' && returnPath !== `/workspaces/${fields.workspaceId}`)
  ) {
    return { id: '', error: 'Workspace return route is not supported.' }
  }
  if (!String(identity.userId).trim() || !identity.organizationId) {
    return { id: '', error: 'Sign in and choose an organization before saving a workspace draft.' }
  }
  const id = createSecureDraftId()
  if (!id) return { id: '', error: 'Secure workspace draft IDs are unavailable in this browser.' }
  const safeFields: WorkspaceDraftFields = {
    ...fields,
    credentialIds: [...fields.credentialIds],
    pluginIds: [...fields.pluginIds],
    ...(fields.repos ? { repos: [...fields.repos] } : {}),
  }
  const draft: WorkspaceDraft = {
    version: 1,
    id,
    userId: String(identity.userId),
    organizationId: identity.organizationId,
    createdAt: Date.now(),
    returnPath,
    fields: safeFields,
  }
  try {
    sessionStorage.setItem(`${DRAFT_PREFIX}${id}`, JSON.stringify(draft))
    sessionStorage.setItem(ACTIVE_DRAFT_KEY, id)
    return { id }
  } catch {
    try {
      sessionStorage.removeItem(`${DRAFT_PREFIX}${id}`)
      if (sessionStorage.getItem(ACTIVE_DRAFT_KEY) === id)
        sessionStorage.removeItem(ACTIVE_DRAFT_KEY)
    } catch {
      // Storage may be unavailable; no further cleanup is possible.
    }
    return {
      id: '',
      error:
        'Workspace draft could not be saved in this browser session. Please keep this dialog open and try again.',
    }
  }
}

export function readWorkspaceDraft(
  id: string | null | undefined,
  identity: {
    userId: string | number | null | undefined
    organizationId: string | null | undefined
  },
): DraftReadResult {
  if (!id) return { status: 'missing' }
  let raw: string | null
  try {
    raw = sessionStorage.getItem(`${DRAFT_PREFIX}${id}`)
  } catch {
    return { status: 'storage-error' }
  }
  if (!raw) return { status: 'missing' }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    removeWorkspaceDraft(id)
    return { status: 'invalid' }
  }
  if (!isDraft(parsed) || parsed.id !== id) {
    removeWorkspaceDraft(id)
    return { status: 'invalid' }
  }
  if (Date.now() - parsed.createdAt > WORKSPACE_DRAFT_TTL_MS || parsed.createdAt > Date.now()) {
    removeWorkspaceDraft(id)
    return { status: 'expired' }
  }
  if (
    String(identity.userId ?? '') !== parsed.userId ||
    identity.organizationId !== parsed.organizationId
  ) {
    removeWorkspaceDraft(id)
    return { status: 'identity-mismatch' }
  }
  return { status: 'ok', draft: parsed }
}

export function getActiveWorkspaceDraftId(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_DRAFT_KEY)
  } catch {
    return null
  }
}

export function clearWorkspaceDrafts(): void {
  try {
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = sessionStorage.key(index)
      if (key?.startsWith(DRAFT_PREFIX) || key === ACTIVE_DRAFT_KEY) sessionStorage.removeItem(key)
    }
  } catch {
    // Storage may be unavailable; there is nothing else to clear.
  }
}

export function removeWorkspaceDraft(id: string): void {
  try {
    sessionStorage.removeItem(`${DRAFT_PREFIX}${id}`)
    if (sessionStorage.getItem(ACTIVE_DRAFT_KEY) === id) sessionStorage.removeItem(ACTIVE_DRAFT_KEY)
  } catch {
    // Best-effort cleanup; draft data remains metadata-only.
  }
}
