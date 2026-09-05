import { describe, expect, it } from 'vitest'
import {
  HARNESS_AGENT_NAMES,
  MENTION_FILE_LIMIT,
  MENTION_TOTAL_LIMIT,
  applyMentionCandidate,
  consumeSlashQuery,
  createPointerHoverGate,
  detectMentionQuery,
  detectSlashQuery,
  filterMentionCandidates,
  filterSkillCandidates,
  flattenFilePaths,
  mentionFileSearchQuery,
  mergeMentionFilePaths,
  workspaceRelativePath,
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
    expect(filesOnly[0]).toMatchObject({
      kind: 'file',
      label: 'src/a.ts',
      insert: 'file:/workspace/src/a.ts',
    })
  })

  it('ranks basename matches first and caps the file list', () => {
    const ranked = filterMentionCandidates('util', [
      '/workspace/src/utility/readme.md',
      '/workspace/notes-util.md',
      '/workspace/src/utils.ts',
    ]).filter((candidate) => candidate.kind === 'file')
    expect(ranked.map((candidate) => candidate.label)).toEqual([
      'src/utils.ts',
      'notes-util.md',
      'src/utility/readme.md',
    ])

    const many = Array.from({ length: 20 }, (_, index) => `/workspace/f${index}.ts`)
    const capped = filterMentionCandidates('', many)
    const files = capped.filter((candidate) => candidate.kind === 'file')
    expect(files.length).toBeLessThanOrEqual(MENTION_FILE_LIMIT)
    expect(capped.length).toBe(MENTION_TOTAL_LIMIT)
    expect(capped.filter((candidate) => candidate.kind === 'agent').length).toBe(
      HARNESS_AGENT_NAMES.length,
    )
  })

  it('strips file: for search and keeps agent: from fetching files', () => {
    expect(mentionFileSearchQuery('file:src/a')).toBe('src/a')
    expect(mentionFileSearchQuery('agent:plan')).toBeNull()
    expect(mentionFileSearchQuery('src')).toBe('src')
    expect(workspaceRelativePath('/workspace/src/a.ts')).toBe('src/a.ts')
    expect(mergeMentionFilePaths(['/workspace/a.ts'], ['/workspace/a.ts', '/workspace/b.ts'])).toEqual([
      '/workspace/a.ts',
      '/workspace/b.ts',
    ])
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

  it('detects a slash skill query but ignores absolute paths', () => {
    expect(detectSlashQuery('/lint', 5)).toBe('lint')
    expect(detectSlashQuery('hello /rev', 10)).toBe('rev')
    expect(detectSlashQuery('/workspace/foo', 14)).toBeNull()
    expect(detectSlashQuery('no slash', 8)).toBeNull()
  })

  it('filters skills by name and consumes the slash query', () => {
    const skills = [
      { id: 's1', name: 'Lint rules' },
      { id: 's2', name: 'Review' },
    ]
    const hits = filterSkillCandidates('lin', skills)
    expect(hits).toEqual([{ kind: 'skill', label: 'Lint rules', insert: 's1' }])
    const consumed = consumeSlashQuery('hello /lin', 10)
    expect(consumed.text).toBe('hello ')
  })

  it('ignores pointer hover until the cursor actually moves', () => {
    const gate = createPointerHoverGate()
    expect(gate.moved({ clientX: 10, clientY: 20 })).toBe(true)
    expect(gate.moved({ clientX: 10, clientY: 20 })).toBe(false)
    expect(gate.moved({ clientX: 11, clientY: 20 })).toBe(true)
    gate.reset()
    expect(gate.moved({ clientX: 11, clientY: 20 })).toBe(true)
  })
})
