import { describe, expect, it, vi } from 'vitest'
import type { editor } from 'monaco-editor/editor.js'
import { updateMonacoModel } from './monacoModel'

/** Model adapter reflecting Monaco's separate BOM and EOL storage. */
function modelFor(initial: string) {
  let bom = initial.startsWith('\uFEFF')
  let eol = initial.includes('\r\n') ? '\r\n' : '\n'
  let content = initial.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n')
  const model = {
    getValue: vi.fn((_eol?: unknown, preserveBOM?: boolean) =>
      `${preserveBOM && bom ? '\uFEFF' : ''}${content.replace(/\n/g, eol)}`),
    setValue: vi.fn((value: string) => {
      bom = value.startsWith('\uFEFF')
      content = value.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n')
      eol = value.includes('\r\n') ? '\r\n' : '\n'
    }),
    pushStackElement: vi.fn(),
    getFullModelRange: vi.fn(() => ({ startLineNumber: 1, startColumn: 1, endLineNumber: 2, endColumn: 1 })),
    pushEditOperations: vi.fn((_selections: unknown, edits: editor.IIdentifiedSingleEditOperation[]) => {
      content = edits[0]!.text!.replace(/\r\n/g, '\n')
      return null
    }),
    pushEOL: vi.fn((sequence: number) => { eol = sequence === 1 ? '\r\n' : '\n' }),
  }
  return model
}

describe('Monaco model encoding and external reloads', () => {
  it('does not modify a BOM-bearing buffer or its undo stack on a v-model echo', () => {
    const model = modelFor('\uFEFFbefore\r\n')
    updateMonacoModel(model, '\uFEFFbefore\r\n')
    expect(model.getValue).toHaveBeenCalledWith(undefined, true)
    expect(model.pushEditOperations).not.toHaveBeenCalled()
    expect(model.pushStackElement).not.toHaveBeenCalled()
  })

  it('keeps exactly one BOM when replacing a document through an undoable edit', () => {
    const model = modelFor('\uFEFFbefore\n')
    updateMonacoModel(model, '\uFEFFafter\n')
    expect(model.getValue(undefined, true)).toBe('\uFEFFafter\n')
    expect(model.setValue).not.toHaveBeenCalled()
    expect(model.pushStackElement).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['before\r\n', 'after\n'],
    ['before\n', 'after\r\n'],
  ])('adopts disk EOL changes from %j to %j', (initial, incoming) => {
    const model = modelFor(initial)
    updateMonacoModel(model, incoming)
    expect(model.getValue(undefined, true)).toBe(incoming)
    expect(model.setValue).not.toHaveBeenCalled()
  })

  it('handles an explicit BOM encoding change without duplicating editable content', () => {
    const model = modelFor('before\n')
    updateMonacoModel(model, '\uFEFFafter\n')
    expect(model.getValue(undefined, true)).toBe('\uFEFFafter\n')
    expect(model.setValue).toHaveBeenCalledOnce()
    expect(model.pushEditOperations).not.toHaveBeenCalled()
  })
})
