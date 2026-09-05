import { describe, expect, it } from 'vitest'
import {
  HARNESS_AGENT_NAMES,
  applyMentionCandidate,
  detectMentionQuery,
  filterMentionCandidates,
  flattenFilePaths,
} from './harnessMentions'
import type { FileNode } from '@/types'

function file(path: string): FileNode {
  return { name: path.split('/').pop() ?? path, path, type: 'file', size: 1 }
}

describe('harnessMentions', () => {
  it('exposes the four static agent names', () => {
    expect([...HARNESS_AGENT_NAMES]).toEqual(['build', 'plan', 'general', 'explore'])
  })

  it('flattens file-explorer trees to file paths only', () => {
    const tree: FileNode[] = [
      { name: 'src', path: '/workspace/src', type: 'directory', size: 0, children: [file('/workspace/src/a.ts')] },
      file('/workspace/README.md'),
    ]
    expect(flattenFilePaths(tree)).toEqual(['/workspace/src/a.ts', '/workspace/README.md'])
  })

  it('offers agents and files for a bare @ query', () => {
    const candidates = filterMentionCandidates('', ['/workspace/src/a.ts'])
    expect(candidates.some((c) => c.kind === 'agent' && c.insert === 'agent:plan')).toBe(true)
    expect(candidates.some((c) => c.kind === 'file' && c.insert === 'file:/workspace/src/a.ts')).toBe(true)
  })

  it('restricts to agents for agent: queries and to files for file: queries', () => {
    const agentsOnly = filterMentionCandidates('agent:pl', ['/workspace/plan.md'])
    expect(agentsOnly.length).toBeGreaterThan(0)
    expect(agentsOnly.every((c) => c.kind === 'agent')).toBe(true)
    expect(agentsOnly.some((c) => c.insert === 'agent:plan')).toBe(true)

    const filesOnly = filterMentionCandidates('file:src', ['/workspace/src/a.ts', '/workspace/README.md'])
    expect(filesOnly.length).toBe(1)
    expect(filesOnly[0]).toMatchObject({ kind: 'file', insert: 'file:/workspace/src/a.ts' })
  })

  it('detects the @ query before the cursor', () => {
    expect(detectMentionQuery('hello @pla', 10)).toBe('pla')
    expect(detectMentionQuery('no mention here', 15)).toBeNull()
    expect(detectMentionQuery('@file:/work', 11)).toBe('file:/work')
  })

  it('inserts the chosen candidate with @ prefix and trailing space', () => {
    const result = applyMentionCandidate('fix @pla', 8, {
      kind: 'agent',
      label: '@agent:plan',
      insert: 'agent:plan',
    })
    expect(result.text).toBe('fix @agent:plan ')
    expect(result.cursor).toBe(result.text.length)
  })
})
