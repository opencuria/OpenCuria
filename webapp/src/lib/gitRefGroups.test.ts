import { describe, expect, it } from 'vitest'

import type { GitRefTag } from '@/types/git'
import { groupRefTags, parseRemoteRef } from './gitRefGroups'

function tag(name: string, remote = false, current = false): GitRefTag {
  return { name, remote, current }
}

describe('parseRemoteRef', () => {
  it('splits a simple remote ref', () => {
    expect(parseRemoteRef('origin/main', ['origin'])).toEqual({
      remote: 'origin',
      branch: 'main',
    })
  })

  it('prefers the longest known-remote prefix (origin/feature/foo)', () => {
    expect(parseRemoteRef('origin/feature/foo', ['origin'])).toEqual({
      remote: 'origin',
      branch: 'feature/foo',
    })
  })

  it('falls back to the first segment for unknown remotes', () => {
    expect(parseRemoteRef('upstream/main', ['origin'])).toEqual({
      remote: 'upstream',
      branch: 'main',
    })
  })

  it('returns null for bare names without a slash', () => {
    expect(parseRemoteRef('main', ['origin'])).toBeNull()
    expect(parseRemoteRef('', ['origin'])).toBeNull()
  })
})

describe('groupRefTags', () => {
  it('groups a local branch with its same-name remote into one badge', () => {
    const groups = groupRefTags(
      [tag('main', false, true), tag('origin/main', true)],
      ['origin'],
    )
    expect(groups).toHaveLength(1)
    expect(groups[0]).toMatchObject({
      local: { name: 'main', current: true },
      remotes: [{ full: 'origin/main', remote: 'origin' }],
      isHead: false,
    })
  })

  it('collects multiple remotes into one badge, sorted by remote', () => {
    const groups = groupRefTags(
      [
        tag('main', false, true),
        tag('upstream/main', true),
        tag('origin/main', true),
      ],
      ['origin', 'upstream'],
    )
    expect(groups).toHaveLength(1)
    expect(groups[0]!.remotes).toEqual([
      { full: 'origin/main', remote: 'origin' },
      { full: 'upstream/main', remote: 'upstream' },
    ])
  })

  it('groups nested branch names (origin/feature/foo → feature/foo)', () => {
    const groups = groupRefTags(
      [tag('feature/foo'), tag('origin/feature/foo', true)],
      ['origin'],
    )
    expect(groups).toHaveLength(1)
    expect(groups[0]!.local?.name).toBe('feature/foo')
    expect(groups[0]!.remotes).toEqual([
      { full: 'origin/feature/foo', remote: 'origin' },
    ])
  })

  it('never matches fork/main to local main (no suffix guessing)', () => {
    const groups = groupRefTags(
      [tag('main', false, true), tag('fork/main', true)],
      ['origin'],
    )
    expect(groups).toHaveLength(2)
    const local = groups.find((g) => g.local?.name === 'main')
    expect(local?.remotes).toEqual([])
    const remote = groups.find((g) => g.local === null)
    expect(remote?.remotes).toEqual([{ full: 'fork/main', remote: 'fork' }])
  })

  it('keeps unknown remotes as standalone badges', () => {
    const groups = groupRefTags([tag('main'), tag('mystery/main', true)], [
      'origin',
    ])
    expect(groups).toHaveLength(2)
    expect(groups.find((g) => g.local?.name === 'main')?.remotes).toEqual([])
  })

  it('keeps remote-only refs as standalone badges', () => {
    const groups = groupRefTags([tag('origin/main', true)], ['origin'])
    expect(groups).toHaveLength(1)
    expect(groups[0]).toMatchObject({
      local: null,
      remotes: [{ full: 'origin/main', remote: 'origin' }],
    })
  })

  it('keeps the HEAD pseudo-tag always separate', () => {
    const groups = groupRefTags(
      [tag('HEAD', false, true), tag('main', false, true), tag('origin/main', true)],
      ['origin'],
    )
    expect(groups).toHaveLength(2)
    expect(groups[0]).toMatchObject({ isHead: true, local: { name: 'HEAD' } })
    expect(groups[0]!.remotes).toEqual([])
  })

  it('sorts the current branch first', () => {
    const groups = groupRefTags(
      [tag('aaa'), tag('main', false, true), tag('origin/main', true)],
      ['origin'],
    )
    expect(groups[0]!.local?.name).toBe('main')
  })
})
