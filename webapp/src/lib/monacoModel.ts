import type { editor, IRange } from 'monaco-editor/editor.js'

type EditableModel = Pick<editor.ITextModel,
  'getValue' | 'setValue' | 'pushStackElement' | 'pushEditOperations' | 'pushEOL'
> & { getFullModelRange: () => IRange }

/** External reloads are undoable; v-model echoes leave the history intact. */
export function updateMonacoModel(model: EditableModel, value: string): void {
  const current = model.getValue(undefined, true)
  if (current === value) return
  const hasBOM = value.startsWith('\uFEFF')
  if (current.startsWith('\uFEFF') !== hasBOM) {
    // Monaco stores the BOM outside the editable range and has no public BOM
    // setter. An explicit encoding change therefore needs a fresh model value.
    model.setValue(value)
    return
  }
  model.pushStackElement()
  model.pushEditOperations([], [{
    range: model.getFullModelRange(),
    text: hasBOM ? value.slice(1) : value,
  }], () => null)
  if (value.includes('\n')) model.pushEOL(value.includes('\r\n') ? 1 : 0)
  model.pushStackElement()
}
