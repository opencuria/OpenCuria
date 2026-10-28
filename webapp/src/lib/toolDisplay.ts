/** Web-specific Lucide icon mapping with shared pure tool display helpers. */

import type { Component } from 'vue'
import {
  FilePenLine,
  FileText,
  FolderOpen,
  Globe,
  Image,
  BookText,
  Lightbulb,
  ListTodo,
  MessageCircleQuestion,
  Monitor,
  Network,
  Search,
  Terminal,
  Wrench,
} from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import { isComputerUseTool, resolveToolName } from '@opencuria/chat-core/toolDisplay'

export {
  computerUseSummary,
  isComputerUseTool,
  hydrateHarnessPart,
  parseToolArguments,
  resolveToolName,
  stringArg,
  toolDisplayLabel,
  truncatePreview,
} from '@opencuria/chat-core/toolDisplay'

/** Lucide icon for a tool or reasoning row. */
export function toolDisplayIcon(part: HarnessPart): Component {
  if (isComputerUseTool(part)) return Monitor
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
    case 'memory':
      return BookText
    case 'image_generation':
      return Image
    case 'websearch':
      return Search
    case 'tool_call':
      return Wrench
    case 'webfetch':
      return Globe
    case 'todowrite':
      return ListTodo
    case 'question':
      return MessageCircleQuestion
    default:
      return Wrench
  }
}
