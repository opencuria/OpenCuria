/**
 * Parse unified diffs for compact chat file-change cards.
 *
 * Collapsed preview is capped at 4 lines (one-line edits keep a neighbor
 * above and below). Expanded view keeps two unchanged context lines at
 * each hunk edge and drops hunk headers.
 */

export type DiffLineType = 'context' | 'add' | 'del'

export interface FileDiffLine {
  type: DiffLineType
  oldNo: number | null
  newNo: number | null
  content: string
}

export interface FileDiffHunk {
  oldStart: number
  newStart: number
  lines: FileDiffLine[]
}

export interface ParsedFileDiff {
  additions: number
  deletions: number
  hunks: FileDiffHunk[]
  collapsed: FileDiffLine[]
  expanded: FileDiffLine[]
}

export const COLLAPSED_DIFF_LINES = 4
export const EXPANDED_CONTEXT_LINES = 2

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

/** Parse a unified diff into collapsed/expanded line windows and +/- counts. */
export function parseFileDiff(unified: string): ParsedFileDiff {
  const hunks = parseHunks(unified)
  const allLines = hunks.flatMap((hunk) => hunk.lines)
  let additions = 0
  let deletions = 0
  for (const line of allLines) {
    if (line.type === 'add') additions += 1
    if (line.type === 'del') deletions += 1
  }
  return {
    additions,
    deletions,
    hunks,
    collapsed: collapsedWindow(allLines),
    expanded: hunks.flatMap((hunk) => trimEdgeContext(hunk.lines, EXPANDED_CONTEXT_LINES)),
  }
}

function parseHunks(unified: string): FileDiffHunk[] {
  const raw = unified.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  if (!raw.trim()) return []

  const hunks: FileDiffHunk[] = []
  let current: FileDiffHunk | null = null
  let oldNo = 1
  let newNo = 1

  function startHunk(nextOld: number, nextNew: number): void {
    current = { oldStart: nextOld, newStart: nextNew, lines: [] }
    hunks.push(current)
    oldNo = nextOld
    newNo = nextNew
  }

  for (const rawLine of raw.split('\n')) {
    if (rawLine.startsWith('--- ') || rawLine.startsWith('+++ ')) continue
    if (rawLine.startsWith('\\')) continue

    const hunkMatch = HUNK_RE.exec(rawLine)
    if (hunkMatch) {
      startHunk(Number(hunkMatch[1]), Number(hunkMatch[3]))
      continue
    }

    if (!rawLine) continue
    const kind = lineKind(rawLine)
    if (!kind) continue
    if (!current) startHunk(1, 1)

    const content = rawLine.slice(1)
    if (kind === 'add') {
      current!.lines.push({ type: 'add', oldNo: null, newNo, content })
      newNo += 1
      continue
    }
    if (kind === 'del') {
      current!.lines.push({ type: 'del', oldNo, newNo: null, content })
      oldNo += 1
      continue
    }
    current!.lines.push({ type: 'context', oldNo, newNo, content })
    oldNo += 1
    newNo += 1
  }

  return hunks.filter((hunk) => hunk.lines.length > 0)
}

function lineKind(line: string): DiffLineType | null {
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  if (line.startsWith(' ') || line === '') return 'context'
  return null
}

function isSingleLineChange(lines: FileDiffLine[]): boolean {
  let adds = 0
  let dels = 0
  for (const line of lines) {
    if (line.type === 'add') adds += 1
    if (line.type === 'del') dels += 1
  }
  return adds + dels >= 1 && adds <= 1 && dels <= 1
}

function collapsedWindow(lines: FileDiffLine[]): FileDiffLine[] {
  if (lines.length <= COLLAPSED_DIFF_LINES) return lines

  const first = lines.findIndex((line) => line.type !== 'context')
  if (first < 0) return lines.slice(0, COLLAPSED_DIFF_LINES)

  if (isSingleLineChange(lines)) {
    let last = first
    for (let i = first; i < lines.length; i++) {
      if (lines[i]!.type !== 'context') last = i
    }
    const start = Math.max(0, first - 1)
    const end = Math.min(lines.length, last + 2)
    return lines.slice(start, end).slice(0, COLLAPSED_DIFF_LINES)
  }

  const start = first > 0 && lines[first - 1]?.type === 'context' ? first - 1 : first
  return lines.slice(start, start + COLLAPSED_DIFF_LINES)
}

function trimEdgeContext(lines: FileDiffLine[], keep: number): FileDiffLine[] {
  let start = 0
  while (start < lines.length && lines[start]?.type === 'context') start += 1
  const dropStart = Math.max(0, start - keep)

  let end = lines.length
  while (end > start && lines[end - 1]?.type === 'context') end -= 1
  const trailing = lines.length - end
  const dropEnd = Math.max(0, trailing - keep)

  return lines.slice(dropStart, lines.length - dropEnd)
}

/** Basename for a workspace or relative path. */
export function diffFileName(path: string): string {
  const trimmed = path.replace(/\/+$/, '')
  if (!trimmed) return 'file'
  const slash = trimmed.lastIndexOf('/')
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}
