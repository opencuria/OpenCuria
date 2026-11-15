// @vitest-environment node
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { buildMonacoChangePatch, monacoChangeRange, type MonacoChangeRange } from './monacoChanges'

function applyPatch(original: string, modified: string, range: MonacoChangeRange) {
  const directory = mkdtempSync(join(tmpdir(), 'opencuria-editor-patch-'))
  try {
    const git = (...args: string[]) => execFileSync('git', args, { cwd: directory, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] })
    git('init', '--quiet')
    writeFileSync(join(directory, 'file.txt'), original)
    git('add', 'file.txt')
    const patch = buildMonacoChangePatch(original, modified, range)
    expect(patch).not.toBeNull()
    const fullPatch = `--- a/file.txt\n+++ b/file.txt\n${patch}`
    execFileSync('git', ['apply', '--cached', '--unidiff-zero', '-'], { cwd: directory, input: fullPatch, stdio: ['pipe', 'pipe', 'pipe'] })
    const staged = git('show', ':file.txt')
    writeFileSync(join(directory, 'file.txt'), modified)
    const reversePatch = `--- a/file.txt\n+++ b/file.txt\n${buildMonacoChangePatch(original, modified, range, 'discard')}`
    execFileSync('git', ['apply', '--reverse', '--unidiff-zero', '-'], { cwd: directory, input: reversePatch, stdio: ['pipe', 'pipe', 'pipe'] })
    return { staged, reverted: readFileSync(join(directory, 'file.txt'), 'utf8') }
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
}

describe('Monaco change ranges', () => {
  it('converts inclusive replacements and empty insertions/deletions to half-open ranges', () => {
    expect(monacoChangeRange({ originalStartLineNumber: 3, originalEndLineNumber: 5, modifiedStartLineNumber: 2, modifiedEndLineNumber: 4 }))
      .toEqual({ originalStart: 3, originalEnd: 6, modifiedStart: 2, modifiedEnd: 5 })
    expect(monacoChangeRange({ originalStartLineNumber: 0, originalEndLineNumber: 0, modifiedStartLineNumber: 1, modifiedEndLineNumber: 2 }))
      .toEqual({ originalStart: 1, originalEnd: 1, modifiedStart: 1, modifiedEnd: 3 })
  })
})

describe('editor Git patches applied by real Git', () => {
  it('stages and reverts a replacement without touching another changed hunk', () => {
    expect(applyPatch('a\nb\nc\nd\n', 'A\nb\nC\nd\n', { originalStart: 3, originalEnd: 4, modifiedStart: 3, modifiedEnd: 4 }))
      .toEqual({ staged: 'a\nb\nC\nd\n', reverted: 'A\nb\nc\nd\n' })
  })

  it('stages and reverts selected lines within an inserted block', () => {
    expect(applyPatch('a\nb\n', 'a\nx\ny\nz\nb\n', {
      originalStart: 2, originalEnd: 2, modifiedStart: 2, modifiedEnd: 5,
      selection: { side: 'modified', startLine: 3, endLine: 4 },
    })).toEqual({ staged: 'a\ny\nb\n', reverted: 'a\nx\nz\nb\n' })
  })

  it('stages and reverts selected removed lines from the original pane', () => {
    expect(applyPatch('a\nx\ny\nz\nb\n', 'a\nb\n', {
      originalStart: 2, originalEnd: 5, modifiedStart: 2, modifiedEnd: 2,
      selection: { side: 'original', startLine: 3, endLine: 4 },
    })).toEqual({ staged: 'a\nx\nz\nb\n', reverted: 'a\ny\nb\n' })
  })

  it('stages and reverts a selected replacement line', () => {
    expect(applyPatch('a\nb\nc\nd\n', 'a\nB\nC\nd\n', {
      originalStart: 2, originalEnd: 4, modifiedStart: 2, modifiedEnd: 4,
      selection: { side: 'modified', startLine: 3, endLine: 4 },
    })).toEqual({ staged: 'a\nb\nC\nd\n', reverted: 'a\nB\nc\nd\n' })
  })

  it('handles whole-file additions, empty files, and deletions', () => {
    expect(applyPatch('', 'a\nb\n', { originalStart: 1, originalEnd: 1, modifiedStart: 1, modifiedEnd: 4 }))
      .toEqual({ staged: 'a\nb\n', reverted: '' })
    expect(applyPatch('a\nb\n', '', { originalStart: 1, originalEnd: 4, modifiedStart: 1, modifiedEnd: 1 }))
      .toEqual({ staged: '', reverted: 'a\nb\n' })
  })

  it('preserves CRLF and missing final newline markers', () => {
    expect(applyPatch('a\r\nb\r\n', 'a\r\nB', { originalStart: 2, originalEnd: 3, modifiedStart: 2, modifiedEnd: 3 }))
      .toEqual({ staged: 'a\r\nB', reverted: 'a\r\nb\r\n' })
    expect(applyPatch('a', 'a\n', { originalStart: 1, originalEnd: 2, modifiedStart: 1, modifiedEnd: 3 }))
      .toEqual({ staged: 'a\n', reverted: 'a' })
  })

  it('applies a selection across multiple hunks atomically', () => {
    expect(applyPatch('a\nb\nc\nd\n', 'A\nb\nC\nd\n', {
      originalStart: 1, originalEnd: 2, modifiedStart: 1, modifiedEnd: 2,
      additionalChanges: [{ originalStart: 3, originalEnd: 4, modifiedStart: 3, modifiedEnd: 4 }],
    })).toEqual({ staged: 'A\nb\nC\nd\n', reverted: 'a\nb\nc\nd\n' })
  })

  it('accounts for selected insertions and earlier unselected changes across hunks', () => {
    expect(applyPatch('a\nb\nc\nd\n', 'X\na\nY\nb\nc\nZ\nd\n', {
      originalStart: 2, originalEnd: 2, modifiedStart: 3, modifiedEnd: 4,
      additionalChanges: [{ originalStart: 4, originalEnd: 4, modifiedStart: 6, modifiedEnd: 7 }],
    })).toEqual({ staged: 'a\nY\nb\nc\nZ\nd\n', reverted: 'X\na\nb\nc\nd\n' })
  })

  it('ignores selections outside the changed lines', () => {
    expect(buildMonacoChangePatch('a\nb\n', 'a\nB\n', {
      originalStart: 2, originalEnd: 3, modifiedStart: 2, modifiedEnd: 3,
      selection: { side: 'modified', startLine: 4, endLine: 5 },
    })).toBeNull()
  })
})
