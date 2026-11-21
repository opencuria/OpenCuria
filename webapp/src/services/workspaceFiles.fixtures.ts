/**
 * Test doubles for `@/services/workspaceFiles`. Mock the module with
 * `vi.mock('@/services/workspaceFiles', () => import('@/services/workspaceFiles.fixtures'))`
 * and drive the shared fake through `workspaceFiles()`.
 */
import { vi, type Mocked } from 'vitest'
import type { WorkspaceFileKind, WorkspaceFileLink } from '@opencuria/chat-core/workspaceFiles'
import type { WorkspaceFilesApi } from '@opencuria/client/files'

const KINDS: Record<string, [WorkspaceFileKind, string]> = {
  png: ['image', 'image/png'],
  jpg: ['image', 'image/jpeg'],
  svg: ['image', 'image/svg+xml'],
  mp4: ['video', 'video/mp4'],
  pdf: ['pdf', 'application/pdf'],
  txt: ['text', 'text/plain'],
  md: ['text', 'text/markdown'],
}

/** A signed link as the server returns it (fake absolute URLs). */
export function makeFileLink(
  path: string,
  overrides: Partial<WorkspaceFileLink> = {},
): WorkspaceFileLink {
  const name = path.split('/').pop() || path
  const [kind, mimeType] = KINDS[name.split('.').pop()?.toLowerCase() ?? ''] ?? [
    'binary',
    'application/octet-stream',
  ]
  return {
    path,
    name,
    type: 'file',
    size: 1024,
    modifiedAt: 1_700_000_000,
    kind,
    mimeType,
    contentUrl: `https://files.test/content${path}`,
    downloadUrl: `https://files.test/download${path}`,
    thumbnailUrl: kind === 'image' || kind === 'video' ? `https://files.test/thumb${path}` : '',
    expiresAt: 4_102_444_800,
    ...overrides,
  }
}

function createFake(): Mocked<WorkspaceFilesApi> {
  return {
    link: vi.fn(async (_workspaceId: string, path: string) => makeFileLink(path)),
    peekLink: vi.fn(() => undefined),
    forget: vi.fn(),
    createUpload: vi.fn(),
    upload: vi.fn(
      async (_workspaceId: string, input: { directory: string; filename: string; body: Blob }) => ({
        path: `${input.directory}/${input.filename}`,
        size: input.body.size,
      }),
    ),
    readBytes: vi.fn(async (_workspaceId: string, path: string) => ({
      link: makeFileLink(path),
      bytes: new Uint8Array(),
      truncated: false,
    })),
  } as unknown as Mocked<WorkspaceFilesApi>
}

const fake = createFake()

/** The shared fake; reset it in `beforeEach` with {@link resetWorkspaceFiles}. */
export function workspaceFiles(): Mocked<WorkspaceFilesApi> {
  return fake
}

/** Restore the default fake behavior and clear recorded calls. */
export function resetWorkspaceFiles(): void {
  Object.assign(fake, createFake())
  startDownload.mockReset()
}

export const startDownload = vi.fn()
