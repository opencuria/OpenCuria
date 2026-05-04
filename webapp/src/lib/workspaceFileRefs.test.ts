import { describe, expect, it } from 'vitest'
import {
  buildWorkspaceReferenceMarkdown,
  classifyWorkspaceFile,
  extractWorkspacePathReferences,
} from './workspaceFileRefs'

describe('workspaceFileRefs', () => {
  it('classifies files by extension and mime type', () => {
    expect(classifyWorkspaceFile('image.png')).toBe('image')
    expect(classifyWorkspaceFile('clip.mov')).toBe('video')
    expect(classifyWorkspaceFile('readme.md')).toBe('text')
    expect(classifyWorkspaceFile('archive.bin')).toBe('binary')
    expect(classifyWorkspaceFile('noext', 'text/plain')).toBe('text')
  })

  it('builds markdown references by kind', () => {
    expect(buildWorkspaceReferenceMarkdown('cat.png', '/workspace/cat.png', 'image'))
      .toBe('![cat.png](/workspace/cat.png)')
    expect(buildWorkspaceReferenceMarkdown('notes.txt', '/workspace/notes.txt', 'text'))
      .toBe('[notes.txt](/workspace/notes.txt)')
  })

  it('extracts workspace path references from markdown', () => {
    const refs = extractWorkspacePathReferences(
      '![img](/workspace/pic.png)\n[file](/workspace/doc.txt)\n[web](https://example.com)',
    )

    expect(refs).toEqual([
      { path: '/workspace/pic.png', label: 'img', isMediaMarkdown: true },
      { path: '/workspace/doc.txt', label: 'file', isMediaMarkdown: false },
    ])
  })
})

