/** Trusted native clipboard gestures for a same-origin KasmVNC canvas. */
export function bindDesktopClipboard(
  frame: HTMLIFrameElement,
  actions: {
    enabled: () => boolean
    blocked?: () => boolean
    copy: (context: Window) => void
    copySelectionDuringGesture?: (context: Window, shift: boolean) => void
    paste: (text: string) => Promise<boolean>
  },
): () => void {
  const doc = frame.contentDocument
  const win = frame.contentWindow as (Window & typeof globalThis) | null
  if (!doc || !win || !doc.body) return () => {}
  const input = doc.createElement('textarea')
  input.setAttribute('aria-label', 'Desktop clipboard input')
  input.tabIndex = -1
  input.style.cssText =
    'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none;'
  doc.body.appendChild(input)
  let replaying = false
  let disposed = false
  let pasteTarget: HTMLElement | null = null
  let pasteShift = false
  let focusTimer: ReturnType<typeof setTimeout> | undefined
  const remoteTarget = (event: Event): boolean => {
    const target = event.target as HTMLElement | null
    // Kasm's IME textarea is remote keyboard input, not a local editor.
    return (
      target === input ||
      target?.id === 'noVNC_keyboardinput' ||
      !target?.closest?.('input, textarea, [contenteditable="true"]')
    )
  }
  const eligible = (event: Event): boolean => !disposed && actions.enabled() && remoteTarget(event)
  const block = (event: Event): boolean => {
    if (!disposed && actions.blocked?.() && remoteTarget(event)) {
      suppress(event)
      return true
    }
    return false
  }
  const shortcut = (event: KeyboardEvent): 'c' | 'v' | null => {
    if (!(event.metaKey || event.ctrlKey) || event.altKey) return null
    const key = event.key.toLowerCase()
    return key === 'c' || key === 'v' ? key : null
  }
  const suppress = (event: Event, preventDefault = true): void => {
    if (preventDefault) event.preventDefault()
    event.stopImmediatePropagation()
  }
  const restoreFocus = (): void => {
    clearTimeout(focusTimer)
    if (doc.activeElement === input) pasteTarget?.focus({ preventScroll: true })
    input.value = ''
    pasteTarget = null
  }
  const dispatch = (target: HTMLElement, key: 'c' | 'v', shift: boolean): void => {
    const send = (type: string, key: string, code: string, ctrlKey: boolean, shiftKey = false) =>
      target.dispatchEvent(
        new win.KeyboardEvent(type, {
          key,
          code,
          ctrlKey,
          shiftKey,
          bubbles: true,
          cancelable: true,
        }),
      )
    replaying = true
    try {
      // Release host modifiers already forwarded by Kasm (Mac Meta maps to
      // Alt). Balanced Linux Control chords avoid stuck keys and double paste.
      for (const modifier of ['Meta', 'Control', 'Shift']) {
        send('keyup', modifier, `${modifier}Left`, false)
        send('keyup', modifier, `${modifier}Right`, false)
      }
      send('keydown', 'Control', 'ControlLeft', true)
      if (shift) send('keydown', 'Shift', 'ShiftLeft', true, true)
      send('keydown', key, `Key${key.toUpperCase()}`, true, shift)
      send('keyup', key, `Key${key.toUpperCase()}`, true, shift)
      if (shift) send('keyup', 'Shift', 'ShiftLeft', true)
      send('keyup', 'Control', 'ControlLeft', false)
    } finally {
      replaying = false
    }
  }
  const keydown = (event: KeyboardEvent): void => {
    if (replaying) return
    if (shortcut(event) && block(event)) return
    if (!eligible(event)) return
    const key = shortcut(event)
    if (!key) return
    if (event.repeat) {
      suppress(event)
      return
    }
    const target = (doc.activeElement ?? doc.body) as HTMLElement
    if (key === 'c') {
      suppress(event)
      // Start a promise-backed write in the focused iframe's trusted gesture,
      // then send the guest copy. A parent write loses Safari's focus/activation.
      if (
        typeof win.navigator.clipboard?.write !== 'function' &&
        actions.copySelectionDuringGesture
      ) {
        // The runner sends exactly one guest copy and returns acknowledged text
        // synchronously. Replaying C here too would race/duplicate that copy.
        replaying = true
        try {
          for (const modifier of ['Meta', 'Control', 'Shift'])
            for (const side of ['Left', 'Right'])
              target.dispatchEvent(
                new win.KeyboardEvent('keyup', {
                  key: modifier,
                  code: `${modifier}${side}`,
                  bubbles: true,
                }),
              )
        } finally {
          replaying = false
        }
        actions.copySelectionDuringGesture(win, event.shiftKey)
      } else {
        actions.copy(win)
        dispatch(target, 'c', event.shiftKey)
      }
    } else {
      // Native paste needs default processing and an editable target on Safari,
      // including HTTP where navigator.clipboard is completely absent.
      suppress(event, false)
      if (target !== input) pasteTarget = target
      pasteShift = event.shiftKey
      input.focus({ preventScroll: true })
      clearTimeout(focusTimer)
      focusTimer = setTimeout(restoreFocus, 1500)
    }
  }
  const keyup = (event: KeyboardEvent): void => {
    if (!replaying && shortcut(event)) {
      if (block(event)) return
      if (eligible(event)) suppress(event)
    }
  }
  const paste = (event: ClipboardEvent): void => {
    if (block(event) || !eligible(event)) return
    suppress(event)
    // DataTransfer must be read synchronously while this trusted event lives.
    const data = event.clipboardData
    const text = data?.getData('text/plain')
    // WebKit can expose native text while reporting an empty types list.
    // Still ignore image/file-only pastes rather than replaying stale VM data.
    const hasText =
      text !== undefined && (text !== '' || Array.from(data?.types ?? []).includes('text/plain'))
    const target =
      pasteTarget ?? doc.querySelector<HTMLElement>('#noVNC_container canvas, canvas[tabindex]')
    const shift = pasteShift
    restoreFocus()
    if (!hasText || text === undefined || !target) return
    void actions.paste(text).then((sent) => {
      if (sent && !disposed && actions.enabled() && target.isConnected) dispatch(target, 'v', shift)
    })
  }
  win.addEventListener('keydown', keydown, true)
  win.addEventListener('keyup', keyup, true)
  win.addEventListener('paste', paste, true)
  return () => {
    disposed = true
    win.removeEventListener('keydown', keydown, true)
    win.removeEventListener('keyup', keyup, true)
    win.removeEventListener('paste', paste, true)
    restoreFocus()
    input.remove()
  }
}

/** Wait for the actual guest clipboard event; bound identical-copy fallback. */
export function createDesktopClipboardReader(fallback: () => Promise<string>) {
  let pending: { receive: (text: string) => void; cancel: () => void } | null = null
  return {
    receive(text: string): void {
      pending?.receive(text)
    },
    cancel(): void {
      pending?.cancel()
    },
    read(): Promise<string> {
      pending?.cancel()
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          if (pending?.receive !== receive) return
          pending = null
          fallback().then(resolve, reject)
        }, 350)
        const receive = (text: string): void => {
          clearTimeout(timer)
          pending = null
          resolve(text)
        }
        pending = {
          receive,
          cancel: () => {
            clearTimeout(timer)
            pending = null
            reject(new Error('Desktop clipboard request cancelled'))
          },
        }
      })
    },
  }
}
