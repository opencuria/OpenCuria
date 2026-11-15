import type * as Monaco from 'monaco-editor/editor.js'
import { onEditorModelDisposal } from './editorModelLifecycle'
import { updateMonacoModel } from './monacoModel'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?monaco-worker'
import JsonWorker from 'monaco-editor/languages/features/json/json.worker.js?monaco-worker'
import CssWorker from 'monaco-editor/languages/features/css/css.worker.js?monaco-worker'
import HtmlWorker from 'monaco-editor/languages/features/html/html.worker.js?monaco-worker'
import TypeScriptWorker from 'monaco-editor/languages/features/typescript/ts.worker.js?monaco-worker'

export type MonacoRuntime = typeof Monaco
export { updateMonacoModel } from './monacoModel'

let runtime: Promise<MonacoRuntime> | undefined
const models = new Map<string, Monaco.editor.ITextModel>()
const viewStates = new Map<string, Monaco.editor.ICodeEditorViewState | Monaco.editor.IDiffEditorViewState>()

function themeColor(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return /^#[\da-f]{6}$/i.test(value) ? value : fallback
}

function applyTheme(monaco: MonacoRuntime): void {
  const dark = document.documentElement.classList.contains('dark')
  const background = themeColor('--background', dark ? '#131316' : '#fafaf9')
  const foreground = themeColor('--foreground', dark ? '#fafafa' : '#18181b')
  const muted = themeColor('--muted-foreground', dark ? '#a1a1aa' : '#6f6f78')
  const border = themeColor('--border', dark ? '#2a2a2e' : '#e4e4e7')
  const card = themeColor('--popover', dark ? '#232329' : '#ffffff')
  const accent = themeColor('--ring', '#8b85e8')
  monaco.editor.defineTheme('opencuria', {
    base: dark ? 'vs-dark' : 'vs',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': background,
      'editor.foreground': foreground,
      'editorGutter.background': background,
      'editorLineNumber.foreground': muted,
      'editorLineNumber.activeForeground': foreground,
      'editorCursor.foreground': accent,
      'editor.selectionBackground': `${accent}40`,
      'editor.inactiveSelectionBackground': `${accent}20`,
      'editor.lineHighlightBackground': dark ? '#ffffff06' : '#00000003',
      'editorIndentGuide.background1': border,
      'editorWhitespace.foreground': `${muted}60`,
      'editorWidget.background': card,
      'editorWidget.border': border,
      'editorSuggestWidget.background': card,
      'editorSuggestWidget.border': border,
      'editorSuggestWidget.foreground': foreground,
      'editorHoverWidget.background': card,
      'editorHoverWidget.border': border,
      'dropdown.background': card,
      'dropdown.border': border,
      'input.background': background,
      'input.foreground': foreground,
      'input.border': border,
      'focusBorder': accent,
      'diffEditor.border': border,
      'diffEditor.insertedTextBackground': dark ? '#4ade8026' : '#15803d20',
      'diffEditor.removedTextBackground': dark ? '#f8717126' : '#dc262620',
      'diffEditor.insertedLineBackground': dark ? '#4ade8010' : '#15803d10',
      'diffEditor.removedLineBackground': dark ? '#f8717110' : '#dc262610',
      'scrollbarSlider.background': `${muted}35`,
      'scrollbarSlider.hoverBackground': `${muted}55`,
      'scrollbarSlider.activeBackground': `${muted}75`,
    },
  })
  monaco.editor.setTheme('opencuria')
}

/** Load VS Code's editor only when a file is opened. Workers stay local to Vite. */
export function loadMonaco(): Promise<MonacoRuntime> {
  if (!runtime) {
    self.MonacoEnvironment = {
      getWorker(_moduleId, label) {
        if (label === 'json') return new JsonWorker()
        if (['css', 'scss', 'less'].includes(label)) return new CssWorker()
        if (['html', 'handlebars', 'razor'].includes(label)) return new HtmlWorker()
        if (['typescript', 'javascript'].includes(label)) return new TypeScriptWorker()
        return new EditorWorker()
      },
    }
    runtime = import('./monacoRuntime').then((monaco) => {
      applyTheme(monaco)
      const observer = new MutationObserver(() => applyTheme(monaco))
      observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] })
      return monaco
    }).catch((error: unknown) => {
      runtime = undefined
      throw error
    })
  }
  return runtime
}

function languageForPath(monaco: MonacoRuntime, path: string): string {
  const filename = path.split('/').pop() ?? path
  const extension = filename.includes('.') ? `.${filename.split('.').pop()?.toLowerCase()}` : ''
  // Monaco's HTML tokenizer supports embedded script/style sections in these
  // single-file component formats, which have no bundled language definition.
  if (['.vue', '.svelte'].includes(extension)) return 'html'
  if (extension === '.toml' || filename === '.env' || filename.startsWith('.env.')) return 'ini'
  const language = monaco.languages.getLanguages().find((entry) =>
    entry.filenames?.includes(filename) || (extension && entry.extensions?.includes(extension)),
  )
  return language?.id ?? 'plaintext'
}

export function getMonacoModel(
  monaco: MonacoRuntime,
  key: string,
  path: string,
  value: string,
): Monaco.editor.ITextModel {
  let model = models.get(key)
  if (!model || model.isDisposed()) {
    const uri = monaco.Uri.from({ scheme: 'opencuria', authority: 'editor', path: `/${encodeURIComponent(key)}/${path.replace(/^\/+/, '')}` })
    model = monaco.editor.createModel(value, languageForPath(monaco, path), uri)
    models.set(key, model)
  } else {
    updateMonacoModel(model, value)
  }
  return model
}

export function saveMonacoViewState(
  key: string,
  state: Monaco.editor.ICodeEditorViewState | Monaco.editor.IDiffEditorViewState | null,
): void {
  if (state) viewStates.set(key, state)
}

export function getMonacoViewState(key: string) {
  return viewStates.get(key)
}

/** Closing a tab must dispose its retained text, original model and view states. */
export function disposeMonacoModel(key: string): void {
  for (const modelKey of [key, `${key}:original`]) {
    models.get(modelKey)?.dispose()
    models.delete(modelKey)
  }
  viewStates.delete(`${key}:text`)
  viewStates.delete(`${key}:diff`)
}

export function disposeMonacoModels(prefix?: string): void {
  for (const key of [...models.keys()]) {
    if (!prefix || key.startsWith(prefix)) disposeMonacoModel(key)
  }
}

const stopModelDisposalListener = onEditorModelDisposal(disposeMonacoModel)
if (import.meta.hot) import.meta.hot.dispose(stopModelDisposalListener)

export const monacoEditorOptions: Monaco.editor.IStandaloneEditorConstructionOptions = {
  automaticLayout: true,
  autoDetectHighContrast: false,
  fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace",
  fontSize: 13,
  lineHeight: 21,
  tabSize: 2,
  padding: { top: 12, bottom: 20 },
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  smoothScrolling: true,
  renderWhitespace: 'selection',
  wordWrap: 'off',
  fixedOverflowWidgets: true,
  theme: 'opencuria',
}
