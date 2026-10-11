import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { toast } from 'vue-sonner'

import FileContextMenu from './FileContextMenu.vue'
import { useClipboardStore } from '@/stores/clipboard'
import type { FileNode } from '@/types'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

const node: FileNode = {
  name: 'notes.txt',
  path: '/workspace/notes.txt',
  type: 'file',
  size: 12,
}

function mountMenu() {
  return mount(FileContextMenu, {
    props: { node, x: 12, y: 24 },
    attachTo: document.body,
  })
}

describe('FileContextMenu copy path', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('emits the copied path and closes only after clipboard copy succeeds', async () => {
    let resolveWrite!: () => void
    const writeText = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveWrite = resolve
        }),
    )
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = mountMenu()

    document.querySelector<HTMLButtonElement>('[data-testid="file-context-copy-path"]')!.click()
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(node.path)
    expect(wrapper.emitted('copyPath')).toBeUndefined()
    expect(wrapper.emitted('close')).toBeUndefined()
    expect(document.querySelector('[data-testid="file-context-copy-path"]')).not.toBeNull()

    resolveWrite()
    await flushPromises()

    expect(wrapper.emitted('copyPath')).toEqual([[node.path]])
    expect(wrapper.emitted('close')).toEqual([[]])
    expect(toast.info).toHaveBeenCalledWith('Copied file path', { duration: 5000 })
    wrapper.unmount()
  })

  it('does not emit a copied path when the manual-copy request is dismissed', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('blocked')) },
    })
    const wrapper = mountMenu()
    const clipboard = useClipboardStore()

    document.querySelector<HTMLButtonElement>('[data-testid="file-context-copy-path"]')!.click()
    await vi.waitFor(() => expect(clipboard.request?.text).toBe(node.path))
    expect(clipboard.request?.label).toBe('file path')

    const dialog = document.createElement('div')
    dialog.setAttribute('data-clipboard-dialog', '')
    document.body.append(dialog)
    dialog.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(wrapper.emitted('close')).toBeUndefined()
    expect(document.querySelector('[data-testid="file-context-copy-path"]')).not.toBeNull()

    clipboard.finish()
    await flushPromises()

    expect(wrapper.emitted('copyPath')).toBeUndefined()
    expect(wrapper.emitted('close')).toEqual([[]])
    wrapper.unmount()
  })

  it.each(['outside click', 'Escape'])(
    'dismisses a pending browser write via %s without stale success',
    async (action) => {
      let resolveWrite!: () => void
      Object.assign(navigator, {
        clipboard: {
          writeText: vi.fn(
            () =>
              new Promise<void>((resolve) => {
                resolveWrite = resolve
              }),
          ),
        },
      })
      const wrapper = mountMenu()
      document.querySelector<HTMLButtonElement>('[data-testid="file-context-copy-path"]')!.click()
      if (action === 'Escape')
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      else document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(wrapper.emitted('close')).toEqual([[]])
      resolveWrite()
      await flushPromises()
      expect(wrapper.emitted('copyPath')).toBeUndefined()
      expect(toast.info).not.toHaveBeenCalled()
      wrapper.unmount()
    },
  )
})
