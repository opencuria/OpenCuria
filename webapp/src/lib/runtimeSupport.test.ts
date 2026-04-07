import { describe, expect, it } from 'vitest'

import { RuntimeType, RunnerStatus, type Runner } from '@/types'
import { filterRunnersByRuntime, runnerSupportsRuntime } from './runtimeSupport'

function makeRunner(id: string, availableRuntimes: string[]): Runner {
  return {
    id,
    name: `runner-${id}`,
    status: RunnerStatus.ONLINE,
    available_runtimes: availableRuntimes,
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
    created_at: '2026-03-29T00:00:00Z',
    updated_at: '2026-03-29T00:00:00Z',
  }
}

describe('runnerSupportsRuntime', () => {
  it('returns true for supported runtimes', () => {
    expect(
      runnerSupportsRuntime(makeRunner('1', ['docker', 'qemu']), RuntimeType.QEMU),
    ).toBe(true)
  })

  it('returns false for unsupported runtimes', () => {
    expect(
      runnerSupportsRuntime(makeRunner('1', ['docker']), RuntimeType.QEMU),
    ).toBe(false)
  })
})

describe('filterRunnersByRuntime', () => {
  it('keeps only compatible runners', () => {
    const runners = [
      makeRunner('docker-only', ['docker']),
      makeRunner('qemu-only', ['qemu']),
      makeRunner('both', ['docker', 'qemu']),
    ]

    expect(
      filterRunnersByRuntime(runners, RuntimeType.DOCKER).map((runner) => runner.id),
    ).toEqual(['docker-only', 'both'])
  })
})
