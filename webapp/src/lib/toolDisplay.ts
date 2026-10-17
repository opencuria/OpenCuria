/** Web-specific Lucide icon mapping with shared pure tool display helpers. */

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
import { resolveToolName } from '@opencuria/chat-core/toolDisplay'

export {
  computerUseSummary,
  hydrateHarnessPart,
  parseToolArguments,
  resolveToolName,
  stringArg,
  toolDisplayLabel,
  truncatePreview,
} from '@opencuria/chat-core/toolDisplay'

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
