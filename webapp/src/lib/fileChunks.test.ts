import { describe, expect, it, vi } from 'vitest'

import {
  CHUNK_TRANSFER_TIMEOUT_MS,
  FILE_CHUNK_B64_SIZE,
  createChunkedTransferStore,
  decodedBase64Size,
  shouldChunkPayload,
  splitBase64Chunks,
  stripBase64Whitespace,
  type FileChunk,
} from './fileChunks'

function makeChunk(requestId: string, index: number, total: number, content: string): FileChunk {
  return {
    workspace_id: 'ws-1',
    request_id: requestId,
    path: '/workspace/a.txt',
    index,
    total_chunks: total,
    content,
  }
}

describe('stripBase64Whitespace', () => {
  it('removes all whitespace runs', () => {
    expect(stripBase64Whitespace('QUJD\nRkVI\t I g==\r\n')).toBe('QUJDRkVIIg==')
  })

  it('handles empty / whitespace-only input', () => {
    expect(stripBase64Whitespace('')).toBe('')
    expect(stripBase64Whitespace('   \n\t')).toBe('')
  })
})

describe('splitBase64Chunks', () => {
  it('splits so that joining restores the input in order', () => {
    const input = 'ABCDEFGHIJKLMNOP'.repeat(500) // 8000 chars
    const chunks = splitBase64Chunks(input, 1024)
    expect(chunks.length).toBe(Math.ceil(8000 / 1024))
    expect(chunks.join('')).toBe(input)
  })

  it('keeps full chunks 4-char aligned', () => {
    const chunks = splitBase64Chunks('ABCD'.repeat(100), 10)
    expect(chunks.join('')).toBe('ABCD'.repeat(100))
    for (const piece of chunks.slice(0, -1)) {
      expect(piece.length % 4).toBe(0)
    }
  })

  it('strips whitespace before splitting', () => {
    expect(splitBase64Chunks('QUJD\nREVG\tRUZI', 4)).toEqual(['QUJD', 'REVG', 'RUZI'])
  })

  it('returns [] for empty payloads', () => {
    expect(splitBase64Chunks('')).toEqual([])
    expect(splitBase64Chunks('  \n ')).toEqual([])
  })

  it('rejects non-positive or sub-4 chunk sizes', () => {
    expect(() => splitBase64Chunks('QUJD', 0)).toThrow()
    expect(() => splitBase64Chunks('QUJD', -4)).toThrow()
    // size 3 aligns down to 0, which is unusable
    expect(() => splitBase64Chunks('QUJD', 3)).toThrow()
  })
})

describe('shouldChunkPayload', () => {
  it('is false at exactly FILE_CHUNK_B64_SIZE and true one char above', () => {
    expect(shouldChunkPayload('A'.repeat(FILE_CHUNK_B64_SIZE))).toBe(false)
    expect(shouldChunkPayload('A'.repeat(FILE_CHUNK_B64_SIZE + 1))).toBe(true)
  })

  it('ignores whitespace when measuring', () => {
    expect(shouldChunkPayload(`${'A'.repeat(FILE_CHUNK_B64_SIZE)}\n  `)).toBe(false)
  })
})

describe('createChunkedTransferStore', () => {
  it('reassembles out-of-order chunks in index order', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('oo', 3)
    store.addChunk('oo', makeChunk('oo', 2, 3, 'Q0ND'))
    store.addChunk('oo', makeChunk('oo', 0, 3, 'QUFB'))
    store.addChunk('oo', makeChunk('oo', 1, 3, 'QkJC'))
    const { content } = store.finish('oo')
    expect(content).toBe('QUFBQkJCQ0ND')
    expect(store.has('oo')).toBe(false)
  })

  it('fails closed on duplicate chunks (later finish throws unknown)', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('dup', 2)
    store.addChunk('dup', makeChunk('dup', 0, 2, 'QUJD'))
    expect(() => store.addChunk('dup', makeChunk('dup', 0, 2, 'QUJD'))).toThrow(/duplicate/)
    expect(store.has('dup')).toBe(false)
    expect(() => store.finish('dup')).toThrow(/unknown/)
  })

  it('throws incomplete when chunks are missing', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('inc', 2)
    store.addChunk('inc', makeChunk('inc', 0, 2, 'QUJD'))
    expect(() => store.finish('inc')).toThrow(/incomplete/)
    expect(store.has('inc')).toBe(false)
  })

  it('throws mismatch when finish is given the wrong total', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('mm', 2)
    store.addChunk('mm', makeChunk('mm', 0, 2, 'QUJD'))
    store.addChunk('mm', makeChunk('mm', 1, 2, 'REVG'))
    expect(() => store.finish('mm', 3)).toThrow(/mismatch/)
    expect(store.has('mm')).toBe(false)
  })

  it('rejects start with total 0, non-integer, or above maxChunks', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    expect(() => store.start('zero', 0)).toThrow(/invalid/)
    expect(() => store.start('frac', 1.5)).toThrow(/invalid/)
    // default maxChunks is MAX_READ_CHUNKS_PER_TRANSFER (560)
    expect(() => store.start('huge', 561)).toThrow(/invalid/)
    expect(store.has('zero')).toBe(false)
  })

  it('rejects addChunk whose total_chunks differs from the pinned total', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('pin', 2)
    expect(() => store.addChunk('pin', makeChunk('pin', 0, 3, 'QUJD'))).toThrow(/mismatch/)
    expect(store.has('pin')).toBe(false)
  })

  it('purges the transfer when buffered chars exceed maxChars', () => {
    const store = createChunkedTransferStore({ maxChars: 8 })
    store.start('big', 2)
    store.addChunk('big', makeChunk('big', 0, 2, 'QUJD')) // 4 chars buffered
    expect(() => store.addChunk('big', makeChunk('big', 1, 2, 'QUJDREVGRw=='))).toThrow(
      /exceeds size limit/,
    )
    expect(store.has('big')).toBe(false)
  })

  it('fires onTimeout and drops the transfer after CHUNK_TRANSFER_TIMEOUT_MS', () => {
    vi.useFakeTimers()
    try {
      const onTimeout = vi.fn()
      const store = createChunkedTransferStore({ maxChars: 1024, onTimeout })
      store.start('to', 2)
      expect(store.has('to')).toBe(true)
      vi.advanceTimersByTime(CHUNK_TRANSFER_TIMEOUT_MS + 1)
      expect(onTimeout).toHaveBeenCalledWith('to')
      expect(store.has('to')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('accepts start total 560 and rejects 561 with maxChunks 560', () => {
    const store = createChunkedTransferStore({
      maxChars: Number.MAX_SAFE_INTEGER,
      maxChunks: 560,
    })
    expect(() => store.start('ok-560', 560)).not.toThrow()
    expect(store.has('ok-560')).toBe(true)
    expect(() => store.start('bad-561', 561)).toThrow(/invalid/)
    store.clear()
  })

  it('fits the 140MiB webapp read cap within 560 chunks (arithmetic only)', () => {
    const capChars = 140 * 1024 * 1024
    expect(Math.ceil(capChars / FILE_CHUNK_B64_SIZE)).toBeLessThanOrEqual(560)
    expect(FILE_CHUNK_B64_SIZE * 560).toBeGreaterThanOrEqual(capChars)
  })
})

describe('decodedBase64Size', () => {
  it('computes exact sizes padding-aware (incl. empty)', () => {
    expect(decodedBase64Size('')).toBe(0)
    expect(decodedBase64Size('QUJD')).toBe(3) // no padding
    expect(decodedBase64Size('QUI=')).toBe(2) // one pad
    expect(decodedBase64Size('QQ==')).toBe(1) // two pads
    expect(decodedBase64Size(btoa('hello world'))).toBe(11)
  })

  it('rejects malformed base64 instead of mis-sizing', () => {
    expect(() => decodedBase64Size('!!!')).toThrow(/invalid base64/)
    expect(() => decodedBase64Size('ABC')).toThrow(/invalid base64/)
    expect(() => decodedBase64Size('QUJD===')).toThrow(/invalid base64/)
    expect(() => decodedBase64Size('QUJD====')).toThrow(/invalid base64/)
    expect(() => decodedBase64Size('QUJDQUJD===')).toThrow(/invalid base64/)
  })

  it('sizes multi-megabyte payloads without overflowing the regex stack', () => {
    // 3 MiB raw => ~4 MiB base64; the old single-regex shape check blew
    // the call stack on 10 MiB inputs.
    const rawBytes = 3 * 1024 * 1024
    const content = 'AAAA'.repeat(rawBytes / 3)
    expect(content.length).toBeGreaterThanOrEqual(4 * 1024 * 1024)
    expect(decodedBase64Size(content)).toBe(rawBytes)
  })
})

describe('path identity validation', () => {
  it('accepts a first chunk matching the pinned expected path', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('pinned', 2, {}, '/workspace/a.txt')
    expect(() =>
      store.addChunk('pinned', makeChunk('pinned', 0, 2, 'QUJD')),
    ).not.toThrow()
    expect(store.has('pinned')).toBe(true)
    store.clear()
  })

  it('rejects a wrong-path chunk against the pinned expected path', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('pin2', 2, {}, '/workspace/expected.txt')
    const wrong: FileChunk = {
      workspace_id: 'ws-1',
      request_id: 'pin2',
      path: '/workspace/evil.txt',
      index: 0,
      total_chunks: 2,
      content: 'QUJD',
    }
    expect(() => store.addChunk('pin2', wrong)).toThrow(/path mismatch/)
    expect(store.has('pin2')).toBe(false)
  })

  it('exposes the pinned path via getPath', () => {
    const store = createChunkedTransferStore({ maxChars: 1024 })
    store.start('gp', 1, {}, '/workspace/a.txt')
    expect(store.getPath('gp')).toBe('/workspace/a.txt')
    expect(store.getPath('missing')).toBeNull()
    store.clear()
  })
})
