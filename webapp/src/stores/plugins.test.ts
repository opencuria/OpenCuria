import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as pluginsApi from '@/services/plugins.api'
import { usePluginStore } from '@/stores/plugins'
import { createPinia, setActivePinia } from 'pinia'
import { toast } from 'vue-sonner'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('@/services/plugins.api', () => ({
  listPlugins: vi.fn(),
  getPlugin: vi.fn(),
  createPlugin: vi.fn(),
  updatePlugin: vi.fn(),
  deletePlugin: vi.fn(),
  togglePluginActivation: vi.fn(),
  listWorkspacePlugins: vi.fn(),
  updateWorkspacePlugins: vi.fn(),
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ activeOrganizationId: 'org-1' }),
}))

const listPluginsMock = vi.mocked(pluginsApi.listPlugins)

function makePlugin(overrides: Record<string, unknown> = {}) {
  return {
    id: 'plugin-1',
    name: 'Playwright',
    slug: 'playwright',
    description: 'Browser automation',
    enabled: true,
    published: true,
    organization_id: null,
    is_global: true,
    org_enabled: false,
    skills: [],
    mcp_servers: [],
    credential_requirements: [],
    credential_readiness: { required_service_ids: [], missing_required_service_ids: [], ready: true },
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

describe('plugin store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('fetches the catalog with loading/error handling', async () => {
    listPluginsMock.mockResolvedValue([makePlugin()])
    const store = usePluginStore()
    await store.fetchPlugins()
    expect(store.plugins).toHaveLength(1)
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
  })

  it('records load errors without throwing', async () => {
    listPluginsMock.mockRejectedValue(new Error('boom'))
    const store = usePluginStore()
    await store.fetchPlugins()
    expect(store.error).toBe('boom')
  })

  it('clears state and refetches on org switch', async () => {
    listPluginsMock.mockResolvedValue([makePlugin()])
    const store = usePluginStore()
    store.loadedOrgId = 'org-old'
    store.plugins = [makePlugin({ id: 'stale' })]
    store.workspacePlugins = { 'ws-1': [] }
    await store.reload()
    expect(store.plugins[0]?.id).toBe('plugin-1')
    expect(store.workspacePlugins).toEqual({})
  })

  it('creates/updates/deletes plugins and toasts without secrets', async () => {
    const store = usePluginStore()
    vi.mocked(pluginsApi.createPlugin).mockResolvedValue(makePlugin())
    const created = await store.createPlugin({ name: 'Playwright' })
    expect(created?.id).toBe('plugin-1')
    expect(toast.success).toHaveBeenCalledWith('Plugin created', expect.objectContaining({ description: expect.any(String) }))

    vi.mocked(pluginsApi.updatePlugin).mockResolvedValue(makePlugin({ name: 'Renamed' }))
    const updated = await store.updatePlugin('plugin-1', { name: 'Renamed' })
    expect(updated?.name).toBe('Renamed')

    vi.mocked(pluginsApi.deletePlugin).mockResolvedValue(undefined)
    expect(await store.deletePlugin('plugin-1')).toBe(true)
    expect(store.plugins).toHaveLength(0)
  })

  it('toggles org activation and surfaces failures', async () => {
    const store = usePluginStore()
    store.plugins = [makePlugin()]
    vi.mocked(pluginsApi.togglePluginActivation).mockResolvedValue(
      makePlugin({ org_enabled: true }),
    )
    expect(await store.toggleActivation('plugin-1', true)).toBe(true)
    expect(store.plugins[0]?.org_enabled).toBe(true)

    vi.mocked(pluginsApi.togglePluginActivation).mockRejectedValue(new Error('forbidden'))
    expect(await store.toggleActivation('plugin-1', false)).toBe(false)
    expect(toast.error).toHaveBeenCalledWith('Update failed', expect.objectContaining({ description: 'forbidden' }))
  })

  it('loads and replaces workspace plugin activations', async () => {
    const store = usePluginStore()
    vi.mocked(pluginsApi.listWorkspacePlugins).mockResolvedValue([
      {
        id: 'plugin-1',
        name: 'Playwright',
        slug: 'playwright',
        description: '',
        organization_id: null,
        is_global: true,
        workspace_enabled: true,
        missing_required_credentials: [],
        ready: true,
      },
    ])
    await store.fetchWorkspacePlugins('ws-1')
    expect(store.workspacePlugins['ws-1']).toHaveLength(1)

    vi.mocked(pluginsApi.updateWorkspacePlugins).mockResolvedValue([])
    const result = await store.setWorkspacePlugins('ws-1', [])
    expect(result).toEqual([])
    expect(store.workspacePlugins['ws-1']).toEqual([])
  })

  it('tracks concurrent toggles per plugin id', async () => {
    const store = usePluginStore()
    store.plugins = [makePlugin(), makePlugin({ id: 'plugin-2', name: 'Other' })]
    let resolveToggle!: (value: ReturnType<typeof makePlugin>) => void
    vi.mocked(pluginsApi.togglePluginActivation).mockImplementation(
      (_id: string) => new Promise((resolve) => { resolveToggle = resolve as typeof resolveToggle }),
    )
    const pending = store.toggleActivation('plugin-1', true)
    expect(store.togglingIds).toContain('plugin-1')
    resolveToggle(makePlugin({ org_enabled: true }))
    await expect(pending).resolves.toBe(true)
    expect(store.togglingIds).not.toContain('plugin-1')
  })

  it('discards stale catalog fetches after an org switch', async () => {
    const store = usePluginStore()
    let firstResolve!: (value: ReturnType<typeof makePlugin>[]) => void
    const first = new Promise<ReturnType<typeof makePlugin>[]>((resolve) => {
      firstResolve = resolve
    })
    listPluginsMock.mockReturnValueOnce(first)
    const pendingFirst = store.fetchPlugins()
    // Simulate an org switch clearing state while the first fetch is slow.
    store.clear()
    listPluginsMock.mockResolvedValue([makePlugin({ id: 'plugin-new' })])
    await store.fetchPlugins()
    firstResolve([makePlugin({ id: 'plugin-stale' })])
    await pendingFirst
    expect(store.plugins[0]?.id).toBe('plugin-new')
  })

  it('discards stale workspace plugin fetches after a clear', async () => {
    const store = usePluginStore()
    let firstResolve!: (value: never[]) => void
    const first = new Promise<never[]>((resolve) => { firstResolve = resolve })
    vi.mocked(pluginsApi.listWorkspacePlugins).mockReturnValueOnce(first)
    const pendingFirst = store.fetchWorkspacePlugins('ws-1')
    store.clear()
    vi.mocked(pluginsApi.listWorkspacePlugins).mockResolvedValue([])
    await store.fetchWorkspacePlugins('ws-1')
    firstResolve([])
    await pendingFirst
    expect(store.workspacePlugins['ws-1']).toEqual([])
  })
})
