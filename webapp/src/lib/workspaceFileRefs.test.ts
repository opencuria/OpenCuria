import { describe, expect, it } from 'vitest'
import {
  buildWorkspaceReferenceMarkdown,
  classifyWorkspaceFile,
  extractWorkspacePathReferences,
  resolveWorkspaceLinkPath,
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
    expect(buildWorkspaceReferenceMarkdown('cat.png', '/workspace/cat.png', 'image')).toBe(
      '![cat.png](/workspace/cat.png)',
    )
    expect(buildWorkspaceReferenceMarkdown('notes.txt', '/workspace/notes.txt', 'text')).toBe(
      '[notes.txt](/workspace/notes.txt)',
    )
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

  it.each([
    ['sb-laden-konzept/sb-laden-konzept.pdf', '/workspace/sb-laden-konzept/sb-laden-konzept.pdf'],
    ['./docs/../README.md', '/workspace/README.md'],
    ['/workspace/docs/plan.pdf', '/workspace/docs/plan.pdf'],
    ['docs/plan.pdf?download=1#page=2', '/workspace/docs/plan.pdf'],
    ['docs/Updated%20plan%20%C3%A4.pdf', '/workspace/docs/Updated plan ä.pdf'],
    ['docs/Updated plan.pdf', '/workspace/docs/Updated plan.pdf'],
    ['docs/100%25%20done%23final%3F.pdf', '/workspace/docs/100% done#final?.pdf'],
    ['docs/%252e%252e.pdf', '/workspace/docs/%2e%2e.pdf'],
  ])('resolves a local anchor href %s', (href, path) => {
    expect(resolveWorkspaceLinkPath(href)).toBe(path)
  })

  it.each([
    '',
    '#section',
    '?query=only',
    'https://example.com/plan.pdf',
    '//example.com/plan.pdf',
    'mailto:hello@example.com',
    'tel:123',
    'file:///workspace/plan.pdf',
    'data:application/pdf;base64,AAAA',
    'blob:abc',
    'javascript:alert(1)',
    '../etc/passwd',
    '/workspace-other/plan.pdf',
    '/tmp/plan.pdf',
    '/workspace/%2e%2e/etc/passwd',
    '%2e%2e/etc/passwd',
    '%2Ftmp/plan.pdf',
    '%2F%2Fexample.com/plan.pdf',
    '%68ttps%3Aexample.com',
    'bad%ZZ.pdf',
    'bad%E0%A4.pdf',
    'bad%00.pdf',
    'bad%0a.pdf',
    'docs%5cplan.pdf',
  ])('does not treat %s as a workspace file link', (href) => {
    expect(resolveWorkspaceLinkPath(href)).toBeNull()
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
