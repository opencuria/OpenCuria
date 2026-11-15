import { describe, expect, it } from 'vitest'

import { diffFileName, parseFileDiff } from './fileDiff'

const MULTI_HUNK = `--- a/CommandPalette.vue
+++ b/CommandPalette.vue
@@ -272,6 +272,7 @@
   <Dialog :open="props.open" @update:open="setOpen">
-  <DialogContent class="max-w-xl p-0" :show-close-button>
+  <DialogContent
+    class="gap-0 p-0 sm:max-w-2xl"
     extra
     lines
     here
     too
`

const ONE_LINE = `--- a/a.ts
+++ b/a.ts
@@ -9,3 +9,3 @@
   const a = 1
-  const b = 2
+  const b = 3
   const c = 4
`

const THREE_CONTEXT = `--- a/a.ts
+++ b/a.ts
@@ -1,7 +1,7 @@
 keep-1
 keep-2
 keep-3
-old
+new
 keep-4
 keep-5
 keep-6
`

describe('parseFileDiff', () => {
  it('counts additions and deletions across the full diff', () => {
    const parsed = parseFileDiff(MULTI_HUNK)
    expect(parsed.additions).toBe(2)
    expect(parsed.deletions).toBe(1)
  })

  it('caps the collapsed preview at 4 lines with one leading context', () => {
    const parsed = parseFileDiff(MULTI_HUNK)
    expect(parsed.collapsed).toHaveLength(4)
    expect(parsed.collapsed.map((line) => line.type)).toEqual([
      'context',
      'del',
      'add',
      'add',
    ])
    expect(parsed.collapsed[0]?.content).toContain('<Dialog :open')
    expect(parsed.collapsed.some((line) => line.content.includes('extra'))).toBe(
      false,
    )
  })

  it('keeps the line above and below a one-line replacement', () => {
    const parsed = parseFileDiff(ONE_LINE)
    expect(parsed.collapsed.map((line) => line.content)).toEqual([
      '  const a = 1',
      '  const b = 2',
      '  const b = 3',
      '  const c = 4',
    ])
    expect(parsed.collapsed.map((line) => line.type)).toEqual([
      'context',
      'del',
      'add',
      'context',
    ])
  })

  it('trims expanded hunks to two context lines on each edge', () => {
    const parsed = parseFileDiff(THREE_CONTEXT)
    expect(parsed.expanded.map((line) => line.content)).toEqual([
      'keep-2',
      'keep-3',
      'old',
      'new',
      'keep-4',
      'keep-5',
    ])
    expect(parsed.expanded.some((line) => line.content === 'keep-1')).toBe(false)
    expect(parsed.expanded.some((line) => line.content === 'keep-6')).toBe(false)
  })

  it('parses diffs that omit hunk headers', () => {
    const parsed = parseFileDiff('--- a/a.txt\n+++ b/a.txt\n-old\n+new')
    expect(parsed.additions).toBe(1)
    expect(parsed.deletions).toBe(1)
    expect(parsed.collapsed.map((line) => line.content)).toEqual(['old', 'new'])
  })
})

describe('diffFileName', () => {
  it('returns the basename of a workspace path', () => {
    expect(diffFileName('/workspace/src/CommandPalette.vue')).toBe(
      'CommandPalette.vue',
    )
    expect(diffFileName('a.txt')).toBe('a.txt')
  })
})
