import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import DropdownMenu from './DropdownMenu.vue'
import DropdownMenuContent from './DropdownMenuContent.vue'
import DropdownMenuItem from './DropdownMenuItem.vue'
import DropdownMenuTrigger from './DropdownMenuTrigger.vue'

describe('DropdownMenuContent', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('portals above the desktop overlay z-index', async () => {
    const wrapper = mount(
      {
        components: {
          DropdownMenu,
          DropdownMenuContent,
          DropdownMenuItem,
          DropdownMenuTrigger,
        },
        template: `
          <DropdownMenu :open="true">
            <DropdownMenuTrigger>Open</DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem>Choice</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        `,
      },
      { attachTo: document.body },
    )
    await nextTick()

    const content = document.querySelector('[data-slot="dropdown-menu-content"]')
    expect(content).toBeTruthy()
    expect(content?.className).toContain('z-(--z-floating)')
    wrapper.unmount()
  })
})
