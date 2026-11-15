/** Monaco and Git use different conventions for empty and inclusive line ranges. */
export interface MonacoLineChange {
  originalStartLineNumber: number
  originalEndLineNumber: number
  modifiedStartLineNumber: number
  modifiedEndLineNumber: number
}

export interface MonacoLineSelection {
  side: 'original' | 'modified'
  startLine: number
  endLine: number
}

/** One-based, half-open ranges; an insertion/deletion has an empty side. */
export interface MonacoChangeRange {
  originalStart: number
  originalEnd: number
  modifiedStart: number
  modifiedEnd: number
  selection?: MonacoLineSelection
  additionalChanges?: MonacoChangeRange[]
}

export function monacoChangeRange(change: MonacoLineChange): MonacoChangeRange {
  return {
    originalStart: change.originalEndLineNumber === 0
      ? change.originalStartLineNumber + 1 : change.originalStartLineNumber,
    originalEnd: change.originalEndLineNumber === 0
      ? change.originalStartLineNumber + 1 : change.originalEndLineNumber + 1,
    modifiedStart: change.modifiedEndLineNumber === 0
      ? change.modifiedStartLineNumber + 1 : change.modifiedStartLineNumber,
    modifiedEnd: change.modifiedEndLineNumber === 0
      ? change.modifiedStartLineNumber + 1 : change.modifiedEndLineNumber + 1,
  }
}

export function monacoSelectionOverlaps(
  range: MonacoChangeRange,
  selection: MonacoLineSelection,
): boolean {
  const start = selection.side === 'original' ? range.originalStart : range.modifiedStart
  const end = selection.side === 'original' ? range.originalEnd : range.modifiedEnd
  // Empty-side deletions/insertions are anchored immediately after the preceding line.
  return start === end
    ? selection.startLine <= Math.max(1, start - 1) && selection.endLine > Math.max(1, start - 1)
    : selection.startLine < end && selection.endLine > start
}

function selectedRange(range: MonacoChangeRange): MonacoChangeRange | null {
  const selection = range.selection
  if (!selection) return range
  if (!monacoSelectionOverlaps(range, selection)) return null
  const originalSide = selection.side === 'original'
  const start = originalSide ? range.originalStart : range.modifiedStart
  const end = originalSide ? range.originalEnd : range.modifiedEnd
  if (start === end) return range
  const selectedStart = Math.max(start, selection.startLine)
  const selectedEnd = Math.min(end, selection.endLine)
  const otherStart = originalSide ? range.modifiedStart : range.originalStart
  const otherEnd = originalSide ? range.modifiedEnd : range.originalEnd
  // Align replacements by line position. Selecting the final replacement line
  // also includes any excess removed lines (and vice versa in the original pane).
  const mappedStart = Math.min(otherEnd, otherStart + selectedStart - start)
  const mappedEnd = selectedEnd === end
    ? otherEnd : Math.min(otherEnd, otherStart + selectedEnd - start)
  return originalSide
    ? { originalStart: selectedStart, originalEnd: selectedEnd, modifiedStart: mappedStart, modifiedEnd: mappedEnd }
    : { originalStart: mappedStart, originalEnd: mappedEnd, modifiedStart: selectedStart, modifiedEnd: selectedEnd }
}

function fileLines(text: string): string[] {
  if (!text) return []
  const lines = text.split('\n')
  if (text.endsWith('\n')) lines.pop()
  return lines
}

/**
 * Build a headerless Git patch for a hunk or selected replacement lines.
 * Coordinates are based on the side Git will apply to, accounting for only
 * selected changes. The server supplies trusted file headers and applies
 * context-free hunks with --unidiff-zero.
 */
export function buildMonacoChangePatch(
  original: string,
  modified: string,
  change: MonacoChangeRange,
  operation: 'stage' | 'discard' | 'unstage' = 'stage',
): string | null {
  const { additionalChanges, ...first } = change
  let delta = 0
  const patches = [first, ...(additionalChanges ?? [])].map((item) => {
    const result = buildHunk(original, modified, item, operation, delta)
    if (result) delta += result.delta
    return result?.patch
  }).filter((patch): patch is string => Boolean(patch))
  return patches.length ? patches.join('') : null
}

function buildHunk(
  original: string,
  modified: string,
  change: MonacoChangeRange,
  operation: 'stage' | 'discard' | 'unstage',
  previousDelta: number,
): { patch: string; delta: number } | null {
  const range = selectedRange(change)
  if (!range) return null
  const oldLines = fileLines(original)
  const newLines = fileLines(modified)
  const oldStart = Math.min(oldLines.length + 1, range.originalStart)
  const oldEnd = Math.min(oldLines.length + 1, range.originalEnd)
  const newStart = Math.min(newLines.length + 1, range.modifiedStart)
  const newEnd = Math.min(newLines.length + 1, range.modifiedEnd)
  const removed = oldLines.slice(oldStart - 1, oldEnd - 1)
  const added = newLines.slice(newStart - 1, newEnd - 1)
  if (!removed.length && !added.length) return null

  const oldNoNewline = removed.length > 0 && oldEnd === oldLines.length + 1 && !original.endsWith('\n')
  const newNoNewline = added.length > 0 && newEnd === newLines.length + 1 && !modified.endsWith('\n')
  if (removed.join('\n') === added.join('\n') && oldNoNewline === newNoNewline) return null

  const oldCoordinate = operation === 'stage' ? oldStart : newStart - previousDelta
  const newCoordinate = operation === 'stage' ? oldStart + previousDelta : newStart
  const lines = [`@@ -${removed.length ? oldCoordinate : oldCoordinate - 1},${removed.length} +${added.length ? newCoordinate : newCoordinate - 1},${added.length} @@`]
  removed.forEach((line, index) => {
    lines.push(`-${line}`)
    if (oldNoNewline && index === removed.length - 1) lines.push('\\ No newline at end of file')
  })
  added.forEach((line, index) => {
    lines.push(`+${line}`)
    if (newNoNewline && index === added.length - 1) lines.push('\\ No newline at end of file')
  })
  return { patch: `${lines.join('\n')}\n`, delta: added.length - removed.length }
}
