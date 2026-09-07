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

describe('DialogContent', () => {
  afterEach(() => {
    document.body.innerHTML = ''
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
