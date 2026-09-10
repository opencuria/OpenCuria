import { describe, expect, it } from 'vitest'
import {
  agentDisplayName,
  isSubagent,
  resolvePreviewEffort,
} from './harnessAgents'

describe('harnessAgents', () => {
  it('previews strategy efforts with floor(n/2) for medium', () => {
    expect(resolvePreviewEffort(['low', 'medium', 'high'], 'lowest')).toBe('low')
    expect(resolvePreviewEffort(['low', 'medium', 'high'], 'medium')).toBe('medium')
    expect(resolvePreviewEffort(['low', 'medium', 'high'], 'highest')).toBe('high')
    expect(resolvePreviewEffort(['a', 'b', 'c', 'd'], 'medium')).toBe('c')
    expect(resolvePreviewEffort(['low', 'high'], 'medium')).toBe('high')
  })

  it('returns empty for fixed/inherit/unknown/empty', () => {
    expect(resolvePreviewEffort(['low', 'high'], 'fixed')).toBe('')
    expect(resolvePreviewEffort(['low', 'high'], 'inherit')).toBe('')
    expect(resolvePreviewEffort(['low', 'high'], 'unknown')).toBe('')
    expect(resolvePreviewEffort(['low', 'high'], '')).toBe('')
    expect(resolvePreviewEffort([], 'lowest')).toBe('')
  })

  it('detects subagents by mode', () => {
    expect(isSubagent('subagent')).toBe(true)
    expect(isSubagent('primary')).toBe(false)
    expect(isSubagent({ mode: 'subagent' } as never)).toBe(true)
  })

  it('labels agents', () => {
    expect(agentDisplayName('build')).toBe('Build')
    expect(agentDisplayName('computeruse')).toBe('Computer Use')
    expect(agentDisplayName('mystery')).toBe('mystery')
  })
})
