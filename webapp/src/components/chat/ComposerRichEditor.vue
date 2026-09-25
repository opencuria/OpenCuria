<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { parseComposerSegments, removeComposerToken, type ComposerToken } from '@/lib/composerTokens'
import { workspaceFileIconUrl } from '@/lib/fileIconAssets'

const props = withDefaults(defineProps<{
  modelValue: string
  disabled?: boolean
  placeholder?: string
}>(), { disabled: false, placeholder: '' })
const emit = defineEmits<{
  'update:modelValue': [value: string]
  input: []
  keydown: [event: KeyboardEvent]
}>()
const editor = ref<HTMLElement | null>(null)
const MAX_HEIGHT = 200

// The editor's DOM is an atomic rendering of the wire-format string. Browser
// editing is read back into the same string after every input, including IME.
function nodeText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? ''
  if (!(node instanceof HTMLElement)) return ''
  if (node.dataset.mentionRaw) return node.dataset.mentionRaw
  if (node.tagName === 'BR') return '\n'
  let result = ''
  for (const child of Array.from(node.childNodes)) result += nodeText(child)
  if (node !== editor.value && (node.tagName === 'DIV' || node.tagName === 'P')) result += '\n'
  return result
}

function value(): string {
  const el = editor.value
  if (!el) return props.modelValue
  return Array.from(el.childNodes).map((node) => nodeText(node)).join('')
}

function offsetFor(node: Node, offset: number): number {
  const el = editor.value
  if (!el) return 0
  if (node === el) {
    return Array.from(el.childNodes).slice(0, offset).reduce((sum, child) => sum + nodeText(child).length, 0)
  }
  let position = 0
  for (const child of Array.from(el.childNodes)) {
    if (child === node) return position + (child.nodeType === Node.TEXT_NODE ? offset : nodeText(child).length)
    if (child.contains(node)) return position + nodeText(child).length
    position += nodeText(child).length
  }
  return position
}

function cursor(): number {
  const selection = window.getSelection()
  if (!selection?.rangeCount || !editor.value?.contains(selection.anchorNode)) return value().length
  return offsetFor(selection.anchorNode!, selection.anchorOffset)
}

function insertNewline(): void {
  const selection = window.getSelection()
  const el = editor.value
  if (!el || !selection?.rangeCount || !el.contains(selection.anchorNode)) return
  const range = selection.getRangeAt(0)
  const start = offsetFor(range.startContainer, range.startOffset)
  const end = offsetFor(range.endContainer, range.endOffset)
  const text = `${value().slice(0, start)}\n${value().slice(end)}`
  render(text, start + 1)
  emit('update:modelValue', text)
  emit('input')
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && event.shiftKey && !event.isComposing) {
    event.preventDefault()
    insertNewline()
    return
  }
  emit('keydown', event)
}

function setCursor(offset: number): void {
  const el = editor.value
  if (!el) return
  let remaining = Math.max(0, offset)
  const range = document.createRange()
  for (const node of Array.from(el.childNodes)) {
    const length = nodeText(node).length
    if (remaining <= length) {
      if (node.nodeType === Node.TEXT_NODE) range.setStart(node, remaining)
      else if (remaining === 0) range.setStartBefore(node)
      else range.setStartAfter(node)
      range.collapse(true)
      const selection = window.getSelection()
      selection?.removeAllRanges()
      selection?.addRange(range)
      return
    }
    remaining -= length
  }
  range.selectNodeContents(el)
  range.collapse(false)
  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
}

function focus(atEnd = false): void {
  editor.value?.focus()
  if (atEnd) setCursor(value().length)
}

function resize(): void {
  const el = editor.value
  if (!el) return
  if (!value()) {
    el.style.height = ''
    el.style.overflowY = ''
    return
  }
  el.style.height = 'auto'
  const height = el.scrollHeight
  if (!height) return
  el.style.height = `${Math.min(height, MAX_HEIGHT)}px`
  el.style.overflowY = height > MAX_HEIGHT ? 'auto' : 'hidden'
}

function chip(token: ComposerToken): HTMLElement {
  const badge = document.createElement('span')
  badge.dataset.mentionRaw = token.raw
  badge.dataset.testid = token.kind === 'file' ? 'composer-file-badge' : 'composer-agent-badge'
  badge.setAttribute('contenteditable', 'false')
  badge.className = 'mx-0.5 inline-flex max-w-[min(100%,24rem)] items-center gap-1 align-baseline rounded-lg border border-border bg-muted/70 px-1.5 py-0.5 text-sm font-medium text-foreground'
  badge.title = token.kind === 'file' ? token.path : token.raw
  const icon = document.createElement('span')
  icon.className = 'group/icon relative inline-flex size-4 shrink-0 items-center justify-center'
  const remove = document.createElement('button')
  remove.type = 'button'
  remove.tabIndex = 0
  remove.dataset.testid = 'composer-badge-remove'
  remove.className = 'absolute inset-0 flex items-center justify-center rounded-sm text-muted-foreground opacity-0 transition-opacity group-hover/icon:opacity-100 hover:text-foreground focus:opacity-100'
  remove.setAttribute('aria-label', `Remove ${token.name} mention`)
  remove.textContent = '×'
  remove.addEventListener('mousedown', (event) => event.preventDefault())
  remove.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    const parts = parseComposerSegments(value())
    // Determine the offset in the current DOM (which may have changed since creation).
    let start = 0
    for (const sibling of Array.from(editor.value?.childNodes ?? [])) {
      if (sibling === badge) break
      start += nodeText(sibling).length
    }
    const current = parts.find((part) => part.kind !== 'text' && part.start === start && part.raw === token.raw)
    if (!current) return
    const next = removeComposerToken(value(), current.start, current.end)
    emit('update:modelValue', next.text)
    render(next.text, next.cursor)
    emit('input')
    focus()
    setCursor(next.cursor)
  })
  if (token.kind === 'file') {
    const img = document.createElement('img')
    img.dataset.testid = 'composer-badge-icon'
    img.src = workspaceFileIconUrl(token.path)
    img.alt = ''
    img.setAttribute('aria-hidden', 'true')
    img.className = 'size-4 shrink-0 object-contain transition-opacity group-hover/icon:opacity-0'
    icon.append(img)
  } else {
    // Keep agent glyph visually consistent with the file glyph.
    const agent = document.createElement('span')
    agent.textContent = '✦'
    agent.setAttribute('aria-hidden', 'true')
    icon.append(agent)
    icon.classList.add('text-primary', 'group-hover/icon:text-transparent')
  }
  icon.append(remove)
  const label = document.createElement('span')
  label.textContent = token.kind === 'file' ? token.name : `@agent:${token.name}`
  label.className = 'truncate'
  badge.append(icon, label)
  return badge
}

function render(text: string, caret?: number): void {
  const el = editor.value
  if (!el) return
  const fragment = document.createDocumentFragment()
  for (const segment of parseComposerSegments(text)) {
    if (segment.kind === 'text') fragment.append(document.createTextNode(segment.text))
    else fragment.append(chip(segment))
  }
  el.replaceChildren(fragment)
  if (caret !== undefined && document.activeElement === el) setCursor(caret)
  resize()
}

function onInput(): void {
  if (!editor.value) return
  const offset = cursor()
  const text = value()
  // Leave ordinary browser edits in place (including native undo history).
  // Rebuild only when a token has just become complete or been deleted.
  if (!composing.value) {
    const tokens = parseComposerSegments(text).filter((part) => part.kind !== 'text')
    const badges = Array.from(editor.value.querySelectorAll<HTMLElement>('[data-mention-raw]'))
    if (tokens.length !== badges.length || tokens.some((token, index) => token.raw !== badges[index]?.dataset.mentionRaw)) {
      render(text, offset)
    } else {
      resize()
    }
  }
  emit('update:modelValue', text)
  emit('input')
}
const composing = ref(false)
function onCompositionEnd(): void {
  composing.value = false
  onInput()
}

function onPaste(event: ClipboardEvent): void {
  if (props.disabled) return
  const text = event.clipboardData?.getData('text/plain')
  if (text === undefined) return
  event.preventDefault()
  const selection = window.getSelection()
  if (!selection?.rangeCount) return
  const range = selection.getRangeAt(0)
  range.deleteContents()
  const node = document.createTextNode(text)
  range.insertNode(node)
  range.setStartAfter(node)
  range.collapse(true)
  selection.removeAllRanges()
  selection.addRange(range)
  onInput()
}

function onCopy(event: ClipboardEvent): void {
  const selection = window.getSelection()
  if (!selection?.rangeCount || !editor.value?.contains(selection.anchorNode)) return
  const fragment = selection.getRangeAt(0).cloneContents()
  event.clipboardData?.setData('text/plain', Array.from(fragment.childNodes).map((node) => nodeText(node)).join(''))
  event.preventDefault()
}

function onCut(event: ClipboardEvent): void {
  if (props.disabled) return
  onCopy(event)
  const selection = window.getSelection()
  if (!selection?.rangeCount || !editor.value?.contains(selection.anchorNode)) return
  selection.getRangeAt(0).deleteContents()
  onInput()
}

watch(() => props.modelValue, (text) => {
  if (!composing.value && text !== value()) render(text, document.activeElement === editor.value ? Math.min(cursor(), text.length) : undefined)
})
onMounted(() => { render(props.modelValue); void nextTick(resize) })
defineExpose({ focus, setCursor, cursor, value, resize, el: editor })
</script>

<template>
  <div class="relative">
    <div
      ref="editor"
      data-testid="composer-textarea"
      role="textbox"
      aria-label="Chat prompt"
      aria-multiline="true"
      :aria-disabled="disabled"
      :contenteditable="disabled ? 'false' : 'true'"
      :data-placeholder="placeholder"
      class="min-h-10 max-h-[200px] w-full overflow-y-auto whitespace-pre-wrap break-words px-4 py-2 text-base outline-none empty:before:pointer-events-none empty:before:text-muted-foreground empty:before:content-[attr(data-placeholder)] md:min-h-9"
      @input="onInput"
      @keydown="onKeydown"
      @paste="onPaste"
      @copy="onCopy"
      @cut="onCut"
      @compositionstart="composing = true"
      @compositionend="onCompositionEnd"
    />
  </div>
</template>
