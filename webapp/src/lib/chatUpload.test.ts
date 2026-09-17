import { describe, expect, it, vi, afterEach } from 'vitest'

import {
  appendUploadMentions,
  buildUploadMentionToken,
  CHAT_UPLOAD_DIR,
  collectUploadDirFilenames,
  fileToBase64,
  getDroppedFiles,
  hasTreePath,
  hasUploadMention,
  isFileDrag,
  nextChatUploadRequestId,
  resolveUniqueFilename,
  sanitizeUploadFilename,
  UPLOAD_MAX_BYTES,
  uploadTargetPath,
} from './chatUpload'
import type { FileNode } from '@/types'

function makeNode(overrides: Partial<FileNode> & { name: string; path: string }): FileNode {
  return { type: 'file', size: 1, ...overrides }
}

describe('chatUpload', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('targets /workspace/.opencuria/user-uploaded', () => {
    expect(CHAT_UPLOAD_DIR).toBe('/workspace/.opencuria/user-uploaded')
    expect(uploadTargetPath('a.txt')).toBe('/workspace/.opencuria/user-uploaded/a.txt')
  })

  it('sanitizes filenames like the historical chat upload', () => {
    expect(sanitizeUploadFilename('my file (1).png')).toBe('my_file__1_.png')
    expect(sanitizeUploadFilename('a/b.txt')).toBe('a_b.txt')
    expect(sanitizeUploadFilename('')).toBe('upload')
    expect(sanitizeUploadFilename('.')).toBe('upload')
    expect(sanitizeUploadFilename('..')).toBe('upload')
    expect(sanitizeUploadFilename('  notes.md  ')).toBe('notes.md')
  })

  it('deduplicates colliding names with a _1/_2 suffix without overwriting', () => {
    const taken = new Set(['report.pdf'])
    expect(resolveUniqueFilename(taken, 'report.pdf')).toBe('report_1.pdf')
    // The set is updated, so the next collision keeps counting.
    expect(resolveUniqueFilename(taken, 'report.pdf')).toBe('report_2.pdf')
    expect(resolveUniqueFilename(taken, 'fresh.txt')).toBe('fresh.txt')
  })

  it('deduplicates names without extension and dotfiles', () => {
    const taken = new Set(['LICENSE', '.gitignore'])
    expect(resolveUniqueFilename(taken, 'LICENSE')).toBe('LICENSE_1')
    expect(resolveUniqueFilename(taken, '.gitignore')).toBe('.gitignore_1')
  })

  it('builds @file: mention tokens with trailing space like the mention picker', () => {
    expect(buildUploadMentionToken('/workspace/.opencuria/user-uploaded/a.txt')).toBe(
      '@file:/workspace/.opencuria/user-uploaded/a.txt',
    )
    expect(appendUploadMentions('', ['/workspace/a.txt'])).toBe('@file:/workspace/a.txt ')
    expect(appendUploadMentions('hello', ['/workspace/a.txt', '/workspace/b.txt'])).toBe(
      'hello\n@file:/workspace/a.txt @file:/workspace/b.txt ',
    )
    expect(appendUploadMentions('hello', [])).toBe('hello')
  })

  it('never inserts the same @file: reference twice', () => {
    expect(hasUploadMention('@file:/workspace/a.txt ', '/workspace/a.txt')).toBe(true)
    expect(hasUploadMention('see @file:/workspace/a.txt', '/workspace/a.txt')).toBe(true)
    expect(hasUploadMention('@file:/workspace/a.txt', '/workspace/a.txt')).toBe(true)
    // Prefix paths must not count as a match.
    expect(hasUploadMention('@file:/workspace/a.txt.bak ', '/workspace/a.txt')).toBe(false)
    expect(hasUploadMention('', '/workspace/a.txt')).toBe(false)
    const once = appendUploadMentions('', ['/workspace/a.txt'])
    expect(appendUploadMentions(once, ['/workspace/a.txt'])).toBe(once)
    expect(appendUploadMentions(once, ['/workspace/a.txt', '/workspace/b.txt'])).toBe(
      `${once}\n@file:/workspace/b.txt `,
    )
  })

  it('uses crypto UUID request ids when available', () => {
    const spy = vi
      .spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValue('11111111-2222-4333-8444-555555555555')
    try {
      expect(nextChatUploadRequestId('a.txt')).toBe('upload-11111111-2222-4333-8444-555555555555')
    } finally {
      spy.mockRestore()
    }
  })

  it('falls back to unique timestamp+counter request ids', () => {
    const cryptoObj = globalThis.crypto as unknown as { randomUUID?: () => string }
    const original = cryptoObj.randomUUID
    cryptoObj.randomUUID = undefined
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000)
    try {
      const first = nextChatUploadRequestId('same.txt')
      const second = nextChatUploadRequestId('same.txt')
      expect(first).not.toBe(second)
      expect(first).toContain('same.txt')
    } finally {
      nowSpy.mockRestore()
      cryptoObj.randomUUID = original
    }
  })

  it('encodes files as base64', async () => {
    const file = new File(['hi'], 'a.txt', { type: 'text/plain' })
    vi.spyOn(file, 'arrayBuffer').mockResolvedValue(new Uint8Array([104, 105]).buffer)
    expect(await fileToBase64(file)).toBe(btoa('hi'))
  })

  it('rejects oversize browser files against the 10 MiB cap', () => {
    expect(UPLOAD_MAX_BYTES).toBe(10 * 1024 * 1024)
  })

  it('detects file drags and extracts dropped files', () => {
    const fileDrag = { dataTransfer: { types: ['Files'] } } as unknown as DragEvent
    const textDrag = { dataTransfer: { types: ['text/plain'] } } as unknown as DragEvent
    expect(isFileDrag(fileDrag)).toBe(true)
    expect(isFileDrag(textDrag)).toBe(false)
    expect(isFileDrag({} as DragEvent)).toBe(false)

    const file = new File(['x'], 'x.txt')
    const drop = { dataTransfer: { files: [file] } } as unknown as DragEvent
    expect(getDroppedFiles(drop)).toEqual([file])
    expect(getDroppedFiles({} as DragEvent)).toEqual([])
  })

  it('finds the upload dir and collects known filenames from the tree', () => {
    const tree: FileNode[] = [
      makeNode({ type: 'directory', name: 'workspace', path: '/workspace', children: [
        makeNode({ type: 'directory', name: '.opencuria', path: '/workspace/.opencuria', children: [
          makeNode({
            type: 'directory',
            name: 'user-uploaded',
            path: '/workspace/.opencuria/user-uploaded',
            children: [
              makeNode({ name: 'a.txt', path: '/workspace/.opencuria/user-uploaded/a.txt' }),
            ],
          }),
        ] }),
      ] }),
    ]
    expect(hasTreePath(tree, CHAT_UPLOAD_DIR)).toBe(true)
    expect(hasTreePath(tree, '/workspace/.opencuria/missing')).toBe(false)
    expect(collectUploadDirFilenames(tree)).toEqual(new Set(['a.txt']))
    expect(collectUploadDirFilenames([])).toEqual(new Set())
  })
})
