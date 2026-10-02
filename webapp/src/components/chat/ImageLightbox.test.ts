import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'

import ImageLightbox from './ImageLightbox.vue'

let wrapper: VueWrapper
const disconnect = vi.fn()

async function mountLightbox(): Promise<HTMLElement> {
  wrapper = mount(ImageLightbox, {
    props: { src: 'data:image/png;base64,abc', alt: 'Chat photo' },
    attachTo: document.body,
  })
  await nextTick()
  await nextTick()
  return document.body.querySelector<HTMLElement>('[role="dialog"]')!
}

function button(name: string): HTMLButtonElement {
  return document.body.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)!
}

async function key(dialog: HTMLElement, value: string): Promise<void> {
  dialog.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true }))
  await nextTick()
}

describe('ImageLightbox', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe = vi.fn()
        disconnect = disconnect
      },
    )
    disconnect.mockClear()
  })

  afterEach(() => {
    wrapper?.unmount()
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
  })

  it('renders an accessible modal with the original image and named controls', async () => {
    const dialog = await mountLightbox()

    expect(dialog).not.toBeNull()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(document.getElementById(dialog.getAttribute('aria-labelledby')!)?.textContent).toBe(
      'Image Viewer',
    )
    expect(dialog.querySelector('img')?.getAttribute('alt')).toBe('Chat photo')
    expect(dialog.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,abc')
    expect(button('Zoom out').disabled).toBe(true)
    expect(button('Zoom in').disabled).toBe(false)
    expect(button('Close image preview')).not.toBeNull()
  })

  it('zooms with controls and keyboard, respects limits, and resets', async () => {
    const dialog = await mountLightbox()
    button('Zoom in').click()
    await nextTick()
    expect(dialog.textContent).toContain('120%')
    await key(dialog, '+')
    expect(dialog.textContent).toContain('144%')
    await key(dialog, '-')
    expect(dialog.textContent).toContain('120%')
    for (let i = 0; i < 20; i++) await key(dialog, '+')
    expect(dialog.textContent).toContain('600%')
    expect(button('Zoom in').disabled).toBe(true)
    button('Reset view').click()
    await nextTick()
    expect(dialog.textContent).toContain('100%')
    await key(dialog, '-')
    expect(dialog.textContent).toContain('100%')
    await key(dialog, '+')
    await key(dialog, '0')
    expect(dialog.textContent).toContain('100%')
  })

  it('emits close from the close button and Escape', async () => {
    const dialog = await mountLightbox()
    button('Close image preview').click()
    await nextTick()
    expect(wrapper.emitted('close')).toHaveLength(1)
    await key(dialog, 'Escape')
    expect(wrapper.emitted('close')).toHaveLength(2)
  })

  it('shows an image failure without trapping the user or leaving enabled zoom controls', async () => {
    const dialog = await mountLightbox()
    dialog.querySelector('img')!.dispatchEvent(new Event('error'))
    await nextTick()

    expect(dialog.querySelector('[role="status"]')?.textContent).toContain(
      'Image could not be loaded.',
    )
    expect(button('Zoom in').disabled).toBe(true)
    expect(button('Zoom out').disabled).toBe(true)
    expect(button('Reset view').disabled).toBe(true)
    expect(button('Close image preview').disabled).toBe(false)
    await key(dialog, 'Escape')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('disconnects stage observation on unmount and ignores keys outside the modal', async () => {
    const dialog = await mountLightbox()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: '+', bubbles: true }))
    await nextTick()
    expect(dialog.textContent).toContain('100%')
    wrapper.unmount()
    expect(disconnect).toHaveBeenCalled()
  })
})
