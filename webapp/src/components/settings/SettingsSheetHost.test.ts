import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import SettingsSheetHost from './SettingsSheetHost.vue'
import { OPEN_SETTINGS_EVENT } from './settingsTabs'

const mockState = vi.hoisted(() => ({
  replaceMock: vi.fn().mockResolvedValue(undefined),
  route: null as { path: string; query: Record<string, unknown> } | null,
}))

const replaceMock = mockState.replaceMock

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const route = reactive({ path: '/', query: {} as Record<string, unknown> })
  mockState.route = route
  return {
    useRoute: () => route,
    useRouter: () => ({
      replace: mockState.replaceMock,
    }),
  }
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAdmin: true }),
}))

function setQuery(query: Record<string, unknown>) {
  mockState.route!.query = query
}

async function flush() {
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

async function mountHost() {
  setActivePinia(createPinia())
  replaceMock.mockClear()
  const events: Array<{ tab?: string }> = []
  const listener = (e: Event) => events.push((e as CustomEvent<{ tab?: string }>).detail ?? {})
  window.addEventListener(OPEN_SETTINGS_EVENT, listener)
  const wrapper = mount(SettingsSheetHost, {
    global: {
      stubs: {
        SettingsSheet: { template: '<div />' },
      },
    },
  })
  await flush()
  return {
    events,
    wrapper,
    cleanup: () => {
      window.removeEventListener(OPEN_SETTINGS_EVENT, listener)
      wrapper.unmount()
    },
  }
}

describe('SettingsSheetHost', () => {
  it('opens the sheet for ?settings=<tab> and removes the query', async () => {
    setQuery({ settings: 'provider' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(ctx.events[0]).toMatchObject({ tab: 'provider' })
      expect(replaceMock).toHaveBeenCalledWith({ path: '/', query: {} })
    } finally {
      ctx.cleanup()
    }
  })

  it('fires exactly once while the same query value persists (no strip in between)', async () => {
    setQuery({ settings: 'provider' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      // Dieselbe Query (gleicher Wert, neues Query-Objekt) erneut zugestellt,
      // ohne dass die Query zwischendurch gestrippt wurde: kein zweites
      // Event/Replace — exakt-1x-Guard für dieselbe Navigation.
      setQuery({ settings: 'provider' })
      await flush()
      await flush()
      expect(ctx.events).toHaveLength(1)
      expect(replaceMock).toHaveBeenCalledTimes(1)
    } finally {
      ctx.cleanup()
    }
  })

  it('forwards plugin detail deep links while keeping the plugin query', async () => {
    setQuery({ settings: 'plugins', plugin: 'plug-1' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(ctx.events[0]).toMatchObject({ tab: 'plugins', pluginId: 'plug-1' })
      expect(replaceMock).toHaveBeenCalledWith({ path: '/', query: { plugin: 'plug-1' } })
    } finally {
      ctx.cleanup()
    }
  })

  it('forwards the opaque workspace draft ID to credentials without including draft data in the URL', async () => {
    setQuery({ settings: 'credentials', add_credential: 'svc-1', workspace_draft: 'opaque-id' })
    const ctx = await mountHost()
    try {
      expect(ctx.events[0]).toMatchObject({
        tab: 'credentials',
        serviceId: 'svc-1',
        workspaceDraftId: 'opaque-id',
      })
      expect(replaceMock).toHaveBeenCalledWith({ path: '/', query: {} })
    } finally {
      ctx.cleanup()
    }
  })

  it('forwards reconnect credential context and consumes it only once', async () => {
    setQuery({ settings: 'credentials', reconnect_credential: 'cred-1' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(ctx.events[0]).toMatchObject({ tab: 'credentials', credentialId: 'cred-1' })
      expect(replaceMock).toHaveBeenCalledWith({ path: '/', query: {} })
    } finally {
      ctx.cleanup()
    }
  })

  it('opens the sheet again when the same ?settings= value navigates anew after stripping', async () => {
    setQuery({ settings: 'provider' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(replaceMock).toHaveBeenCalledTimes(1)
      // Strip-Abschluss simulieren: Query ist weg → Guard wird zurückgesetzt.
      setQuery({})
      await flush()
      expect(ctx.events).toHaveLength(1)
      // Neue Navigation mit demselben Wert (z. B. zweiter Legacy-Link-Klick):
      // Event feuert ein zweites Mal + zweites Replace.
      setQuery({ settings: 'provider' })
      await flush()
      expect(ctx.events).toHaveLength(2)
      expect(ctx.events[1]).toMatchObject({ tab: 'provider' })
      expect(replaceMock).toHaveBeenCalledTimes(2)
      expect(replaceMock).toHaveBeenLastCalledWith({ path: '/', query: {} })
    } finally {
      ctx.cleanup()
    }
  })

  it('opens credentials from a service deep link and preserves action context while stripping consumed query', async () => {
    setQuery({ settings: 'credentials', add_credential: 'svc-1' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(ctx.events[0]).toMatchObject({ tab: 'credentials', serviceId: 'svc-1' })
      expect(replaceMock).toHaveBeenCalledWith({ path: '/', query: {} })
    } finally {
      ctx.cleanup()
    }
  })

  it('opens credentials after an OAuth callback with no settings query', async () => {
    setQuery({ oauth_result: 'connected', credential_id: 'cred-1' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(ctx.events[0]).toMatchObject({ tab: 'credentials' })
      expect(replaceMock).toHaveBeenCalledWith({
        path: '/',
        query: { oauth_result: 'connected', credential_id: 'cred-1' },
      })
    } finally {
      ctx.cleanup()
    }
  })

  it('strips OAuth callback result exactly through CredentialsPanel, not by the settings host', async () => {
    setQuery({ settings: 'credentials', oauth_result: 'connected', credential_id: 'cred-1' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(ctx.events[0]).toMatchObject({ tab: 'credentials' })
      expect(replaceMock).toHaveBeenCalledWith({
        path: '/',
        query: { oauth_result: 'connected', credential_id: 'cred-1' },
      })
    } finally {
      ctx.cleanup()
    }
  })

  it('keeps unrelated query params when stripping ?settings=', async () => {
    setQuery({ session: 's-1', settings: 'provider' })
    const ctx = await mountHost()
    try {
      expect(ctx.events).toHaveLength(1)
      expect(replaceMock).toHaveBeenCalledWith({ path: '/', query: { session: 's-1' } })
    } finally {
      ctx.cleanup()
    }
  })
})
