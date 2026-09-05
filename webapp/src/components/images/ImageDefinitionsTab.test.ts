import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ImageDefinitionsTab from './ImageDefinitionsTab.vue'
import { RunnerStatus, type ImageDefinition, type Runner } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import * as api from '@/services/api'

vi.mock('@/services/workspaces.api', () => ({
  listImageDefinitions: vi.fn(),
  listRunnerImageBuilds: vi.fn(),
  createImageDefinition: vi.fn(),
  updateImageDefinition: vi.fn(),
  deleteImageDefinition: vi.fn(),
  duplicateImageDefinition: vi.fn(),
  activateImageDefinition: vi.fn(),
  createRunnerImageBuild: vi.fn(),
  updateRunnerImageBuild: vi.fn(),
  deleteRunnerImageBuild: vi.fn(),
  getRunnerImageBuildLog: vi.fn(),
}))

vi.mock('@/services/api', async () => {
  const actual = await vi.importActual<typeof import('@/services/api')>('@/services/api')
  return {
    ...actual,
    get: vi.fn(),
  }
})

const listImageDefinitions = vi.mocked(workspacesApi.listImageDefinitions)
const listRunnerImageBuilds = vi.mocked(workspacesApi.listRunnerImageBuilds)
const getMock = vi.mocked(api.get)

function makeDefinition(overrides: Partial<ImageDefinition> = {}): ImageDefinition {
  return {
    id: 'def-1',
    organization_id: 'org-1',
    name: 'Python Base',
    description: 'Dev image',
    is_standard: false,
    runtime_type: 'docker',
    base_distro: 'ubuntu:24.04',
    packages: [],
    env_vars: {},
    custom_dockerfile: '',
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
    available_runtimes: ['docker', 'qemu'],
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

describe('ImageDefinitionsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    listImageDefinitions.mockResolvedValue([
      makeDefinition(),
      makeDefinition({ id: 'gone', name: 'Deleted Recipe', status: 'deleted' }),
    ])
    listRunnerImageBuilds.mockResolvedValue([])
    getMock.mockResolvedValue([makeRunner()] as never)
  })

  it('renders API summary counts without expanding a card', async () => {
    const wrapper = mount(ImageDefinitionsTab)
    await flushPromises()

    expect(wrapper.text()).toContain('2 active, 1 building, 0 failed')
    expect(wrapper.text()).not.toContain('0 active, 0 building, 0 failed')
    expect(listRunnerImageBuilds).not.toHaveBeenCalled()
  })

  it('does not render fully deleted definitions', async () => {
    const wrapper = mount(ImageDefinitionsTab)
    await flushPromises()

    expect(wrapper.text()).toContain('Python Base')
    expect(wrapper.text()).not.toContain('Deleted Recipe')
  })

  it('offers Build instead of Assign & Activate for a runner without a build', async () => {
    const wrapper = mount(ImageDefinitionsTab)
    await flushPromises()

    await wrapper.get('button.text-muted-foreground').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Build')
    expect(wrapper.text()).not.toContain('Assign & Activate')
    expect(wrapper.text()).not.toContain('Rebuild / Activate')
  })
})
