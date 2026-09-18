import { describe, expect, it } from 'vitest'
import {
  buildWorkspaceReferenceMarkdown,
  classifyWorkspaceFile,
  extractWorkspacePathReferences,
  resolveWorkspaceMediaPath,
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

  it('resolves relative and absolute dests under /workspace', () => {
    expect(resolveWorkspaceMediaPath('cat.png')).toBe('/workspace/cat.png')
    expect(resolveWorkspaceMediaPath('./cat.png')).toBe('/workspace/cat.png')
    expect(resolveWorkspaceMediaPath('screenshots/login.png')).toBe(
      '/workspace/screenshots/login.png',
    )
    expect(resolveWorkspaceMediaPath('/workspace/cat.png')).toBe('/workspace/cat.png')
    expect(resolveWorkspaceMediaPath('cat.png "kitten"')).toBe('/workspace/cat.png')
    expect(resolveWorkspaceMediaPath('<screenshots/login.png>')).toBe(
      '/workspace/screenshots/login.png',
    )
  })

  it('rejects remote URLs and sandbox escapes', () => {
    expect(resolveWorkspaceMediaPath('https://example.com/a.png')).toBeNull()
    expect(resolveWorkspaceMediaPath('data:image/png;base64,AAAA')).toBeNull()
    expect(resolveWorkspaceMediaPath('../etc/passwd.png')).toBeNull()
    expect(resolveWorkspaceMediaPath('/tmp/x.png')).toBeNull()
    expect(resolveWorkspaceMediaPath('/workspace/../etc/passwd.png')).toBeNull()
  })

  it('extracts workspace path references from markdown', () => {
    const refs = extractWorkspacePathReferences(
      '![img](/workspace/pic.png)\n[file](/workspace/doc.txt)\n[web](https://example.com)\n![shot](screenshots/a.png)',
    )

    expect(refs).toEqual([
      { path: '/workspace/pic.png', label: 'img', isMediaMarkdown: true },
      { path: '/workspace/doc.txt', label: 'file', isMediaMarkdown: false },
      { path: '/workspace/screenshots/a.png', label: 'shot', isMediaMarkdown: true },
    ])
  })
})

