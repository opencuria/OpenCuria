import { describe, expect, it } from 'vitest'
import { imageMentionTokens, parseComposerSegments, removeComposerToken } from './composerTokens'

describe('composer mention tokens', () => {
  it('parses inline file and agent tokens with their exact offsets and image thumbnails', () => {
    const text = 'Fix @file:/workspace/src/shell.py with @agent:plan and @file:/workspace/cat.PNG now'
    const tokens = parseComposerSegments(text).filter((part) => part.kind !== 'text')
    expect(tokens.map((part) => part.kind)).toEqual(['file', 'agent', 'file'])
    expect(tokens[0]).toMatchObject({ name: 'shell.py', path: '/workspace/src/shell.py', start: 4 })
    expect(tokens[1]).toMatchObject({ name: 'plan', raw: '@agent:plan' })
    expect(imageMentionTokens(text)).toMatchObject([{ path: '/workspace/cat.PNG' }])
    expect(tokens.map((part) => text.slice(part.start, part.end))).toEqual(tokens.map((part) => part.raw))
  })

  it('keeps incomplete, unsafe and embedded references as literal text', () => {
    const input = 'email@file:/workspace/a.ts @file:relative.ts @file:/workspace/../secrets @file: @agent: and'
    expect(parseComposerSegments(input).filter((part) => part.kind !== 'text')).toEqual([])
    expect(imageMentionTokens('@file:/workspace/../secret.png')).toEqual([])
  })

  it('removes one token and its separator without affecting duplicates or adjacent text', () => {
    const text = 'A @file:/workspace/a.py B @file:/workspace/a.py C'
    const tokens = parseComposerSegments(text).filter((part) => part.kind === 'file')
    expect(removeComposerToken(text, tokens[0]!.start, tokens[0]!.end).text).toBe('A B @file:/workspace/a.py C')
    expect(removeComposerToken(text, -1, 5).text).toBe(text)
  })
})
