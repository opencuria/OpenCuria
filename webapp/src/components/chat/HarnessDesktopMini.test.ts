import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'

import HarnessDesktopMini from './HarnessDesktopMini.vue'
import { useDesktopStore } from '@/stores/desktop'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'

function mountMini() {
  return mount(HarnessDesktopMini, {
    global: {
      provide: {
        [harnessWorkspaceIdKey as symbol]: ref('ws-1'),
      },
    },
  })
}

describe('HarnessDesktopMini', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.setItem('kern_access_token', 'tok')
  })

  it('never mounts a second KasmVNC iframe', () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    const wrapper = mountMini()

    expect(wrapper.find('iframe').exists()).toBe(false)
  })

  it('shows LIVE when the persistent surface is connected for this workspace', () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    const wrapper = mountMini()

    expect(wrapper.text()).toContain('LIVE')
    expect(wrapper.text()).toContain('Open live desktop')
  })

  it('shows Connecting while the persistent surface connects', () => {
    const store = useDesktopStore()
    store.setConnecting('ws-1')
    const wrapper = mountMini()

    expect(wrapper.text()).toContain('Connecting to desktop…')
    expect(wrapper.find('iframe').exists()).toBe(false)
  })

  it('ignores sessions from other workspaces', () => {
    const store = useDesktopStore()
    store.setConnected('ws-other', '/ws/desktop/ws-other/')
    const wrapper = mountMini()

    expect(wrapper.text()).not.toContain('LIVE')
    expect(wrapper.text()).toContain('Open desktop')
    expect(wrapper.find('iframe').exists()).toBe(false)
  })

  it('opens the full desktop on click', async () => {
    const store = useDesktopStore()
    const wrapper = mountMini()

    await wrapper.get('[data-testid="harness-desktop-mini"]').trigger('click')
    expect(store.isOpen).toBe(true)
  })

  it('hides the preview while the full desktop is open', () => {
    const store = useDesktopStore()
    store.open()
    const wrapper = mountMini()

    expect(wrapper.find('[data-testid="harness-desktop-mini"]').exists()).toBe(false)
  })

  it('keeps the desktop aspect ratio without an iframe', () => {
    const wrapper = mountMini()

    const frame = wrapper.get('[data-testid="harness-desktop-mini"] > div')
    expect(frame.attributes('style')).toContain('1920 / 1080')
  })
})
