import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, shallowMount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EditRunnerResourcesDialog from './EditRunnerResourcesDialog.vue'
import type { Runner } from '@/types'

enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())

const updateRunner = vi.fn()
const getRunnerMetricsLatest = vi.fn()

const runnerStore = {
  updateRunner,
}

vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => runnerStore,
}))

vi.mock('@/services/runners.api', () => ({
  getRunnerMetricsLatest: (...args: unknown[]) => getRunnerMetricsLatest(...args),
}))

function makeRunner(overrides: Partial<Runner> = {}): Runner {
  return {
    id: overrides.id ?? 'runner-1',
    name: overrides.name ?? 'Runner',
    status: overrides.status ?? 'online',
    available_runtimes: overrides.available_runtimes ?? ['docker', 'qemu'],
    organization_id: overrides.organization_id ?? 'org-1',
    connected_at: overrides.connected_at ?? null,
    disconnected_at: overrides.disconnected_at ?? null,
    qemu_min_vcpus: overrides.qemu_min_vcpus ?? 1,
    qemu_max_vcpus: overrides.qemu_max_vcpus ?? 8,
    qemu_default_vcpus: overrides.qemu_default_vcpus ?? 2,
    qemu_min_memory_mb: overrides.qemu_min_memory_mb ?? 1024,
    qemu_max_memory_mb: overrides.qemu_max_memory_mb ?? 16384,
    qemu_default_memory_mb: overrides.qemu_default_memory_mb ?? 4096,
    qemu_min_disk_size_gb: overrides.qemu_min_disk_size_gb ?? 20,
    qemu_max_disk_size_gb: overrides.qemu_max_disk_size_gb ?? 200,
    qemu_default_disk_size_gb: overrides.qemu_default_disk_size_gb ?? 50,
    qemu_max_active_vcpus: overrides.qemu_max_active_vcpus ?? null,
    qemu_max_active_memory_mb: overrides.qemu_max_active_memory_mb ?? null,
    qemu_max_active_disk_size_gb: overrides.qemu_max_active_disk_size_gb ?? null,
    created_at: overrides.created_at ?? '2026-04-01T10:00:00.000Z',
    updated_at: overrides.updated_at ?? '2026-04-01T10:00:00.000Z',
  } as Runner
}

type DialogVm = typeof EditRunnerResourcesDialog extends never
  ? never
  : {
      isValid: boolean
      minVcpus: number
      minMemoryMb: number | string
      open: boolean
      autosave: { flush: () => Promise<boolean> }
    }

describe('EditRunnerResourcesDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    updateRunner.mockReset()
    updateRunner.mockResolvedValue(true)
    getRunnerMetricsLatest.mockReset()
  })

  it('lays out the limits form in two columns on desktop', () => {
    const wrapper = shallowMount(EditRunnerResourcesDialog, {
      props: { runner: makeRunner() },
      global: { renderStubDefaultSlot: true },
    })

    const form = wrapper.find('#runner-limits-form')
    expect(form.exists()).toBe(true)
    expect(form.classes()).toContain('md:grid-cols-2')
  })

  it('is valid with the runner defaults and submits unlimited totals as null', async () => {
    const wrapper = shallowMount(EditRunnerResourcesDialog, {
      props: { runner: makeRunner() },
    })
    const vm = wrapper.vm as unknown as DialogVm

    expect(vm.isValid).toBe(true)

    vm.open = true
    vm.minVcpus = 2
    await wrapper.vm.$nextTick()
    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()

    expect(updateRunner).toHaveBeenCalledWith(
      'runner-1',
      expect.objectContaining({
        qemu_min_vcpus: 2,
        qemu_max_vcpus: 8,
        qemu_default_vcpus: 2,
        qemu_max_active_vcpus: null,
        qemu_max_active_memory_mb: null,
        qemu_max_active_disk_size_gb: null,
      }),
      { silent: true },
    )
  })

  it('is invalid when a minimum is not positive', async () => {
    const wrapper = shallowMount(EditRunnerResourcesDialog, {
      props: { runner: makeRunner() },
    })
    const vm = wrapper.vm as unknown as DialogVm

    vm.minVcpus = 0
    await wrapper.vm.$nextTick()

    expect(vm.isValid).toBe(false)

    vm.open = true
    await vm.autosave.flush()
    expect(updateRunner).not.toHaveBeenCalled()
  })

  it('keeps a cleared memory field local instead of saving a snapped intermediate value', async () => {
    const wrapper = shallowMount(EditRunnerResourcesDialog, {
      props: { runner: makeRunner() },
    })
    const vm = wrapper.vm as unknown as DialogVm
    vm.open = true
    vm.minMemoryMb = ''
    await wrapper.vm.$nextTick()
    await vi.advanceTimersByTimeAsync(500)
    expect(vm.minMemoryMb).toBe('')
    expect(await vm.autosave.flush()).toBe(false)
    expect(updateRunner).not.toHaveBeenCalled()
  })
})
