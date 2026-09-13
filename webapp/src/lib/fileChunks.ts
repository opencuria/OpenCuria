/**
 * Chunked base64 file transport helpers.
 *
 * Daphne's default inbound message/frame cap is 1 MiB (oversize frames were
 * dropped as disconnects), so no single file event may carry a large base64
 * payload. Payloads above FILE_CHUNK_B64_SIZE ride as ordered 256 KiB base64
 * chunk events plus a small metadata-only final result (`chunked: true`,
 * `total_chunks: N`, empty content). Small payloads stay inline for backward
 * compatibility. Engine.IO keeps its separate 200 MiB HTTP buffer for
 * pre-existing monolithic non-chunk events.
 *
 * Must stay in sync with the runner (`runner/src/chunking.py`
 * `CHUNK_B64_SIZE`) and backend
 * (`backend/apps/harness/access/runner_accessor.py`
 * `HARNESS_CHUNK_B64_SIZE`) copies.
 */

/** Base64 characters per chunk event — far below Daphne's default 1 MiB inbound cap. */
export const FILE_CHUNK_B64_SIZE = 256 * 1024

/**
 * Upper bound for read/download transfers. 100 MiB raw encodes to ~139.8 M
 * base64 chars, i.e. ~534 chunks at 256 KiB; 560 leaves padding headroom.
 */
export const MAX_READ_CHUNKS_PER_TRANSFER = 560

/** Upper bound for upload/write transfers (10 MiB raw ≈ 52 chunks). */
export const MAX_UPLOAD_CHUNKS_PER_TRANSFER = 64

/** Max raw bytes for one upload/write payload (matches runner cap). */
export const UPLOAD_MAX_BYTES = 10 * 1024 * 1024

/** Backward-compatible alias for the upload/write chunk cap. */
export const MAX_CHUNKS_PER_TRANSFER = MAX_UPLOAD_CHUNKS_PER_TRANSFER

/** How long a partial chunked transfer waits for completion before failing. */
export const CHUNK_TRANSFER_TIMEOUT_MS = 30_000

export interface FileChunk {
  workspace_id: string
  request_id: string
  path: string
  index: number
  total_chunks: number
  content: string
}

export interface ChunkedTransferMeta {
  size?: number
  truncated?: boolean
  mime_type?: string
  filename?: string
  is_archive?: boolean
  /** Announced total echoed back for exact final-count validation. */
  totalChunks?: number
}

export interface ChunkedTransferState {
  totalChunks: number
  chunks: Map<number, string>
  bufferedChars: number
  createdAt: number
  meta: ChunkedTransferMeta
  path: string
  timer: ReturnType<typeof setTimeout> | null
}

/**
 * Exact decoded byte count for whitespace-free base64 (padding-aware).
 * `atob` throws on malformed input, so validate shape + decode here
 * before allocating bytes/Blob/URL — every caller fails closed.
 *
 * The shape check is length-sliced (not one giant alternation) so even
 * 10 MiB payloads validate without blowing the regex stack.
 */
export function decodedBase64Size(clean: string): number {
  if (clean.length === 0) return 0
  if (clean.length % 4 !== 0) {
    throw new Error('invalid base64 payload')
  }
  const body = clean.length > 4 ? clean.slice(0, -4) : ''
  const tail = clean.length > 4 ? clean.slice(-4) : clean
  // Long inputs are checked in slices so a single huge regex can never
  // overflow the stack; short inputs take the same code path.
  for (let i = 0; i < body.length; i += 4096) {
    if (!/^[A-Za-z0-9+/]*$/.test(body.slice(i, i + 4096))) {
      throw new Error('invalid base64 payload')
    }
  }
  if (!/^(?:[A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)$/.test(tail)) {
    throw new Error('invalid base64 payload')
  }
  let padding = 0
  if (clean.endsWith('==')) padding = 2
  else if (clean.endsWith('=')) padding = 1
  return Math.floor((clean.length * 3) / 4) - padding
}

export function stripBase64Whitespace(payload: string): string {
  return (payload ?? '').replace(/\s+/g, '')
}

export function splitBase64Chunks(payload: string, size: number = FILE_CHUNK_B64_SIZE): string[] {
  if (!Number.isFinite(size) || size <= 0) {
    throw new Error('chunk size must be positive')
  }
  const aligned = size - (size % 4)
  if (aligned <= 0) {
    throw new Error('chunk size must be at least 4')
  }
  const clean = stripBase64Whitespace(payload)
  if (!clean) return []
  const out: string[] = []
  for (let i = 0; i < clean.length; i += aligned) {
    out.push(clean.slice(i, i + aligned))
  }
  return out
}

export function shouldChunkPayload(payload: string, limit: number = FILE_CHUNK_B64_SIZE): boolean {
  return stripBase64Whitespace(payload).length > limit
}

/**
 * Create bounded reassembly state for chunked file transfers, keyed by
 * request_id. Invalid, oversized, incomplete, or expired transfers fail
 * closed and are purged so partial state never grows without bounds.
 */
export function createChunkedTransferStore(options: {
  maxChars: number
  maxEntries?: number
  maxChunks?: number
  onTimeout?: (requestId: string) => void
}) {
  const maxEntries = options.maxEntries ?? 32
  const maxChunks = options.maxChunks ?? MAX_READ_CHUNKS_PER_TRANSFER
  const store = new Map<string, ChunkedTransferState>()

  function fail(requestId: string): void {
    const entry = store.get(requestId)
    if (entry?.timer) clearTimeout(entry.timer)
    store.delete(requestId)
  }

  function start(
    requestId: string,
    totalChunks: number,
    meta: ChunkedTransferMeta = {},
    path = '',
  ): void {
    const total = Number(totalChunks)
    if (!requestId || !Number.isInteger(total) || total <= 0 || total > maxChunks) {
      throw new Error(`invalid total_chunks: ${String(totalChunks)}`)
    }
    if (store.has(requestId)) {
      throw new Error(`transfer already in progress: ${requestId}`)
    }
    if (store.size >= maxEntries) {
      throw new Error('too many concurrent chunked transfers')
    }
    const timer = setTimeout(() => {
      fail(requestId)
      options.onTimeout?.(requestId)
    }, CHUNK_TRANSFER_TIMEOUT_MS)
    // Defensive: a leaked store timer must never keep the process alive.
    if (typeof (timer as unknown as { unref?: () => void }).unref === 'function') {
      ;(timer as unknown as { unref: () => void }).unref()
    }
    store.set(requestId, {
      totalChunks: total,
      chunks: new Map(),
      bufferedChars: 0,
      createdAt: Date.now(),
      meta,
      path,
      timer,
    })
  }

  function addChunk(requestId: string, chunk: FileChunk): void {
    const entry = store.get(requestId)
    if (!entry) {
      throw new Error(`unknown transfer: ${requestId}`)
    }
    // Identity validation: a chunk for the wrong path fails closed here,
    // so a first wrong-path chunk can never become ground truth even if
    // the caller started the transfer with the chunk's own path.
    if (entry.path && chunk.path && entry.path !== chunk.path) {
      fail(requestId)
      throw new Error(`path mismatch for ${requestId}`)
    }
    if (!Number.isInteger(chunk.total_chunks) || chunk.total_chunks !== entry.totalChunks) {
      fail(requestId)
      throw new Error(`total_chunks mismatch for ${requestId}`)
    }
    if (!Number.isInteger(chunk.index) || chunk.index < 0 || chunk.index >= entry.totalChunks) {
      fail(requestId)
      throw new Error(`chunk index out of range for ${requestId}`)
    }
    if (entry.chunks.has(chunk.index)) {
      fail(requestId)
      throw new Error(`duplicate chunk ${chunk.index} for ${requestId}`)
    }
    const clean = stripBase64Whitespace(chunk.content ?? '')
    if (!clean) {
      fail(requestId)
      throw new Error(`empty chunk ${chunk.index} for ${requestId}`)
    }
    if (clean.length > FILE_CHUNK_B64_SIZE) {
      fail(requestId)
      throw new Error(`chunk ${chunk.index} too large for ${requestId}`)
    }
    if (entry.bufferedChars + clean.length > options.maxChars) {
      fail(requestId)
      throw new Error(`transfer ${requestId} exceeds size limit`)
    }
    entry.chunks.set(chunk.index, clean)
    entry.bufferedChars += clean.length
  }

  function finish(
    requestId: string,
    expectedTotalChunks?: number,
  ): { content: string; meta: ChunkedTransferMeta; path: string } {
    const entry = store.get(requestId)
    if (!entry) {
      throw new Error(`unknown transfer: ${requestId}`)
    }
    if (
      expectedTotalChunks !== undefined &&
      Number(expectedTotalChunks) !== entry.totalChunks
    ) {
      fail(requestId)
      throw new Error(`total_chunks mismatch for ${requestId}`)
    }
    const missing: number[] = []
    for (let i = 0; i < entry.totalChunks; i++) {
      if (!entry.chunks.has(i)) missing.push(i)
    }
    if (missing.length > 0) {
      fail(requestId)
      throw new Error(
        `incomplete transfer ${requestId}: missing ${missing.length} of ${entry.totalChunks} chunks`,
      )
    }
    const parts: string[] = []
    for (let i = 0; i < entry.totalChunks; i++) {
      parts.push(entry.chunks.get(i)!)
    }
    const meta = entry.meta
    const path = entry.path
    fail(requestId)
    return { content: parts.join(''), meta, path }
  }

  function cancel(requestId: string): void {
    fail(requestId)
  }

  function getPath(requestId: string): string | null {
    return store.get(requestId)?.path ?? null
  }

  function has(requestId: string): boolean {
    return store.has(requestId)
  }

  function clear(): void {
    for (const requestId of [...store.keys()]) fail(requestId)
  }

  return { start, addChunk, finish, cancel, has, getPath, clear }
}

export type ChunkedTransferStore = ReturnType<typeof createChunkedTransferStore>
