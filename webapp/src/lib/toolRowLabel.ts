/** Split a tool row label into verb, object and path hint for typographic hierarchy. */

import type { HarnessPart } from '@/types/harness'
import { resolveToolName, toolDisplayLabel } from '@/lib/toolDisplay'

export interface ToolRowLabel {
  /** Action word (`Read`, `Grep`, `$`, `Thought`), rendered muted. */
  verb: string
  /** Subject of the action (file name, pattern, command), rendered prominent. */
  object: string
  /** Directory of a file path, rendered faint after the object. */
  hint: string
}

const PATH_TOOLS = new Set(['read', 'write', 'edit'])

function splitPath(path: string): { base: string; dir: string } {
  const trimmed = path.replace(/\/+$/, '')
  const slash = trimmed.lastIndexOf('/')
  if (slash < 0) return { base: trimmed, dir: '' }
  return { base: trimmed.slice(slash + 1), dir: trimmed.slice(0, slash) }
}

/**
 * Titles are `Verb object` (`Read src/a.ts`, `Grep foo`) or `$ command`.
 * Anything that does not match stays a single verb so unknown labels render
 * unchanged.
 */
export function toolRowLabel(part: HarnessPart): ToolRowLabel {
  const label = toolDisplayLabel(part).trim()
  if (part.type === 'reasoning') return { verb: label, object: '', hint: '' }

  if (label.startsWith('$ ')) return { verb: '$', object: label.slice(2), hint: '' }

  const match = /^([A-Z][A-Za-z]*)\s+(\S.*)$/.exec(label)
  if (!match) return { verb: label, object: '', hint: '' }
  const verb = match[1]!
  const object = match[2]!

  if (PATH_TOOLS.has(resolveToolName(part).toLowerCase()) && object.includes('/')) {
    const { base, dir } = splitPath(object)
    if (base) return { verb, object: base, hint: dir }
  }
  return { verb, object, hint: '' }
}
