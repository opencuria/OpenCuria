import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WorkspaceSetupCard from './WorkspaceSetupCard.vue'
import { OPEN_SETTINGS_EVENT, SETTINGS_CLOSED_EVENT } from '@/components/settings/settingsTabs'

const authStore = reactive({ isAdmin: true })
const runnerStore = reactive({
  runners: [] as Array<{ id: string; name: string; status: string }>,
  error: null as string | null,
  fetchRunners: vi.fn(async () => undefined),
})
const imageStore = reactive({
  imageDefinitions: [] as Array<Record<string, unknown>>,
  runnerBuildsByDefinition: {} as Record<string, Array<Record<string, unknown>>>,
  capturedImages: [] as unknown[],
  error: null as string | null,
  definitionsError: null as string | null,
  fetchCapturedImages: vi.fn(async () => undefined),
  fetchImageDefinitionsWithBuilds: vi.fn(async () => undefined),
})

vi.mock('@/stores/auth', () => ({ useAuthStore: () => authStore }))
vi.mock('@/stores/runners', () => ({ useRunnerStore: () => runnerStore }))
vi.mock('@/stores/images', () => ({ useImageStore: () => imageStore }))
vi.mock('@/composables/usePolling', () => ({
  usePolling: (fetch: () => Promise<void>) => ({
    start: () => void fetch(),
    stop: vi.fn(),
    refresh: () => void fetch(),
  }),
}))

const ubuntu = { id: 'def-1', name: 'Ubuntu 24.04', is_active: true, status: 'active' }
const readyBuild = {
  'def-1': [{ id: 'b1', runner_id: 'runner-1', status: 'active', image_artifact_id: 'a1' }],
}

const mounted: Array<ReturnType<typeof mount>> = []
function mountCard() {
  const wrapper = mount(WorkspaceSetupCard)
  mounted.push(wrapper)
  return wrapper
}

function state(wrapper: ReturnType<typeof mount>, step: string): string | undefined {
  return wrapper.get(`[data-testid="workspace-setup-step-${step}"]`).attributes('data-state')
}

describe('WorkspaceSetupCard', () => {
  beforeEach(() => {
    authStore.isAdmin = true
    runnerStore.runners = [{ id: 'runner-1', name: 'local-qemu', status: 'offline' }]
    runnerStore.error = null
    imageStore.imageDefinitions = [ubuntu]
    imageStore.runnerBuildsByDefinition = {}
    imageStore.capturedImages = []
    imageStore.error = null
    imageStore.definitionsError = null
  })

  afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.clearAllMocks()
  })

  it('walks an admin from an offline runner to an unbuilt image to creating', async () => {
    const wrapper = mountCard()
    await flushPromises()
    expect(runnerStore.fetchRunners).toHaveBeenCalled()
    expect(state(wrapper, 'runner')).toBe('current')
    expect(wrapper.text()).toContain('local-qemu is offline')
    expect(wrapper.find('[data-testid="workspace-setup-action-image"]').exists()).toBe(false)

    runnerStore.runners = [{ id: 'runner-1', name: 'local-qemu', status: 'online' }]
    await flushPromises()
    expect(state(wrapper, 'runner')).toBe('done')
    expect(state(wrapper, 'image')).toBe('current')
    expect(wrapper.text()).toContain('Build Ubuntu 24.04 on your runner')

    imageStore.runnerBuildsByDefinition = readyBuild
    await flushPromises()
    expect(state(wrapper, 'image')).toBe('done')
    expect(state(wrapper, 'workspace')).toBe('current')
    expect(wrapper.get('[data-testid="workspace-setup-progress"]').text()).toBe('2 of 3')
  })

  it('opens the matching settings tab and emits create from the last step', async () => {
    const listener = vi.fn()
    window.addEventListener(OPEN_SETTINGS_EVENT, listener)
    const wrapper = mountCard()
    await flushPromises()
    await wrapper.get('[data-testid="workspace-setup-action-runner"]').trigger('click')
    expect((listener.mock.calls[0]![0] as CustomEvent).detail).toEqual({ tab: 'runners' })
    await wrapper.get('[data-testid="workspace-setup-action-workspace"]').trigger('click')
    expect(wrapper.emitted('create')).toHaveLength(1)
    window.removeEventListener(OPEN_SETTINGS_EVENT, listener)
  })

  it('re-checks right away when Settings closes and stops listening after unmount', async () => {
    const wrapper = mountCard()
    await flushPromises()
    expect(runnerStore.fetchRunners).toHaveBeenCalledTimes(1)
    window.dispatchEvent(new Event(SETTINGS_CLOSED_EVENT))
    await flushPromises()
    expect(runnerStore.fetchRunners).toHaveBeenCalledTimes(2)
    mounted.splice(mounted.indexOf(wrapper), 1)
    wrapper.unmount()
    window.dispatchEvent(new Event(SETTINGS_CLOSED_EVENT))
    expect(runnerStore.fetchRunners).toHaveBeenCalledTimes(2)
  })

  it('holds members back with a neutral note until an image is ready', async () => {
    authStore.isAdmin = false
    const wrapper = mountCard()
    await flushPromises()
    const create = wrapper.get('[data-testid="workspace-setup-create"]')
    expect(create.attributes('disabled')).toBeDefined()
    const text = wrapper.text()
    expect(text).toContain('No workspace image is available yet')
    expect(wrapper.find('ol').exists()).toBe(false)
    // No admin details or hints for members.
    expect(text).not.toMatch(/runner|admin|settings/i)

    runnerStore.runners = [{ id: 'runner-1', name: 'local-qemu', status: 'online' }]
    imageStore.runnerBuildsByDefinition = readyBuild
    await flushPromises()
    expect(create.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).not.toContain('No workspace image is available yet')
    await create.trigger('click')
    expect(wrapper.emitted('create')).toHaveLength(1)
  })

  it('does not block members when availability could not be checked', async () => {
    authStore.isAdmin = false
    imageStore.definitionsError = 'Failed to load image definitions'
    const wrapper = mountCard()
    await flushPromises()
    expect(
      wrapper.get('[data-testid="workspace-setup-create"]').attributes('disabled'),
    ).toBeUndefined()
  })
})
