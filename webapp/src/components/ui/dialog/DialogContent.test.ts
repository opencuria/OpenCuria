import { defineComponent, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './index'

const Harness = defineComponent({
  components: {
    Dialog,
    DialogBody,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
  },
  template: `
    <Dialog :open="true">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Title</DialogTitle>
          <DialogDescription>Description</DialogDescription>
        </DialogHeader>
        <DialogBody>Scrollable content</DialogBody>
        <DialogFooter><button type="button">OK</button></DialogFooter>
      </DialogContent>
    </Dialog>
  `,
})

const StackHarness = defineComponent({
  components: { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle },
  template: `
    <Dialog :open="true">
      <DialogContent class="z-(--z-desktop-modal)" overlay-class="z-(--z-desktop-modal)">
        <DialogHeader>
          <DialogTitle>Desktop</DialogTitle>
          <DialogDescription>Remote desktop.</DialogDescription>
        </DialogHeader>
      </DialogContent>
    </Dialog>
  `,
})

describe('DialogContent', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('lets a lower z-index replace the default dialog layer on content and overlay', async () => {
    mount(StackHarness, { attachTo: document.body })
    await nextTick()

    const content = document.body.querySelector('[data-slot="dialog-content"]')
    const overlay = document.body.querySelector('[data-slot="dialog-overlay"]')
    expect(content).not.toBeNull()
    expect(overlay).not.toBeNull()
    expect(content!.className).toContain('z-(--z-desktop-modal)')
    expect(content!.className).not.toContain('z-50')
    expect(overlay!.className).toContain('z-(--z-desktop-modal)')
    expect(overlay!.className).not.toContain('z-50')
  })

  it('caps the dialog height to the viewport and clips overflow', async () => {
    mount(Harness, { attachTo: document.body })
    await nextTick()

    const content = document.body.querySelector('[data-slot="dialog-content"]')
    expect(content).not.toBeNull()
    expect(content!.className).toContain('max-h-[calc(100dvh-2rem)]')
    expect(content!.className).toContain('overflow-hidden')
    expect(content!.className).toContain('flex-col')
  })

  it('renders a scrollable body region between header and footer', async () => {
    mount(Harness, { attachTo: document.body })
    await nextTick()

    const body = document.body.querySelector('[data-slot="dialog-body"]')
    expect(body).not.toBeNull()
    expect(body!.className).toContain('overflow-y-auto')
    expect(body!.className).toContain('min-h-0')
    expect(body!.textContent).toContain('Scrollable content')
  })
})
