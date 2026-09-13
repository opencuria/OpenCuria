import { describe, expect, it } from 'vitest'

import {
  FILE_CHUNK_B64_SIZE,
  MAX_UPLOAD_CHUNKS_PER_TRANSFER,
  UPLOAD_MAX_BYTES,
} from '@/lib/fileChunks'
import { planFilesUpload } from '@/services/socket'

const WS_ID = 'ws-1'
const PATH = '/workspace'
const NAME = 'a.bin'

/** Valid base64 with exact raw size (all-zero bytes => 'AAAA...' blocks). */
function base64ForRawBytes(rawBytes: number): string {
  const fullQuads = Math.floor(rawBytes / 3)
  const rest = rawBytes % 3
  let out = 'AAAA'.repeat(fullQuads)
  if (rest === 1) out += 'AA=='
  else if (rest === 2) out += 'AAA='
  return out
}

describe('planFilesUpload', () => {
  it('plans a small inline payload as one files_upload event', () => {
    const content = btoa('hello world')
    const planned = planFilesUpload(WS_ID, 'req-small', PATH, NAME, content)

    expect(planned).toHaveLength(1)
    expect(planned[0]!.event).toBe('frontend:files_upload')
    expect(planned[0]!.payload).toMatchObject({
      workspace_id: WS_ID,
      request_id: 'req-small',
      path: PATH,
      filename: NAME,
      content,
      is_directory: false,
    })
  })

  it('plans a chunked upload as start + N ordered chunks + finish', () => {
    // 300 KiB raw encodes to ~410 KiB base64 => 2 chunks at 256 KiB.
    const content = base64ForRawBytes(300 * 1024)
    expect(content.length).toBeGreaterThan(FILE_CHUNK_B64_SIZE)
    const planned = planFilesUpload(WS_ID, 'req-big', PATH, NAME, content)

    expect(planned[0]!.event).toBe('frontend:files_upload_start')
    expect(planned[planned.length - 1]!.event).toBe('frontend:files_upload_finish')

    const chunks = planned.filter((p) => p.event === 'frontend:files_upload_chunk')
    const totalChunks = chunks.length
    expect(totalChunks).toBeGreaterThanOrEqual(2)
    // Start + chunks + finish.
    expect(planned).toHaveLength(totalChunks + 2)
    expect(planned[0]!.payload).toMatchObject({ total_chunks: totalChunks })

    expect(chunks.map((c) => c.payload['index'])).toEqual(
      Array.from({ length: totalChunks }, (_, i) => i),
    )
    for (const chunk of chunks) {
      expect(chunk.payload['total_chunks']).toBe(totalChunks)
      expect((chunk.payload['content'] as string).length).toBeLessThanOrEqual(
        FILE_CHUNK_B64_SIZE,
      )
    }
    // Payload pieces concatenate back to the clean payload, in order.
    expect(chunks.map((c) => c.payload['content'] as string).join('')).toBe(content)
  })

  it('normalizes whitespace into the inline payload without extra events', () => {
    const planned = planFilesUpload(WS_ID, 'req-ws', PATH, NAME, 'aGVs\nbG8=  ')

    expect(planned).toHaveLength(1)
    expect(planned[0]!.payload['content']).toBe('aGVsbG8=')
  })

  it('allows exactly 10 MiB raw (max 64 chunks) without throwing', () => {
    const content = base64ForRawBytes(UPLOAD_MAX_BYTES)
    const planned = planFilesUpload(WS_ID, 'req-exact', PATH, NAME, content)

    const chunks = planned.filter((p) => p.event === 'frontend:files_upload_chunk')
    expect(chunks.length).toBeLessThanOrEqual(MAX_UPLOAD_CHUNKS_PER_TRANSFER)
    expect(chunks.length).toBeGreaterThan(1)
    expect(chunks.map((c) => c.payload['content'] as string).join('')).toBe(content)
  })

  it('throws before planning any event for 10 MiB + 1 raw byte', () => {
    expect(() => planFilesUpload(WS_ID, 'req-over', PATH, NAME, base64ForRawBytes(UPLOAD_MAX_BYTES + 1))).toThrow(
      /exceeds.*10 MB/,
    )
  })

  it('throws before planning any event for invalid base64', () => {
    expect(() => planFilesUpload(WS_ID, 'req-bad', PATH, NAME, '!!!not-base64!!!')).toThrow(
      /invalid base64/,
    )
  })
})
