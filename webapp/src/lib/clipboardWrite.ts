/**
 * Browser-only writer, independent of UI/stores. Call directly in the user
 * event before unrelated awaits. Once a native write starts it cannot be
 * cancelled; an abort only prevents fallback and stale success feedback.
 */
export async function writeClipboardText(
  text: string,
  signal?: AbortSignal,
  context: Window = window,
): Promise<boolean> {
  if (signal?.aborted) return false
  try {
    if (typeof context.navigator.clipboard?.writeText === 'function') {
      await context.navigator.clipboard.writeText(text)
      return !signal?.aborted
    }
  } catch {
    // A denied modern write may still allow a legacy write. Otherwise the UI
    // supplies a fresh gesture instead of claiming success after an await.
  }
  return !signal?.aborted && copyWithExecCommand(text, context.document)
}

/** Legacy HTTP path: no focus transfer outside a menu/dialog's focus scope. */
export function copyWithExecCommand(text: string, document: Document = window.document): boolean {
  if (typeof document === 'undefined' || !document.body) return false
  const selection = document.getSelection()
  if (!selection || typeof document.execCommand !== 'function') return false

  const ranges = Array.from({ length: selection.rangeCount }, (_, index) =>
    selection.getRangeAt(index).cloneRange(),
  )
  const span = document.createElement('span')
  span.textContent = text
  span.style.cssText = 'position:fixed;top:0;left:0;opacity:0;white-space:pre;pointer-events:none;'
  let payloadSet = false
  const onCopy = (event: ClipboardEvent): void => {
    if (!event.clipboardData) return
    event.clipboardData.setData('text/plain', text)
    event.preventDefault()
    // Our one-shot handler owns only this command, not subsequent native copy.
    event.stopImmediatePropagation()
    payloadSet = true
  }
  try {
    document.body.appendChild(span)
    const range = document.createRange()
    range.selectNodeContents(span)
    selection.removeAllRanges()
    selection.addRange(range)
    document.addEventListener('copy', onCopy, true)
    // Some browsers report true without copying the selected content. Require
    // the copy event and explicit payload delivery as well as command success.
    return document.execCommand('copy') && payloadSet
  } catch {
    return false
  } finally {
    document.removeEventListener('copy', onCopy, true)
    span.remove()
    selection.removeAllRanges()
    for (const range of ranges) {
      if (range.commonAncestorContainer.isConnected) selection.addRange(range)
    }
  }
}
