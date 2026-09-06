import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import SettingsSheetHost from './SettingsSheetHost.vue'
import { OPEN_SETTINGS_EVENT } from './settingsTabs'

const replaceMock = vi.fn()
let currentQuery: Record<string, unknown> = {}

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/', query: currentQuery }),
  useRouter: () => ({
    replace: replaceMock,
    afterEach: vi.fn(() => () => {}),
  }),
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAdmin: true }),
}))

describe('SettingsSheetHost', () => {
  it('opens the sheet for ?settings=<tab> and removes the query', async () => {
    setActivePinia(createPinia())
    replaceMock.mockClear()
    currentQuery = { settings: 'provider' }
    const events: Array<{ tab?: string }> = []
    const listener = (e: Event) =>
      events.push((e as CustomEvent<{ tab?: string }>).detail ?? {})
    window.addEventListener(OPEN_SETTINGS_EVENT, listener)

    try {
      mount(SettingsSheetHost, {
        global: {
          stubs: {
            SettingsSheet: { template: '<div />' },
          },
        },
      })
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(events).toHaveLength(1)
      expect(events[0]).toEqual({ tab: 'provider' })
      expect(replaceMock).toHaveBeenCalledWith({ path: '/', query: {} })
    } finally {
      window.removeEventListener(OPEN_SETTINGS_EVENT, listener)
    }
  })
})
