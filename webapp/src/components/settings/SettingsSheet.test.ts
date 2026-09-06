import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import SettingsSheet from './SettingsSheet.vue'
import { OPEN_SETTINGS_EVENT } from './settingsTabs'
import * as harnessApi from '@/services/harness.api'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    getProviderConfig: vi.fn(),
    saveProviderConfig: vi.fn(),
    deleteProviderConfig: vi.fn(),
  }
})

vi.mock('@/services/organizations.api', () => ({
  getOrganization: vi.fn(async () => ({
    id: 'org-1',
    name: 'Acme',
    slug: 'acme',
    role: 'admin',
    workspace_auto_stop_timeout_minutes: null,
    created_at: '2026-01-01T00:00:00.000Z',
  })),
  updateOrganizationWorkspacePolicy: vi.fn(async (id: string) => ({
    id,
    name: 'Acme',
    slug: 'acme',
    role: 'admin',
    workspace_auto_stop_timeout_minutes: null,
    created_at: '2026-01-01T00:00:00.000Z',
  })),
}))

vi.mock('@/services/api', () => ({
  get: vi.fn(async () => []),
  post: vi.fn(async () => ({})),
  patch: vi.fn(async () => ({})),
  del: vi.fn(async () => ({})),
  ApiRequestError: class ApiRequestError extends Error {},
}))

const authMock = {
  activeOrganizationId: 'org-1',
  activeOrganization: { id: 'org-1', name: 'Acme', role: 'admin' },
  isAdmin: true,
  user: { email: 'admin@example.com' },
}

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authMock,
}))

const skillStoreMock = {
  skills: [],
  loading: false,
  error: null as string | null,
  fetchSkills: vi.fn(),
  createSkill: vi.fn(),
  updateSkill: vi.fn(),
  deleteSkill: vi.fn(),
}

vi.mock('@/stores/skills', () => ({
  useSkillStore: () => skillStoreMock,
}))

vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({
    credentials: [],
    services: [],
    loading: false,
    error: null,
    fetchCredentials: vi.fn(),
    fetchServices: vi.fn(),
  }),
}))

vi.mock('@/stores/apiKeys', () => ({
  useApiKeyStore: () => ({
    keys: [],
    loading: false,
    error: null,
    availablePermissions: [],
    fetchKeys: vi.fn(),
    fetchAvailablePermissions: vi.fn(),
  }),
}))

vi.mock('@/stores/images', () => ({
  useImageStore: () => ({
    images: [],
    loading: false,
    error: null,
    fetchImages: vi.fn(),
    deleteImageArtifact: vi.fn(),
    renameImageArtifact: vi.fn(),
  }),
}))

vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => ({
    runners: [],
    loading: false,
    error: null,
    fetchRunners: vi.fn(),
  }),
}))

vi.mock('@/services/workspaces.api', () => ({
  listImageDefinitions: vi.fn(async () => []),
  listRunnerImageBuilds: vi.fn(async () => []),
  createImageDefinition: vi.fn(),
  updateImageDefinition: vi.fn(),
  deleteImageDefinition: vi.fn(),
  activateImageDefinition: vi.fn(),
  duplicateImageDefinition: vi.fn(),
  createRunnerImageBuild: vi.fn(),
  updateRunnerImageBuild: vi.fn(),
  deleteRunnerImageBuild: vi.fn(),
  getRunnerImageBuildLog: vi.fn(),
}))

vi.mock('@/lib/runtimeSupport', () => ({
  filterRunnersByRuntime: () => [],
}))

const getProviderConfigMock = vi.mocked(harnessApi.getProviderConfig)

function mountSheet() {
  setActivePinia(createPinia())
  const wrapper = mount(SettingsSheet, {
    attachTo: document.body,
    global: {
      stubs: {
        Dialog: { template: '<div><slot /></div>' },
        DialogContent: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
        // create-*/edit-Dialoge rendern DialogTrigger ohne DialogRoot (gestubbt)
        DialogTrigger: { template: '<div><slot /></div>' },
        ScrollArea: { template: '<div><slot /></div>' },
      },
    },
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

// Gestoppte Instanzen abmelden, damit `opencuria:open-settings`-Listener
// früherer Tests nicht in spätere Tests hineinfeuern.
const mountedWrappers: Array<{ unmount: () => void }> = []

afterEach(() => {
  while (mountedWrappers.length) mountedWrappers.pop()!.unmount()
})

describe('SettingsSheet', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    authMock.isAdmin = true
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-big',
      small_model: 'model-small',
      computer_use_model: 'model-cu',
      has_api_key: true,
      api_key_hint: '••••cdef',
    })
  })

  it('renders all nav items for admins', () => {
    const wrapper = mountSheet()
    for (const tab of [
      'general',
      'provider',
      'skills',
      'credentials',
      'api-keys',
      'images',
      'runners',
      'organization',
    ]) {
      expect(wrapper.find(`[data-testid="settings-nav-${tab}"]`).exists()).toBe(true)
    }
  })

  it('hides the runners nav item for non-admins', () => {
    authMock.isAdmin = false
    const wrapper = mountSheet()
    expect(wrapper.find('[data-testid="settings-nav-runners"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="settings-nav-general"]').exists()).toBe(true)
    authMock.isAdmin = true
  })

  it('switches content on tab click', async () => {
    const wrapper = mountSheet()
    await flushPromises()

    // Default: Allgemein (Workspace-Policy)
    expect(wrapper.text()).toContain('Automatic Workspace Stop')

    await wrapper.find('[data-testid="settings-nav-provider"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('OpenRouter Provider')
  })

  it('opens via opencuria:open-settings event with the given tab', async () => {
    const wrapper = mountSheet()
    expect(wrapper.find('[data-testid="settings-sheet-title"]').text()).toBe('Allgemein')

    window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'provider' } }))
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="settings-sheet-title"]').text()).toBe(
      'Provider & Modelle',
    )
    expect(wrapper.text()).toContain('OpenRouter Provider')
  })

  it('maps legacy org-settings tabs onto sheet tabs', async () => {
    const wrapper = mountSheet()

    window.dispatchEvent(
      new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'workspace-policies' } }),
    )
    await flushPromises()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="settings-sheet-title"]').text()).toBe('Allgemein')

    window.dispatchEvent(
      new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'credential-services' } }),
    )
    await flushPromises()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="settings-sheet-title"]').text()).toBe('Organization')
  })

  it('falls back to general for unknown tabs and hides runners for non-admins', async () => {
    authMock.isAdmin = false
    const wrapper = mountSheet()

    window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'runners' } }))
    await flushPromises()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="settings-sheet-title"]').text()).toBe('Allgemein')

    window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'nope' } }))
    await flushPromises()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="settings-sheet-title"]').text()).toBe('Allgemein')
    authMock.isAdmin = true
  })
})
