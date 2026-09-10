import { describe, expect, it } from 'vitest'

import type { HarnessPart } from '@/types/harness'
import {
  attachmentByteSize,
  attachmentSizeLabel,
  deltaAttachments,
  formatAttachmentSize,
  isImageAttachment,
  isPdfAttachment,
  toolAttachments,
  TOOL_ATTACHMENT_MAX_COUNT,
} from './harnessAttachments'

function makePart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'part-1',
    session_id: 'session-1',
    type: 'tool',
    state: 'completed',
    title: '',
    output: '',
    ...overrides,
  }
}

const IMAGE_URL = 'data:image/png;base64,iVBORw0KGgo='
const PDF_URL = 'data:application/pdf;base64,JVBERi0='

describe('toolAttachments', () => {
  it('returns [] for missing or malformed meta', () => {
    expect(toolAttachments(makePart())).toEqual([])
    expect(toolAttachments(makePart({ meta: undefined }))).toEqual([])
    expect(toolAttachments(makePart({ meta: {} }))).toEqual([])
    expect(toolAttachments(makePart({ meta: { attachments: 'nope' } }))).toEqual([])
    expect(toolAttachments(makePart({ meta: { attachments: null } }))).toEqual([])
    expect(toolAttachments(makePart({ meta: { attachments: [{ nope: 1 }] } }))).toEqual([])
  })

  it('keeps only file attachments with a mime and a data: URL', () => {
    const part = makePart({
      meta: {
        attachments: [
          { type: 'file', mime: 'image/png', url: IMAGE_URL },
          { type: 'text', mime: 'text/plain', url: 'data:text/plain;base64,aGk=' },
          { type: 'file', mime: '', url: IMAGE_URL },
          { type: 'file', mime: 'image/png', url: 'https://example.com/a.png' },
          { type: 'file', mime: 'application/pdf', url: PDF_URL },
          'broken',
          null,
        ],
      },
    })
    expect(toolAttachments(part)).toEqual([
      { type: 'file', mime: 'image/png', url: IMAGE_URL },
      { type: 'file', mime: 'application/pdf', url: PDF_URL },
    ])
  })

  it(`caps the result at ${TOOL_ATTACHMENT_MAX_COUNT} entries like the backend`, () => {
    expect(TOOL_ATTACHMENT_MAX_COUNT).toBe(2)
    const part = makePart({
      meta: {
        attachments: [
          { type: 'file', mime: 'image/png', url: `${IMAGE_URL}1` },
          { type: 'file', mime: 'image/jpeg', url: 'data:image/jpeg;base64,/9j/' },
          { type: 'file', mime: 'application/pdf', url: PDF_URL },
        ],
      },
    })
    const kept = toolAttachments(part)
    expect(kept).toHaveLength(2)
    expect(kept.map((entry) => entry.mime)).toEqual(['image/png', 'image/jpeg'])
  })

  it('returns defensive copies', () => {
    const raw = { type: 'file', mime: 'image/png', url: IMAGE_URL, extra: 'drop-me' }
    const part = makePart({ meta: { attachments: [raw] } })
    const kept = toolAttachments(part)
    expect(kept).toEqual([{ type: 'file', mime: 'image/png', url: IMAGE_URL }])
    expect(kept[0]).not.toBe(raw)
  })

  it('carries over a non-empty filename and drops empty/non-string ones', () => {
    const part = makePart({
      meta: {
        attachments: [
          { type: 'file', mime: 'application/pdf', url: PDF_URL, filename: 'doc.pdf' },
          { type: 'file', mime: 'image/png', url: IMAGE_URL, filename: '  cat.png  ' },
          { type: 'file', mime: 'image/png', url: `${IMAGE_URL}-empty`, filename: '' },
        ],
      },
    })
    // Cap is 2, so only the first two are kept.
    const kept = toolAttachments(part)
    expect(kept).toHaveLength(2)
    expect(kept[0]).toEqual({
      type: 'file',
      mime: 'application/pdf',
      url: PDF_URL,
      filename: 'doc.pdf',
    })
    expect(kept[1]).toEqual({
      type: 'file',
      mime: 'image/png',
      url: IMAGE_URL,
      filename: 'cat.png',
    })

    const empties = makePart({
      meta: {
        attachments: [
          { type: 'file', mime: 'image/png', url: IMAGE_URL, filename: '' },
          { type: 'file', mime: 'image/png', url: `${IMAGE_URL}2`, filename: '   ' },
          { type: 'file', mime: 'image/png', url: `${IMAGE_URL}3`, filename: 42 },
          { type: 'file', mime: 'image/png', url: `${IMAGE_URL}4`, filename: null },
        ],
      },
    })
    for (const entry of toolAttachments(empties)) {
      expect(entry).not.toHaveProperty('filename')
    }
  })
})

describe('deltaAttachments', () => {
  it('returns [] for missing or malformed deltas', () => {
    expect(deltaAttachments(undefined)).toEqual([])
    expect(deltaAttachments(null)).toEqual([])
    expect(deltaAttachments('nope')).toEqual([])
    expect(deltaAttachments({})).toEqual([])
    expect(deltaAttachments({ attachments: 'nope' })).toEqual([])
    expect(deltaAttachments({ attachments: null })).toEqual([])
    expect(deltaAttachments({ attachments: [{ nope: 1 }] })).toEqual([])
  })

  it('sanitizes live delta attachments like toolAttachments', () => {
    expect(
      deltaAttachments({
        tool_completed: 'read',
        attachments: [
          { type: 'file', mime: 'image/png', url: IMAGE_URL, filename: 'cat.png' },
          { type: 'text', mime: 'text/plain', url: 'data:text/plain;base64,aGk=' },
          { type: 'file', mime: '', url: IMAGE_URL },
          { type: 'file', mime: 'image/png', url: 'https://example.com/a.png' },
          { type: 'file', mime: 'application/pdf', url: PDF_URL, filename: 'doc.pdf' },
          'broken',
          null,
        ],
      }),
    ).toEqual([
      { type: 'file', mime: 'image/png', url: IMAGE_URL, filename: 'cat.png' },
      { type: 'file', mime: 'application/pdf', url: PDF_URL, filename: 'doc.pdf' },
    ])
  })

  it(`caps the result at ${TOOL_ATTACHMENT_MAX_COUNT} entries`, () => {
    const kept = deltaAttachments({
      attachments: [
        { type: 'file', mime: 'image/png', url: `${IMAGE_URL}1` },
        { type: 'file', mime: 'image/jpeg', url: 'data:image/jpeg;base64,/9j/' },
        { type: 'file', mime: 'application/pdf', url: PDF_URL },
      ],
    })
    expect(kept).toHaveLength(2)
    expect(kept.map((entry) => entry.mime)).toEqual(['image/png', 'image/jpeg'])
  })
})

describe('isImageAttachment / isPdfAttachment', () => {
  it('detects images by image/* MIME prefix', () => {
    expect(isImageAttachment({ mime: 'image/png' })).toBe(true)
    expect(isImageAttachment({ mime: 'Image/JPEG' })).toBe(true)
    expect(isImageAttachment({ mime: 'application/pdf' })).toBe(false)
    expect(isImageAttachment(null)).toBe(false)
    expect(isImageAttachment(undefined)).toBe(false)
  })

  it('detects PDFs by application/pdf MIME', () => {
    expect(isPdfAttachment({ mime: 'application/pdf' })).toBe(true)
    expect(isPdfAttachment({ mime: 'Application/PDF' })).toBe(true)
    expect(isPdfAttachment({ mime: 'image/png' })).toBe(false)
    expect(isPdfAttachment(null)).toBe(false)
  })
})

describe('attachmentByteSize / attachmentSizeLabel / formatAttachmentSize', () => {
  it('estimates bytes from the base64 payload', () => {
    // "aGk=" decodes to "hi" (2 bytes).
    expect(attachmentByteSize('data:text/plain;base64,aGk=')).toBe(2)
    expect(attachmentByteSize('https://example.com/a.png')).toBeNull()
    expect(attachmentByteSize('data:image/png;base64,')).toBeNull()
  })

  it('formats like the file viewer', () => {
    expect(formatAttachmentSize(512)).toBe('512 B')
    expect(formatAttachmentSize(2048)).toBe('2.0 KB')
    expect(formatAttachmentSize(2 * 1024 * 1024)).toBe('2.0 MB')
    expect(attachmentSizeLabel('data:text/plain;base64,aGk=')).toBe('2 B')
    expect(attachmentSizeLabel('https://example.com/a.png')).toBeNull()
  })
})
