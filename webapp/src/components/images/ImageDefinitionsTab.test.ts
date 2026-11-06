import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ImageDefinitionsTab from './ImageDefinitionsTab.vue'
import { RunnerStatus, type ImageDefinition, type Runner, type RunnerImageBuild } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import * as api from '@/services/api'

const auth = vi.hoisted(() => ({ isAdmin: true }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))

vi.mock('@/services/workspaces.api', () => ({
  listImageDefinitions: vi.fn(),
  listRunnerImageBuilds: vi.fn(),
  createImageDefinition: vi.fn(),
  updateImageDefinition: vi.fn(),
  duplicateImageDefinition: vi.fn(),
  activateImageDefinition: vi.fn(),
  createRunnerImageBuild: vi.fn(),
  updateRunnerImageBuild: vi.fn(),
  getRunnerImageBuildLog: vi.fn(),
}))

vi.mock('@/services/api', async () => {
  const actual = await vi.importActual<typeof import('@/services/api')>('@/services/api')
  return {
    ...actual,
    get: vi.fn(),
  }
})

// Inline menus keep secondary actions testable without portals.
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<span><slot /></span>' },
  DropdownMenuContent: { template: '<div role="menu"><slot /></div>' },
  DropdownMenuSeparator: { template: '<hr />' },
  DropdownMenuItem: defineComponent({
    emits: ['select'],
    template: '<button role="menuitem" @click="$emit(\'select\')"><slot /></button>',
  }),
}))

const dialogStubs = {
  Dialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' },
  ...Object.fromEntries(
    [
      'DialogContent',
      'DialogHeader',
      'DialogTitle',
      'DialogDescription',
      'DialogBody',
      'DialogFooter',
    ].map((name) => [name, { template: '<div><slot /></div>' }]),
  ),
  ImageDeletionDialog: { props: ['target', 'name'], template: '<div />' },
}

const listImageDefinitions = vi.mocked(workspacesApi.listImageDefinitions)
const listRunnerImageBuilds = vi.mocked(workspacesApi.listRunnerImageBuilds)
const createRunnerImageBuild = vi.mocked(workspacesApi.createRunnerImageBuild)
const updateRunnerImageBuild = vi.mocked(workspacesApi.updateRunnerImageBuild)
const getMock = vi.mocked(api.get)

function makeDefinition(overrides: Partial<ImageDefinition> = {}): ImageDefinition {
  return {
    id: 'def-1',
    organization_id: 'org-1',
    name: 'Python Base',
    description: 'Dev image',
    is_standard: false,
    base_distro: 'ubuntu:24.04',
    packages: [],
    env_vars: {},
    custom_init_script: '',
    is_active: true,
    status: 'active',
    runner_build_summary: {
      active: 2,
      building: 1,
      failed: 0,
      inactive: 0,
      removing: 0,
    },
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
    ...overrides,
  }
}

function makeRunner(overrides: Partial<Runner> = {}): Runner {
  return {
    id: 'runner-1',
    name: 'edge-1',
    status: RunnerStatus.ONLINE,
    qemu_min_vcpus: 1,
    qemu_max_vcpus: 8,
    qemu_default_vcpus: 2,
    qemu_min_memory_mb: 1024,
    qemu_max_memory_mb: 8192,
    qemu_default_memory_mb: 4096,
    qemu_min_disk_size_gb: 20,
    qemu_max_disk_size_gb: 200,
    qemu_default_disk_size_gb: 50,
    qemu_max_active_vcpus: null,
    qemu_max_active_memory_mb: null,
    qemu_max_active_disk_size_gb: null,
    organization_id: 'org-1',
    connected_at: null,
    disconnected_at: null,
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
    ...overrides,
  }
}

function makeBuild(overrides: Partial<RunnerImageBuild> = {}): RunnerImageBuild {
  return {
    id: 'build-1',
    image_definition_id: 'def-1',
    runner_id: 'runner-1',
    status: 'active',
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
    versions: [],
    ...overrides,
  }
}

async function mountTab() {
  const wrapper = mount(ImageDefinitionsTab, { global: { stubs: dialogStubs } })
  await flushPromises()
  return wrapper
}

async function expand(wrapper: Awaited<ReturnType<typeof mountTab>>) {
  await wrapper.get('[data-testid="image-definition-toggle"]').trigger('click')
  await flushPromises()
}

function buttonsNamed(wrapper: Awaited<ReturnType<typeof mountTab>>, label: string) {
  return wrapper.findAll('button').filter((button) => button.text() === label)
}

describe('ImageDefinitionsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.isAdmin = true
    listImageDefinitions.mockResolvedValue([
      makeDefinition(),
      makeDefinition({ id: 'gone', name: 'Deleted Recipe', status: 'deleted' }),
    ])
    listRunnerImageBuilds.mockResolvedValue([])
    getMock.mockResolvedValue([makeRunner()] as never)
  })

  it('shows one status per definition without loading runner builds', async () => {
    const wrapper = await mountTab()

    const status = wrapper.get('[data-testid="image-status"]')
    expect(status.text()).toBe('Building')
    expect(status.attributes('data-tone')).toBe('progress')
    expect(wrapper.text()).toContain('ubuntu:24.04 · Dev image')
    expect(wrapper.text()).not.toMatch(/\d+ active/)
    expect(listRunnerImageBuilds).not.toHaveBeenCalled()
  })

  it('does not render fully deleted definitions', async () => {
    const wrapper = await mountTab()

    expect(wrapper.text()).toContain('Python Base')
    expect(wrapper.text()).not.toContain('Deleted Recipe')
  })

  it('offers only Build for a runner without an image and starts the build', async () => {
    createRunnerImageBuild.mockResolvedValue(makeBuild({ status: 'pending' }))
    const wrapper = await mountTab()
    await expand(wrapper)

    const row = wrapper.get('[data-testid="image-runner-row"]')
    expect(row.text()).toContain('edge-1')
    expect(row.text()).toContain('Not built')
    expect(row.findAll('button').map((button) => button.text())).toEqual(['Build'])

    await row.get('[data-testid="image-runner-build"]').trigger('click')
    await flushPromises()
    expect(createRunnerImageBuild).toHaveBeenCalledWith('def-1', {
      runner_id: 'runner-1',
      activate: true,
    })
  })

  it('keeps secondary actions of a ready image in its menu and confirms Rebuild', async () => {
    listRunnerImageBuilds.mockResolvedValue([makeBuild()])
    updateRunnerImageBuild.mockResolvedValue(makeBuild())
    const wrapper = await mountTab()
    await expand(wrapper)

    const row = wrapper.get('[data-testid="image-runner-row"]')
    expect(row.text()).toContain('Ready')
    expect(row.get('[data-testid="image-runner-rebuild"]').text()).toBe('Rebuild')
    expect(row.findAll('[role="menuitem"]').map((item) => item.text())).toEqual([
      'Disable',
      'Remove',
    ])

    await row.get('[data-testid="image-runner-rebuild"]').trigger('click')
    await wrapper.get('[data-testid="rebuild-message-input"]').setValue('  Added Playwright ')
    await wrapper.get('#image-rebuild-form').trigger('submit')
    await flushPromises()
    expect(updateRunnerImageBuild).toHaveBeenCalledWith('def-1', 'runner-1', {
      action: 'rebuild',
      message: 'Added Playwright',
    })
  })

  it('shows a failed action and keeps the runner usable', async () => {
    createRunnerImageBuild.mockRejectedValue(new Error('Runner is offline'))
    const wrapper = await mountTab()
    await expand(wrapper)

    await wrapper.get('[data-testid="image-runner-build"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('Runner is offline')
    expect(buttonsNamed(wrapper, 'Build')[0]!.attributes('disabled')).toBeUndefined()
  })

  it('shows members the states but no create, build or definition actions', async () => {
    auth.isAdmin = false
    listRunnerImageBuilds.mockResolvedValue([makeBuild({ status: 'failed' })])
    const wrapper = await mountTab()

    expect(wrapper.find('[data-testid="image-definition-create"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="image-definition-menu"]').exists()).toBe(false)

    await expand(wrapper)
    const row = wrapper.get('[data-testid="image-runner-row"]')
    expect(row.text()).toContain('Failed')
    expect(row.find('[data-testid="image-runner-retry"]').exists()).toBe(false)
    expect(row.findAll('[role="menuitem"]').map((item) => item.text())).toEqual(['Build log'])
  })
})
