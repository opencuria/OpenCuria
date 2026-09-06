import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'

import HarnessDesktopMini from './HarnessDesktopMini.vue'
import { useDesktopStore } from '@/stores/desktop'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import * as workspacesApi from '@/services/workspaces.api'

vi.mock('@/services/workspaces.api', () => ({
  getDesktopStatus: vi.fn(),
}))

vi.mock('@/services/config', () => ({
  getConfig: () => ({ apiBaseUrl: '', wsBaseUrl: 'http://ws.test' }),
}))

const getDesktopStatus = vi.mocked(workspacesApi.getDesktopStatus)

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
    vi.clearAllMocks()
    localStorage.setItem('kern_access_token', 'tok')
    getDesktopStatus.mockResolvedValue({
      active: true,
      proxy_url: '/ws/desktop/ws-1/',
      viewer_held: false,
      computer_use_active: true,
    })
  })

  it('loads the live iframe from desktop status', async () => {
    const wrapper = mountMini()
    await flushPromises()

    expect(getDesktopStatus).toHaveBeenCalledWith('ws-1')
    const iframe = wrapper.get('iframe')
    expect(iframe.attributes('src')).toContain('/ws/desktop/ws-1/')
    expect(iframe.attributes('src')).toContain('token=tok')
    expect(wrapper.text()).toContain('LIVE')
  })

  it('opens the full desktop on click', async () => {
    const store = useDesktopStore()
    const wrapper = mountMini()
    await flushPromises()

    await wrapper.get('[data-testid="harness-desktop-mini"]').trigger('click')
    expect(store.isOpen).toBe(true)
    expect(store.isMinimized).toBe(false)
  })

  it('hides the preview while the full desktop is open', async () => {
    const store = useDesktopStore()
    store.open()
    const wrapper = mountMini()
    await flushPromises()

    expect(wrapper.find('[data-testid="harness-desktop-mini"]').exists()).toBe(false)
    expect(getDesktopStatus).not.toHaveBeenCalled()
  })

  it('uses the store proxy url when already connected', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    const wrapper = mountMini()
    await flushPromises()

    expect(getDesktopStatus).not.toHaveBeenCalled()
    expect(wrapper.get('iframe').attributes('src')).toContain('/ws/desktop/ws-1/')
  })
})
