import { afterEach, describe, expect, it, vi } from 'vitest'

import { bindDesktopClipboard, createDesktopClipboardReader } from './desktopClipboard'

function createFrame() {
  const frame = document.createElement('iframe')
  document.body.appendChild(frame)
  const doc = frame.contentDocument!
  doc.open()
  doc.write('<!doctype html><html><body><canvas tabindex="0"></canvas></body></html>')
  doc.close()
  const canvas = doc.querySelector('canvas')!
  canvas.focus()
  return {
    frame,
    doc,
    win: frame.contentWindow as Window & typeof globalThis,
    canvas,
  }
}

function keyboard(
  win: Window & typeof globalThis,
  target: EventTarget,
  type: 'keydown' | 'keyup',
  options: KeyboardEventInit,
): KeyboardEvent {
  const event = new win.KeyboardEvent(type, { bubbles: true, cancelable: true, ...options })
  target.dispatchEvent(event)
  return event
}

function pasteEvent(
  win: Window & typeof globalThis,
  target: EventTarget,
  text: string,
  types: string[] = ['text/plain'],
) {
  const event = new win.Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clipboardData', {
    value: { getData: vi.fn(() => text), types },
  })
  target.dispatchEvent(event)
  return event
}

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('bindDesktopClipboard', () => {
  it.each([
    ['Control', { ctrlKey: true }],
    ['Meta', { metaKey: true }],
  ] as const)('forwards %s+C as a balanced guest Control chord', (_name, modifier) => {
    const { frame, doc, win, canvas } = createFrame()
    const copy = vi.fn()
    const paste = vi.fn(async () => true)
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy,
      paste,
    })
    const forwarded: Array<{ type: string; key: string; code: string; ctrl: boolean }> = []
    canvas.addEventListener('keydown', (event) => {
      const key = event as KeyboardEvent
      forwarded.push({ type: 'keydown', key: key.key, code: key.code, ctrl: key.ctrlKey })
    })
    canvas.addEventListener('keyup', (event) => {
      const key = event as KeyboardEvent
      forwarded.push({ type: 'keyup', key: key.key, code: key.code, ctrl: key.ctrlKey })
    })

    const original = keyboard(win, canvas, 'keydown', { key: 'c', ...modifier })

    expect(original.defaultPrevented).toBe(true)
    expect(copy).toHaveBeenCalledOnce()
    expect(copy).toHaveBeenCalledWith(win)
    expect(forwarded).toEqual([
      ...['Meta', 'Control', 'Shift'].flatMap((key) => [
        { type: 'keyup', key, code: `${key}Left`, ctrl: false },
        { type: 'keyup', key, code: `${key}Right`, ctrl: false },
      ]),
      { type: 'keydown', key: 'Control', code: 'ControlLeft', ctrl: true },
      { type: 'keydown', key: 'c', code: 'KeyC', ctrl: true },
      { type: 'keyup', key: 'c', code: 'KeyC', ctrl: true },
      { type: 'keyup', key: 'Control', code: 'ControlLeft', ctrl: false },
    ])
    cleanup()
    expect(doc.querySelector('[aria-label="Desktop clipboard input"]')).toBeNull()
  })

  it('uses synchronous HTTP selection copy once, releases Mac modifiers and does not replay guest C', () => {
    const { frame, doc, win, canvas } = createFrame()
    Object.defineProperty(win.navigator, 'clipboard', { configurable: true, value: undefined })
    const copy = vi.fn()
    const copySelectionDuringGesture = vi.fn()
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy,
      copySelectionDuringGesture,
      paste: async () => true,
    })
    const forwarded: Array<[string, string, string, boolean]> = []
    canvas.addEventListener('keydown', (event) => {
      const key = event as KeyboardEvent
      forwarded.push(['keydown', key.key, key.code, key.ctrlKey])
    })
    canvas.addEventListener('keyup', (event) => {
      const key = event as KeyboardEvent
      forwarded.push(['keyup', key.key, key.code, key.ctrlKey])
    })

    const original = keyboard(win, canvas, 'keydown', {
      key: 'c',
      metaKey: true,
      shiftKey: true,
    })

    expect(original.defaultPrevented).toBe(true)
    expect(copySelectionDuringGesture).toHaveBeenCalledOnce()
    expect(copySelectionDuringGesture).toHaveBeenCalledWith(win, true)
    expect(copy).not.toHaveBeenCalled()
    expect(forwarded).toEqual(
      ['Meta', 'Control', 'Shift'].flatMap((key) => [
        ['keyup', key, `${key}Left`, false],
        ['keyup', key, `${key}Right`, false],
      ]),
    )
    cleanup()
    expect(doc.querySelector('[aria-label="Desktop clipboard input"]')).toBeNull()
  })

  it('keeps the secure-context async clipboard path unchanged', () => {
    const { frame, win, canvas } = createFrame()
    const clipboard = { write: vi.fn() }
    Object.defineProperty(win.navigator, 'clipboard', { configurable: true, value: clipboard })
    const copy = vi.fn()
    const copySelectionDuringGesture = vi.fn()
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy,
      copySelectionDuringGesture,
      paste: async () => true,
    })

    keyboard(win, canvas, 'keydown', { key: 'c', ctrlKey: true })

    expect(copy).toHaveBeenCalledOnce()
    expect(copy).toHaveBeenCalledWith(win)
    expect(copySelectionDuringGesture).not.toHaveBeenCalled()
    cleanup()
  })

  it('suppresses repeat shortcuts without copying or forwarding another chord', () => {
    const { frame, win, canvas } = createFrame()
    const copy = vi.fn()
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy,
      paste: async () => true,
    })
    const forwarded: string[] = []
    canvas.addEventListener('keydown', (key) => forwarded.push((key as KeyboardEvent).key))
    canvas.addEventListener('keyup', (key) => forwarded.push((key as KeyboardEvent).key))
    const event = keyboard(win, canvas, 'keydown', { key: 'c', ctrlKey: true, repeat: true })

    expect(event.defaultPrevented).toBe(true)
    expect(copy).not.toHaveBeenCalled()
    expect(forwarded).toEqual([])
    cleanup()
  })

  it('blocks Ctrl/Cmd clipboard traffic while disabled, including native paste', () => {
    const { frame, doc, win, canvas } = createFrame()
    const copy = vi.fn()
    const paste = vi.fn(async () => true)
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => false,
      blocked: () => true,
      copy,
      paste,
    })
    const input = doc.querySelector<HTMLTextAreaElement>('[aria-label="Desktop clipboard input"]')!
    const receivedByCanvas: string[] = []
    const receivedByInput: string[] = []
    for (const type of ['keydown', 'keyup', 'paste'] as const) {
      canvas.addEventListener(type, () => receivedByCanvas.push(type))
      input.addEventListener(type, () => receivedByInput.push(type))
    }

    for (const target of [canvas, input]) {
      target.focus()
      for (const [key, modifier] of [
        ['c', { ctrlKey: true }],
        ['v', { metaKey: true }],
      ] as const) {
        const down = keyboard(win, target, 'keydown', { key, ...modifier })
        const up = keyboard(win, target, 'keyup', { key, ...modifier })
        expect(down.defaultPrevented).toBe(true)
        expect(up.defaultPrevented).toBe(true)
      }
      const nativePaste = pasteEvent(win, target, 'blocked text')
      expect(nativePaste.defaultPrevented).toBe(true)
      expect(doc.activeElement).toBe(target)
    }

    expect(doc.activeElement).toBe(input)
    expect(receivedByCanvas).toEqual([])
    expect(receivedByInput).toEqual([])
    expect(copy).not.toHaveBeenCalled()
    expect(paste).not.toHaveBeenCalled()
    cleanup()
  })

  it('blocks a second guest copy chord while the first clipboard copy is busy', () => {
    const { frame, win, canvas } = createFrame()
    let busy = false
    const copy = vi.fn(() => {
      busy = true
    })
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      blocked: () => busy,
      copy,
      paste: async () => true,
    })
    const guestCopies: string[] = []
    canvas.addEventListener('keydown', (event) => {
      const key = event as KeyboardEvent
      if (key.key === 'c' && key.ctrlKey) guestCopies.push(key.key)
    })

    const first = keyboard(win, canvas, 'keydown', { key: 'c', ctrlKey: true })
    const firstGuestCopyCount = guestCopies.length
    const second = keyboard(win, canvas, 'keydown', { key: 'c', ctrlKey: true })

    expect(first.defaultPrevented).toBe(true)
    expect(second.defaultPrevented).toBe(true)
    expect(firstGuestCopyCount).toBe(1)
    expect(guestCopies).toHaveLength(firstGuestCopyCount)
    expect(copy).toHaveBeenCalledOnce()
    cleanup()
  })

  it('accepts WebKit native paste text even when clipboard types is empty', async () => {
    const { frame, doc, win, canvas } = createFrame()
    const paste = vi.fn(async () => true)
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy: vi.fn(),
      paste,
    })
    const forwarded: KeyboardEvent[] = []
    canvas.addEventListener('keydown', (event) => forwarded.push(event as KeyboardEvent))

    const shortcut = keyboard(win, canvas, 'keydown', { key: 'v', ctrlKey: true })
    const input = doc.querySelector<HTMLTextAreaElement>('[aria-label="Desktop clipboard input"]')!
    expect(shortcut.defaultPrevented).toBe(false)
    expect(doc.activeElement).toBe(input)
    pasteEvent(win, input, 'native clipboard text', [])
    await Promise.resolve()
    await Promise.resolve()

    expect(paste).toHaveBeenCalledWith('native clipboard text')
    expect(doc.activeElement).toBe(canvas)
    expect(forwarded.map(({ key, ctrlKey }) => [key, ctrlKey])).toEqual([
      ['Control', true],
      ['v', true],
    ])
    cleanup()
  })

  it('forwards Shift+Ctrl+V to the terminal only after VM paste succeeds', async () => {
    const { frame, doc, win, canvas } = createFrame()
    const paste = vi.fn(async () => true)
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy: vi.fn(),
      paste,
    })
    const forwarded: Array<[string, string, boolean, boolean]> = []
    for (const type of ['keydown', 'keyup'] as const) {
      canvas.addEventListener(type, (event) => {
        const key = event as KeyboardEvent
        forwarded.push([type, key.key, key.ctrlKey, key.shiftKey])
      })
    }

    keyboard(win, canvas, 'keydown', { key: 'v', ctrlKey: true, shiftKey: true })
    const input = doc.querySelector<HTMLTextAreaElement>('[aria-label="Desktop clipboard input"]')!
    pasteEvent(win, input, 'terminal text')
    await Promise.resolve()
    await Promise.resolve()

    expect(paste).toHaveBeenCalledWith('terminal text')
    expect(forwarded).toContainEqual(['keydown', 'Shift', true, true])
    expect(forwarded).toContainEqual(['keydown', 'v', true, true])
    expect(forwarded).toContainEqual(['keyup', 'v', true, true])
    expect(forwarded).toContainEqual(['keyup', 'Shift', true, false])
    expect(forwarded[forwarded.length - 1]).toEqual(['keyup', 'Control', false, false])
    cleanup()
  })

  it('excludes local editors but permits Kasm remote keyboard input', () => {
    const { frame, doc, win, canvas } = createFrame()
    const copy = vi.fn()
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy,
      paste: async () => true,
    })
    const editors: HTMLElement[] = [
      doc.createElement('input'),
      doc.createElement('textarea'),
      doc.createElement('div'),
    ]
    editors[2]!.setAttribute('contenteditable', 'true')
    doc.body.append(...editors)
    for (const editor of editors) {
      keyboard(win, editor, 'keydown', { key: 'c', ctrlKey: true })
    }
    expect(copy).not.toHaveBeenCalled()

    const kasmInput = doc.createElement('textarea')
    kasmInput.id = 'noVNC_keyboardinput'
    doc.body.appendChild(kasmInput)
    keyboard(win, kasmInput, 'keydown', { key: 'c', ctrlKey: true })
    expect(copy).toHaveBeenCalledOnce()
    expect(copy).toHaveBeenCalledWith(win)
    cleanup()
    expect(canvas.isConnected).toBe(true)
  })

  it('never forwards a failed or disposed native paste and restores focus on cleanup', async () => {
    const { frame, doc, win, canvas } = createFrame()
    const failedPaste = vi.fn(async () => false)
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy: vi.fn(),
      paste: failedPaste,
    })
    const forwarded: string[] = []
    canvas.addEventListener('keydown', (event) => forwarded.push((event as KeyboardEvent).key))
    keyboard(win, canvas, 'keydown', { key: 'v', ctrlKey: true })
    const input = doc.querySelector<HTMLTextAreaElement>('[aria-label="Desktop clipboard input"]')!
    pasteEvent(win, input, 'rejected')
    await Promise.resolve()
    await Promise.resolve()
    expect(failedPaste).toHaveBeenCalledOnce()
    expect(forwarded).toEqual([])

    keyboard(win, canvas, 'keydown', { key: 'v', ctrlKey: true })
    cleanup()
    expect(doc.activeElement).toBe(canvas)
    expect(input.isConnected).toBe(false)
    pasteEvent(win, input, 'after disposal')
    await Promise.resolve()
    expect(failedPaste).toHaveBeenCalledOnce()
    expect(forwarded).toEqual([])
  })

  it('does not forward a VM paste that resolves after disposal', async () => {
    const { frame, doc, win, canvas } = createFrame()
    let finishPaste!: (sent: boolean) => void
    const paste = vi.fn(() => new Promise<boolean>((resolve) => (finishPaste = resolve)))
    const cleanup = bindDesktopClipboard(frame, {
      enabled: () => true,
      copy: vi.fn(),
      paste,
    })
    const forwarded: string[] = []
    canvas.addEventListener('keydown', (event) => forwarded.push((event as KeyboardEvent).key))

    keyboard(win, canvas, 'keydown', { key: 'v', ctrlKey: true })
    const input = doc.querySelector<HTMLTextAreaElement>('[aria-label="Desktop clipboard input"]')!
    pasteEvent(win, input, 'late success')
    expect(paste).toHaveBeenCalledOnce()
    cleanup()
    finishPaste(true)
    await Promise.resolve()
    await Promise.resolve()
    expect(forwarded).toEqual([])
  })
})

describe('createDesktopClipboardReader', () => {
  afterEach(() => vi.useRealTimers())

  it('resolves from the clipboard event before querying the fallback', async () => {
    vi.useFakeTimers()
    const fallback = vi.fn(async () => 'REST fallback')
    const reader = createDesktopClipboardReader(fallback)
    const result = reader.read()

    reader.receive('guest clipboard event')

    await expect(result).resolves.toBe('guest clipboard event')
    await vi.advanceTimersByTimeAsync(400)
    expect(fallback).not.toHaveBeenCalled()
  })

  it('uses fallback after the bounded event wait expires', async () => {
    vi.useFakeTimers()
    const fallback = vi.fn(async () => 'REST clipboard')
    const reader = createDesktopClipboardReader(fallback)
    const result = reader.read()

    await vi.advanceTimersByTimeAsync(350)
    await expect(result).resolves.toBe('REST clipboard')
    expect(fallback).toHaveBeenCalledOnce()
  })

  it('cancels pending requests without starting the fallback', async () => {
    vi.useFakeTimers()
    const fallback = vi.fn(async () => 'must not be read')
    const reader = createDesktopClipboardReader(fallback)
    const result = reader.read()
    reader.cancel()

    await expect(result).rejects.toThrow('Desktop clipboard request cancelled')
    await vi.advanceTimersByTimeAsync(400)
    expect(fallback).not.toHaveBeenCalled()
  })
})
