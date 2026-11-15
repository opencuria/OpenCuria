<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import type { editor, IDisposable } from 'monaco-editor/editor.js'
import { Plus, Minus, Undo2, ChevronUp, ChevronDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  getMonacoModel,
  getMonacoViewState,
  loadMonaco,
  monacoEditorOptions,
  saveMonacoViewState,
  updateMonacoModel,
  type MonacoRuntime,
} from '@/lib/monaco'
import {
  monacoChangeRange,
  monacoSelectionOverlaps,
  type MonacoChangeRange,
  type MonacoLineSelection,
} from '@/lib/monacoChanges'

const props = withDefaults(defineProps<{
  modelValue: string
  original?: string
  path: string
  modelKey?: string
  readOnly?: boolean
  diff?: boolean
  staged?: boolean
  gitActionsEnabled?: boolean
  busy?: boolean
}>(), {
  original: '',
  modelKey: undefined,
  readOnly: false,
  diff: false,
  staged: false,
  gitActionsEnabled: false,
  busy: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  save: []
  stage: [change: MonacoChangeRange]
  discard: [change: MonacoChangeRange]
  selection: [selection: MonacoLineSelection | null]
}>()

const host = ref<HTMLDivElement>()
const surface = ref<HTMLDivElement>()
const isMobile = useMediaQuery('(max-width: 767px)')
const gutterOptions = computed(() => ({
  // Diff actions use the modified editor's glyph margin.
  glyphMargin: props.diff || !isMobile.value,
  lineNumbersMinChars: isMobile.value ? 2 : 5,
}))
const loading = ref(true)
const error = ref<string | null>(null)
const changes = ref<MonacoChangeRange[]>([])
const selection = ref<MonacoLineSelection | null>(null)
const actionPositions = ref<{ index: number; top: number; left: number }[]>([])
const selectedChanges = computed(() => selection.value
  ? changes.value.filter((change) => monacoSelectionOverlaps(change, selection.value!))
  : [])

let monaco: MonacoRuntime | undefined
let textEditor: editor.IStandaloneCodeEditor | undefined
let diffEditor: editor.IStandaloneDiffEditor | undefined
let subscriptions: IDisposable[] = []
let currentKey = ''
let currentDiff = false
let synchronizing = false
let disposed = false
let frame: number | undefined

function modifiedEditor(): editor.IStandaloneCodeEditor | undefined {
  return diffEditor?.getModifiedEditor() ?? textEditor
}

function rememberView(): void {
  const state = diffEditor?.saveViewState() ?? textEditor?.saveViewState()
  if (currentKey && state) saveMonacoViewState(`${currentKey}:${currentDiff ? 'diff' : 'text'}`, state)
}

function clearEditor(): void {
  rememberView()
  for (const subscription of subscriptions) subscription.dispose()
  subscriptions = []
  textEditor?.dispose()
  diffEditor?.dispose()
  textEditor = undefined
  diffEditor = undefined
  changes.value = []
  selection.value = null
  actionPositions.value = []
}

function refreshPositions(): void {
  if (frame !== undefined) return
  frame = requestAnimationFrame(() => {
    frame = undefined
    const modified = diffEditor?.getModifiedEditor()
    if (!modified || !host.value) return
    const container = modified.getDomNode()
    const model = modified.getModel()
    if (!container || !model) return
    const hostBounds = host.value.getBoundingClientRect()
    const editorBounds = container.getBoundingClientRect()
    const left = editorBounds.left - hostBounds.left + 2
    actionPositions.value = changes.value.flatMap((change, index) => {
      const line = Math.max(1, Math.min(model.getLineCount(), change.modifiedStart))
      const top = editorBounds.top - hostBounds.top + modified.getTopForLineNumber(line) - modified.getScrollTop()
      return top >= editorBounds.top - hostBounds.top && top + 22 < hostBounds.height
        ? [{ index, top, left }] : []
    })
  })
}

function trackSelection(codeEditor: editor.IStandaloneCodeEditor, side: MonacoLineSelection['side']): void {
  subscriptions.push(codeEditor.onDidChangeCursorSelection((event) => {
    const range = event.selection
    selection.value = range.isEmpty() ? null : {
      side,
      startLine: range.startLineNumber,
      endLine: range.endLineNumber + (range.endColumn === 1 ? 0 : 1),
    }
    emit('selection', selection.value)
  }))
}

function action(kind: 'stage' | 'discard', change: MonacoChangeRange): void {
  if (!props.gitActionsEnabled || props.busy) return
  if (kind === 'stage') emit('stage', change)
  else if (!props.staged) emit('discard', change)
}

function selectedAction(kind: 'stage' | 'discard'): void {
  if (!selection.value || !selectedChanges.value.length) return
  const selected = selectedChanges.value.map((change) => ({ ...change, selection: selection.value! }))
  action(kind, { ...selected[0]!, additionalChanges: selected.slice(1) })
}

function configureEditor(): void {
  if (!surface.value || !monaco || disposed) return
  clearEditor()
  currentKey = props.modelKey ?? props.path
  currentDiff = props.diff
  synchronizing = true
  const model = getMonacoModel(monaco, currentKey, props.path, props.modelValue)
  const options = { ...monacoEditorOptions, ...gutterOptions.value, readOnly: props.readOnly, ariaLabel: props.path }
  if (props.diff) {
    diffEditor = monaco.editor.createDiffEditor(surface.value, {
      ...options,
      originalEditable: false,
      renderSideBySide: true,
      useInlineViewWhenSpaceIsLimited: true,
      renderIndicators: true,
      renderGutterMenu: false,
      renderMarginRevertIcon: false,
      enableSplitViewResizing: true,
      ignoreTrimWhitespace: false,
      diffAlgorithm: 'advanced',
    })
    diffEditor.setModel({
      original: getMonacoModel(monaco, `${currentKey}:original`, props.path, props.original),
      modified: model,
    })
    const saved = getMonacoViewState(`${currentKey}:diff`)
    if (saved && 'original' in saved) diffEditor.restoreViewState(saved)
    subscriptions.push(diffEditor.onDidUpdateDiff(() => {
      changes.value = (diffEditor?.getLineChanges() ?? []).map(monacoChangeRange)
      refreshPositions()
    }))
    const original = diffEditor.getOriginalEditor()
    const modified = diffEditor.getModifiedEditor()
    trackSelection(original, 'original')
    trackSelection(modified, 'modified')
    subscriptions.push(modified.onDidScrollChange(refreshPositions))
    subscriptions.push(modified.onDidLayoutChange(refreshPositions))
  } else {
    textEditor = monaco.editor.create(surface.value, { ...options, model })
    const saved = getMonacoViewState(`${currentKey}:text`)
    if (saved && 'viewState' in saved) textEditor.restoreViewState(saved)
    trackSelection(textEditor, 'modified')
  }
  const editable = modifiedEditor()!
  subscriptions.push(editable.onDidChangeModelContent(() => {
    if (!synchronizing) emit('update:modelValue', editable.getModel()!.getValue(undefined, true))
  }))
  subscriptions.push(editable.addAction({
    id: 'opencuria.save',
    label: 'Save File',
    keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
    contextMenuGroupId: 'file',
    run: () => { if (!props.readOnly) emit('save') },
  }))
  if (diffEditor) {
    for (const [id, label, kind] of [
      ['opencuria.stage-selection', props.staged ? 'Unstage Selected Lines' : 'Stage Selected Lines', 'stage'],
      ['opencuria.discard-selection', 'Revert Selected Lines', 'discard'],
    ] as const) {
      subscriptions.push(editable.addAction({
        id,
        label,
        contextMenuGroupId: 'git',
        run: () => selectedAction(kind),
      }))
    }
  }
  synchronizing = false
  loading.value = false
  editable.focus()
}

onMounted(async () => {
  try {
    monaco = await loadMonaco()
    configureEditor()
  } catch (cause) {
    if (!disposed) {
      error.value = cause instanceof Error ? cause.message : 'Failed to load editor'
      loading.value = false
    }
  }
})

watch(() => [props.modelKey, props.path, props.diff], () => configureEditor())
watch(() => props.modelValue, (value) => {
  const model = modifiedEditor()?.getModel()
  if (!model) return
  synchronizing = true
  updateMonacoModel(model, value)
  synchronizing = false
})
watch(() => props.original, (value) => {
  const model = diffEditor?.getOriginalEditor().getModel()
  if (model) updateMonacoModel(model, value)
})
watch(() => props.readOnly, (readOnly) => {
  textEditor?.updateOptions({ readOnly })
  diffEditor?.updateOptions({ readOnly })
})
watch(gutterOptions, (options) => {
  textEditor?.updateOptions(options)
  diffEditor?.updateOptions(options)
})
onBeforeUnmount(() => {
  disposed = true
  if (frame !== undefined) cancelAnimationFrame(frame)
  clearEditor()
})

defineExpose({ focus: () => modifiedEditor()?.focus() })
</script>

<template>
  <div ref="host" class="relative h-full min-h-0 w-full overflow-hidden bg-background" data-testid="monaco-editor">
    <div ref="surface" class="h-full w-full" />
    <div v-if="loading" class="absolute inset-0 flex items-center justify-center bg-background text-xs text-muted-foreground" role="status">
      Loading editor…
    </div>
    <div v-else-if="error" class="absolute inset-0 flex items-center justify-center bg-background p-4 text-sm text-destructive" role="alert">
      {{ error }}
    </div>

    <template v-if="diff && !loading && !error">
      <div class="absolute right-5 top-2 z-20 flex items-center gap-0.5 rounded border border-border bg-background/95 shadow-sm">
        <template v-if="gitActionsEnabled && selectedChanges.length">
          <Button
            variant="ghost" size="sm" class="h-7 gap-1 px-2 text-xs" :disabled="busy"
            :title="staged ? 'Unstage selected lines' : 'Stage selected lines'"
            @click="selectedAction('stage')"
          >
            <Minus v-if="staged" class="size-3.5" /><Plus v-else class="size-3.5" />
            {{ staged ? 'Unstage selection' : 'Stage selection' }}
          </Button>
          <Button
            v-if="!staged" variant="ghost" size="icon" class="size-7" :disabled="busy"
            title="Revert selected lines" aria-label="Revert selected lines" @click="selectedAction('discard')"
          ><Undo2 class="size-3.5" /></Button>
          <span class="mx-0.5 h-4 w-px bg-border" />
        </template>
        <Button variant="ghost" size="icon" class="size-7" :disabled="!changes.length" title="Previous change" aria-label="Previous change" @click="diffEditor?.goToDiff('previous')">
          <ChevronUp class="size-3.5" />
        </Button>
        <Button variant="ghost" size="icon" class="size-7" :disabled="!changes.length" title="Next change" aria-label="Next change" @click="diffEditor?.goToDiff('next')">
          <ChevronDown class="size-3.5" />
        </Button>
      </div>
      <template v-if="gitActionsEnabled">
        <div
          v-for="position in actionPositions" :key="position.index"
          class="absolute z-10 flex items-center rounded border border-border bg-background shadow-sm"
          :style="{ top: `${position.top}px`, left: `${position.left}px` }"
        >
          <Button
            variant="ghost" size="icon" class="size-5 rounded-sm" :disabled="busy"
            :title="staged ? 'Unstage change' : 'Stage change'" :aria-label="staged ? 'Unstage change' : 'Stage change'"
            @click="action('stage', changes[position.index]!)"
          ><Minus v-if="staged" class="size-3.5" /><Plus v-else class="size-3.5" /></Button>
          <Button
            v-if="!staged" variant="ghost" size="icon" class="size-5 rounded-sm" :disabled="busy"
            title="Revert change" aria-label="Revert change" @click="action('discard', changes[position.index]!)"
          ><Undo2 class="size-3.5" /></Button>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
:deep(.monaco-editor .context-view),
:deep(.monaco-editor .monaco-hover) {
  z-index: 60;
}
</style>
