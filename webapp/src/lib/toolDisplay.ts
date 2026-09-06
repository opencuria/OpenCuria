/**
 * Per-tool chat display helpers: argument parsing, labels, and icons.
 */

import type { Component } from 'vue'
import {
  FilePenLine,
  FileText,
  FolderOpen,
  Globe,
  Keyboard,
  Lightbulb,
  ListTodo,
  MessageCircleQuestion,
  Monitor,
  MousePointer2,
  Network,
  Search,
  Terminal,
  Wrench,
} from '@lucide/vue'

import type { HarnessPart } from '@/types/harness'

const COMPUTER_USE_VIEW_TOOLS = new Set(['view_screen', 'view_region'])
const COMPUTER_USE_POINTER_TOOLS = new Set([
  'move_mouse',
  'left_click',
  'right_click',
  'middle_click',
  'double_click',
  'drag',
  'scroll',
])
const COMPUTER_USE_KEYBOARD_TOOLS = new Set(['type_text', 'press_key', 'wait'])

/** Parse `input.arguments` whether it is a JSON string or an object. */
export function parseToolArguments(part: HarnessPart): Record<string, unknown> {
  const raw = part.input?.['arguments']
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as Record<string, unknown>
  }
  if (typeof raw !== 'string' || !raw.trim()) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
  } catch {
    return {}
  }
  return {}
}

/** Tool id from `part.tool` or persisted `input.tool`. */
export function resolveToolName(part: HarnessPart): string {
  const named = (part.tool || '').trim()
  if (named) return named
  const fromInput = part.input?.['tool']
  if (typeof fromInput === 'string' && fromInput.trim()) return fromInput.trim()
  return ''
}

/** Copy a fetched part so `tool` is populated from `input.tool` after reload. */
export function hydrateHarnessPart(part: HarnessPart): HarnessPart {
  const tool = resolveToolName(part)
  if (tool && part.tool !== tool) {
    return { ...part, tool }
  }
  return part
}

/** String argument helper for display labels. */
export function stringArg(args: Record<string, unknown>, key: string): string {
  const value = args[key]
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

export function truncatePreview(text: string, max = 2000): string {
  if (text.length <= max) return text
  return `${text.slice(0, max)}\n…[truncated ${text.length} chars total]`
}

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, '')
  const slash = trimmed.lastIndexOf('/')
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}

function formatPoint(args: Record<string, unknown>, xKey: string, yKey: string): string {
  const x = args[xKey]
  const y = args[yKey]
  if (x == null || y == null) return ''
  return `(${x}, ${y})`
}

/** Compact one-line summary for computer-use tool arguments. */
export function computerUseSummary(part: HarnessPart): string {
  const tool = resolveToolName(part)
  const args = parseToolArguments(part)
  switch (tool) {
    case 'view_screen':
      return 'View screen'
    case 'view_region':
      return `View region (${args.left}, ${args.top})–(${args.right}, ${args.bottom})`
    case 'move_mouse':
      return `Move mouse ${formatPoint(args, 'x', 'y')}`.trim()
    case 'left_click':
      return `Left click ${formatPoint(args, 'x', 'y')}`.trim()
    case 'right_click':
      return `Right click ${formatPoint(args, 'x', 'y')}`.trim()
    case 'middle_click':
      return `Middle click ${formatPoint(args, 'x', 'y')}`.trim()
    case 'double_click':
      return `Double click ${formatPoint(args, 'x', 'y')}`.trim()
    case 'drag':
      return `Drag ${formatPoint(args, 'startX', 'startY')} → ${formatPoint(args, 'endX', 'endY')}`.trim()
    case 'scroll':
      return `Scroll ${stringArg(args, 'direction') || 'down'}`.trim()
    case 'type_text': {
      const text = stringArg(args, 'text')
      return text ? `Type “${text}”` : 'Type text'
    }
    case 'press_key':
      return `Press ${stringArg(args, 'key') || 'key'}`
    case 'open_url':
      return `Open ${stringArg(args, 'url') || 'URL'}`
    case 'wait': {
      const ms = stringArg(args, 'milliseconds')
      return ms ? `Wait ${ms}ms` : 'Wait'
    }
    case 'ask_user':
      return stringArg(args, 'question') || 'Ask user'
    default:
      return part.title || tool || 'Computer use'
  }
}

/** Row label: persisted title, then a tool-specific fallback from arguments. */
export function toolDisplayLabel(part: HarnessPart): string {
  if (part.type === 'reasoning') return 'Thought'
  if ((part.title || '').trim()) return part.title
  const tool = resolveToolName(part)
  const args = parseToolArguments(part)
  switch (tool) {
    case 'bash': {
      const command = stringArg(args, 'command').split('\n')[0] ?? ''
      return command ? `$ ${command}` : 'bash'
    }
    case 'read': {
      const path = stringArg(args, 'path')
      return path ? `Read ${basename(path)}` : 'read'
    }
    case 'write': {
      const path = stringArg(args, 'path')
      return path ? `Write ${basename(path)}` : 'write'
    }
    case 'edit': {
      const path = stringArg(args, 'path')
      return path ? `Edit ${basename(path)}` : 'edit'
    }
    case 'glob':
      return stringArg(args, 'pattern') ? `Glob ${stringArg(args, 'pattern')}` : 'glob'
    case 'grep':
      return stringArg(args, 'pattern') ? `Grep ${stringArg(args, 'pattern')}` : 'grep'
    case 'list': {
      const path = stringArg(args, 'path')
      return path ? `List ${path}` : 'list'
    }
    case 'webfetch':
      return stringArg(args, 'url') ? `Fetch ${stringArg(args, 'url')}` : 'webfetch'
    case 'todowrite':
      return 'Update todos'
    case 'question':
    case 'ask_user':
      return 'Question'
    case 'task':
      return 'Subagent'
    default:
      if (COMPUTER_USE_VIEW_TOOLS.has(tool) || COMPUTER_USE_POINTER_TOOLS.has(tool) || COMPUTER_USE_KEYBOARD_TOOLS.has(tool) || tool === 'open_url' || tool === 'ask_user') {
        return computerUseSummary(part)
      }
      return tool || 'tool'
  }
}

/** Lucide icon for a tool or reasoning row. */
export function toolDisplayIcon(part: HarnessPart): Component {
  if (part.type === 'reasoning') return Lightbulb
  const tool = resolveToolName(part).toLowerCase()
  switch (tool) {
    case 'bash':
      return Terminal
    case 'read':
      return FileText
    case 'write':
    case 'edit':
      return FilePenLine
    case 'glob':
    case 'grep':
      return Search
    case 'list':
      return FolderOpen
    case 'task':
      return Network
    case 'webfetch':
    case 'open_url':
      return Globe
    case 'todowrite':
      return ListTodo
    case 'question':
    case 'ask_user':
      return MessageCircleQuestion
    case 'type_text':
    case 'press_key':
    case 'wait':
      return Keyboard
    default:
      if (COMPUTER_USE_VIEW_TOOLS.has(tool)) return Monitor
      if (COMPUTER_USE_POINTER_TOOLS.has(tool)) return MousePointer2
      return Wrench
  }
}
