import { classifyWorkspaceFile } from '@/lib/workspaceFileRefs'
import { HARNESS_AGENT_NAMES } from '@/lib/harnessMentions'

export type ComposerTextSegment = { kind: 'text'; text: string; start: number; end: number }
export type ComposerFileToken = { kind: 'file'; raw: string; path: string; name: string; start: number; end: number }
export type ComposerAgentToken = { kind: 'agent'; raw: string; name: string; start: number; end: number }
export type ComposerToken = ComposerFileToken | ComposerAgentToken
export type ComposerSegment = ComposerTextSegment | ComposerToken

/** Only complete workspace references become chips. Keep arbitrary user text untouched. */
export function parseComposerSegments(text: string): ComposerSegment[] {
  if (!text) return []
  const result: ComposerSegment[] = []
  const pattern = /@(?:file:(\/workspace\/[^\s<>"`\\]+)|agent:([\w-]+))/g
  let last = 0
  for (const match of text.matchAll(pattern)) {
    const start = match.index
    if (start > 0 && !/\s/.test(text[start - 1]!)) continue
    let raw = match[0]
    // Sentence punctuation is not part of a mention, but a dot in a filename is.
    if (match[1]) raw = raw.replace(/[),;!?\]]+$/, '')
    const end = start + raw.length
    if (end < text.length && !/[\s),;!?\]]/.test(text[end]!)) continue
    let token: ComposerToken
    if (match[1]) {
      const path = raw.slice('@file:'.length)
      const parts = path.split('/')
      if (parts.some((part) => part === '..' || part === '.') || !parts[parts.length - 1]) continue
      token = { kind: 'file', path, name: parts[parts.length - 1]!, raw, start, end }
    } else {
      const name = match[2]!
      if (!HARNESS_AGENT_NAMES.some((agent) => agent === name)) continue
      token = { kind: 'agent', name, raw, start, end }
    }
    if (start > last) result.push({ kind: 'text', text: text.slice(last, start), start: last, end: start })
    result.push(token)
    last = end
  }
  if (last < text.length) result.push({ kind: 'text', text: text.slice(last), start: last, end: text.length })
  return result
}

export function imageMentionTokens(text: string): ComposerFileToken[] {
  return parseComposerSegments(text).filter(
    (part): part is ComposerFileToken => part.kind === 'file' && classifyWorkspaceFile(part.path) === 'image',
  )
}

/** Remove exactly one token, not a similarly named mention elsewhere. */
export function removeComposerToken(text: string, start: number, end: number): { text: string; cursor: number } {
  const token = parseComposerSegments(text).find((part) => part.kind !== 'text' && part.start === start && part.end === end)
  if (!token) return { text, cursor: Math.min(start, text.length) }
  // Consume one following separator, or the separator preceding an end-of-line token.
  let from = start
  let to = end
  if (text[to] === ' ') to++
  else if (from > 0 && text[from - 1] === ' ') from--
  return { text: text.slice(0, from) + text.slice(to), cursor: from }
}
