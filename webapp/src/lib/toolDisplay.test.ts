import { describe, expect, it } from 'vitest'

import type { HarnessPart } from '@/types/harness'
import {
  computerUseSummary,
  hydrateHarnessPart,
  parseToolArguments,
  resolveToolName,
  toolDisplayLabel,
  truncatePreview,
} from './toolDisplay'

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

describe('parseToolArguments', () => {
  it('parses a JSON argument string', () => {
    const part = makePart({
      input: { tool: 'read', arguments: '{"path":"/workspace/a.ts"}' },
    })
    expect(parseToolArguments(part)).toEqual({ path: '/workspace/a.ts' })
  })

  it('accepts an already-parsed arguments object', () => {
    const part = makePart({
      input: { tool: 'bash', arguments: { command: 'ls' } },
    })
    expect(parseToolArguments(part)).toEqual({ command: 'ls' })
  })

  it('returns {} for missing or invalid arguments', () => {
    expect(parseToolArguments(makePart())).toEqual({})
    expect(parseToolArguments(makePart({ input: { arguments: 'not-json' } }))).toEqual({})
  })
})

describe('resolveToolName / hydrateHarnessPart', () => {
  it('prefers part.tool then input.tool', () => {
    expect(resolveToolName(makePart({ tool: 'read' }))).toBe('read')
    expect(resolveToolName(makePart({ input: { tool: 'bash' } }))).toBe('bash')
  })

  it('hydrates tool from input after a parts reload', () => {
    const hydrated = hydrateHarnessPart(
      makePart({ input: { tool: 'grep', arguments: '{"pattern":"foo"}' } }),
    )
    expect(hydrated.tool).toBe('grep')
  })
})

describe('toolDisplayLabel', () => {
  it('labels reasoning as Thought', () => {
    expect(toolDisplayLabel(makePart({ type: 'reasoning', output: 'hmm' }))).toBe('Thought')
  })

  it('prefers the persisted title', () => {
    expect(toolDisplayLabel(makePart({ tool: 'read', title: 'Read a.ts' }))).toBe('Read a.ts')
  })

  it('falls back to argument-derived labels', () => {
    expect(
      toolDisplayLabel(
        makePart({
          tool: 'bash',
          input: { arguments: '{"command":"ls -la"}' },
        }),
      ),
    ).toBe('$ ls -la')
    expect(
      toolDisplayLabel(
        makePart({
          tool: 'read',
          input: { arguments: '{"path":"/workspace/src/a.ts"}' },
        }),
      ),
    ).toBe('Read a.ts')
  })
})

describe('computerUseSummary', () => {
  it('formats click coordinates and typed text', () => {
    expect(
      computerUseSummary(
        makePart({
          tool: 'left_click',
          input: { arguments: '{"x":120,"y":40}' },
        }),
      ),
    ).toBe('Left click (120, 40)')
    expect(
      computerUseSummary(
        makePart({
          tool: 'type_text',
          input: { arguments: '{"text":"hello"}' },
        }),
      ),
    ).toBe('Type “hello”')
  })
})

describe('truncatePreview', () => {
  it('keeps short output and truncates long output', () => {
    expect(truncatePreview('ok')).toBe('ok')
    const long = 'x'.repeat(2001)
    expect(truncatePreview(long, 2000)).toContain('truncated 2001 chars')
  })
})
