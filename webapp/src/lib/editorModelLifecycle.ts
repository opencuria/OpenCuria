/** Keep editor state independent of the lazily loaded Monaco runtime. */
type ModelDisposalListener = (modelKey: string) => void

const listeners = new Set<ModelDisposalListener>()

export function onEditorModelDisposal(listener: ModelDisposalListener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function disposeEditorModel(modelKey: string): void {
  for (const listener of listeners) listener(modelKey)
}
