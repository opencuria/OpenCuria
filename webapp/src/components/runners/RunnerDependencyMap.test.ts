import { flushPromises, mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import RunnerDependencyMap from './RunnerDependencyMap.vue'
import type { StorageGeneration, StorageRuntime } from '@/types/runnerStorage'

const generations = [
  {
    id: 'g',
    name: 'Base image',
    runtime_type: 'qemu',
    status: 'ready',
    observed_state: 'unknown',
    dependencies: [
      { id: 'ws', name: 'My workspace', status: 'stopped', observed_state: 'unknown' },
    ],
  },
] as StorageGeneration[]
it('offers keyboard-accessible selection and distinguishes lifecycle, observation and domain usage', async () => {
  const wrapper = mount(RunnerDependencyMap, { props: { runtimes: [], generations } })
  await flushPromises()
  const button = wrapper.get('button[aria-label="Inspect My workspace"]')
  expect(button.attributes('aria-pressed')).toBe('false')
  await button.trigger('click')
  expect(button.attributes('aria-pressed')).toBe('true')
  expect(wrapper.emitted('select')?.[0]?.[0]).toMatchObject({
    label: 'My workspace',
    column: 0,
    workspace: { id: 'ws' },
  })
  expect(wrapper.text()).toContain('Observed: unknown')
  expect(wrapper.text()).toContain('Lifecycle ready · Observed unknown')
  expect(wrapper.get('ul[aria-label="Dependency relationships"]').text()).toContain(
    'not a confirmed physical edge',
  )
  await wrapper.get('input').setValue('g')
  expect(wrapper.text()).toContain('1 matches')
  expect(wrapper.find('button[aria-label="Inspect My workspace"]').exists()).toBe(true)
  await wrapper.get('input').setValue('not found')
  expect(wrapper.text()).toContain('0 matches')
  expect(wrapper.find('button[aria-label="Inspect Base image"]').exists()).toBe(false)
  wrapper.unmount()
})
it('discloses other resource count without showing raw IDs, while retaining linked unresolved targets', async () => {
  const runtimes = [
    {
      runtime_type: 'qemu',
      resources: [
        {
          physical_id: 'workspace-physical',
          kind: 'workspace',
          workspace: { id: 'ws', name: 'Active' },
          dependencies: ['missing'],
          aliases: [],
        },
        { physical_id: '/private/disks/loose', kind: 'disk', dependencies: [], aliases: [] },
      ],
    },
  ] as unknown as StorageRuntime[]
  const wrapper = mount(RunnerDependencyMap, { props: { runtimes, generations: [] } })
  await flushPromises()
  expect(wrapper.text()).toContain('Other resources (2)')
  expect(wrapper.text()).toContain('Unresolved · missing')
  expect(wrapper.text()).not.toContain('/private/disks')
  expect(wrapper.find('button[aria-label="Inspect loose"]').exists()).toBe(false)
  await wrapper
    .findAll('button')
    .find((b) => b.text().includes('Other resources (2)'))!
    .trigger('click')
  expect(wrapper.find('button[aria-label="Inspect loose"]').exists()).toBe(true)
  wrapper.unmount()
})

it('shows valid normalized VM telemetry only on physical QEMU workspace nodes', async () => {
  const runtimes = [
    {
      runtime_type: 'qemu',
      resources: [
        {
          physical_id: 'vm',
          kind: 'workspace',
          workspace: { id: 'ws', name: 'VM', status: 'running' },
          state: 'running',
          dependencies: ['disk'],
          aliases: [],
        },
        {
          physical_id: 'disk',
          kind: 'disk',
          workspace: { id: 'ws', name: 'VM', status: 'running' },
          state: 'present',
          dependencies: [],
          aliases: [],
          allocated_bytes: 2048,
        },
      ],
    },
  ] as unknown as StorageRuntime[]
  const sample = {
    cpu_usage_percent: 25,
    ram_used_bytes: 1024,
    ram_total_bytes: 4096,
    disk_used_bytes: 0,
    disk_total_bytes: 4096,
  }
  const wrapper = mount(RunnerDependencyMap, {
    props: {
      runtimes,
      generations: [],
      vmMetrics: { ws: sample },
      metricsTimestamp: '2026-10-03T09:00:00Z',
    },
  })
  await flushPromises()
  expect(wrapper.get('button[aria-label="Inspect VM"]').text()).toContain(
    'VM CPU 25% · RAM 1.0 KiB / 4.0 KiB',
  )
  expect(wrapper.find('button[aria-label="Inspect VM · Workspace disk"]').exists()).toBe(false)
  expect(wrapper.get('button[aria-label="Inspect VM"]').text()).toContain('Used 2.0 KiB')
  expect(wrapper.find('.topology-columns.grid-cols-2').exists()).toBe(true)
  expect(wrapper.find('section[aria-label="Other resources"]').exists()).toBe(false)
  await wrapper.setProps({ vmMetrics: { ws: { ...sample, cpu_usage_percent: 250 } } })
  expect(wrapper.text()).not.toContain('VM CPU')
  await wrapper.setProps({ vmMetrics: { ws: { ...sample, ram_used_bytes: -1 } } })
  expect(wrapper.text()).not.toContain('VM CPU')
  wrapper.unmount()
})
it('retains distinct allocated and generation accounting in compact image cards', async () => {
  const runtimes = [
    {
      runtime_type: 'qemu',
      resources: [
        {
          physical_id: 'base',
          kind: 'disk',
          image_id: 'g',
          state: 'present',
          allocated_bytes: 1024,
          dependencies: [],
          aliases: [],
        },
      ],
    },
  ] as unknown as StorageRuntime[]
  const wrapper = mount(RunnerDependencyMap, {
    props: {
      runtimes,
      generations: [
        {
          ...generations[0]!,
          size_bytes: 4096,
          size_source: 'logical',
          is_current: true,
          dependencies: [],
        },
      ],
    },
  })
  const image = wrapper.get('button[aria-label="Inspect Base image"]')
  expect(image.text()).toContain('Current default')
  expect(image.text()).toContain('1.0 KiB allocated')
  expect(image.text()).toContain('4.0 KiB image (logical)')
  expect(image.text()).toContain('Lifecycle ready · Observed present')
  wrapper.unmount()
})

it('groups runtime diagrams and searches collapsed boot seed members without a separate toggle', async () => {
  const runtimes = [
    {
      runtime_type: 'qemu',
      resources: [
        {
          physical_id: 'vm',
          kind: 'workspace',
          workspace: { id: 'ws', name: 'VM' },
          state: 'running',
          dependencies: ['disk', 'cloud-init.iso'],
          aliases: [],
        },
        {
          physical_id: 'disk',
          kind: 'disk',
          workspace: { id: 'ws', name: 'VM' },
          state: 'present',
          dependencies: [],
          aliases: [],
        },
        {
          physical_id: 'cloud-init.iso',
          kind: 'file',
          workspace: { id: 'ws', name: 'VM' },
          state: 'present',
          dependencies: [],
          aliases: [],
        },
      ],
    },
    { runtime_type: 'docker', resources: [] },
  ] as unknown as StorageRuntime[]
  const wrapper = mount(RunnerDependencyMap, {
    props: {
      runtimes,
      generations: [{ ...generations[0]!, runtime_type: 'docker', dependencies: [] }],
    },
  })
  await flushPromises()
  expect(wrapper.find('button[aria-label="Inspect VM · Boot seed"]').exists()).toBe(false)
  expect(wrapper.get('button[aria-label="Inspect VM"]').text()).toContain('partial (2 unknown)')
  expect(wrapper.find('button[aria-label="Inspect VM · Workspace disk"]').exists()).toBe(false)
  expect(wrapper.get('section[aria-label="qemu dependency map"]').text()).toContain('QEMU')
  expect(wrapper.get('section[aria-label="docker dependency map"]').text()).toContain('Docker')
  expect(wrapper.findAll('button').some((b) => b.text().includes('Boot seeds'))).toBe(false)
  await wrapper.get('input').setValue('cloud-init')
  expect(wrapper.text()).toContain('1 matches')
  expect(wrapper.find('button[aria-label="Inspect VM"]').exists()).toBe(true)
  expect(wrapper.find('button[aria-label="Inspect VM · Boot seed"]').exists()).toBe(false)
  wrapper.unmount()
})

it('does not color cached observed states as current or show telemetry on an exited VM', async () => {
  const runtimes = [
    {
      runtime_type: 'qemu',
      fresh: false,
      resources: [
        {
          physical_id: 'vm',
          kind: 'workspace',
          workspace: { id: 'ws', name: 'Cached VM' },
          state: 'running',
          dependencies: [],
          aliases: [],
        },
      ],
    },
  ] as unknown as StorageRuntime[]
  const wrapper = mount(RunnerDependencyMap, { props: { runtimes, generations: [] } })
  expect(wrapper.get('button[aria-label="Inspect Cached VM"]').find('.bg-success').exists()).toBe(
    false,
  )
  expect(wrapper.find('[title="Last confirmed observed state: running"]').exists()).toBe(true)
  await wrapper.setProps({ runtimes: [{ ...runtimes[0]!, fresh: true }] })
  expect(wrapper.get('button[aria-label="Inspect Cached VM"]').find('.bg-success').exists()).toBe(
    true,
  )
  await wrapper.setProps({
    runtimes: [
      { ...runtimes[0]!, resources: [{ ...runtimes[0]!.resources[0]!, state: 'exited' }] },
    ],
    vmMetrics: {
      ws: {
        cpu_usage_percent: 1,
        ram_used_bytes: 100,
        ram_total_bytes: 1000,
        disk_used_bytes: 100,
        disk_total_bytes: 1000,
      },
    },
  })
  expect(wrapper.text()).not.toContain('VM CPU')
  wrapper.unmount()
})

it('accepts a parent projection, translates physical selection and searches member aliases', async () => {
  const { buildRunnerTopology, projectRunnerTopology } = await import('@/lib/runnerTopology')
  const runtimes = [
    {
      runtime_type: 'qemu',
      resources: [
        {
          physical_id: 'disk',
          kind: 'disk',
          state: 'running',
          workspace: { id: 'ws', name: 'Disk-only workspace', observed_state: 'unknown' },
          dependencies: [],
          aliases: ['member-alias'],
          allocated_bytes: 1024,
          virtual_bytes: 8192,
        },
        {
          physical_id: 'missing-size',
          kind: 'file',
          state: 'present',
          workspace: { id: 'ws', name: 'Disk-only workspace', observed_state: 'unknown' },
          dependencies: [],
          aliases: [],
          allocated_bytes: null,
        },
      ],
    },
  ] as unknown as StorageRuntime[]
  const topology = projectRunnerTopology(buildRunnerTopology(runtimes, []))
  const wrapper = mount(RunnerDependencyMap, {
    props: {
      runtimes: [],
      generations: [],
      topology,
      selectedKey: JSON.stringify(['qemu', 'physical', 'disk']),
      vmMetrics: {
        ws: {
          cpu_usage_percent: 25,
          ram_used_bytes: 1024,
          ram_total_bytes: 4096,
          disk_used_bytes: 0,
          disk_total_bytes: 4096,
        },
      },
    },
  })
  const card = wrapper.get('button[aria-label="Inspect Disk-only workspace"]')
  expect(card.attributes('aria-pressed')).toBe('true')
  expect(card.text()).toContain('Used 1.0 KiB · partial (1 unknown)')
  expect(card.text()).toContain('Disk capacity 8.0 KiB')
  expect(card.text()).toContain('Observed: unknown')
  expect(card.text()).not.toContain('VM CPU')
  await wrapper.get('input').setValue('member-alias')
  expect(wrapper.text()).toContain('1 matches')
  await card.trigger('click')
  expect(wrapper.emitted('select')?.[0]?.[0]).toEqual(topology.nodes[0])
  wrapper.unmount()
})

it('hides shared and foreign exceptions behind Other resources unless selected or searched', async () => {
  const runtimes = [
    {
      runtime_type: 'qemu',
      resources: [
        {
          physical_id: 'a',
          kind: 'workspace',
          workspace: { id: 'a', name: 'A' },
          dependencies: ['shared'],
          aliases: [],
        },
        {
          physical_id: 'b',
          kind: 'workspace',
          workspace: { id: 'b', name: 'B' },
          dependencies: ['shared'],
          aliases: [],
        },
        { physical_id: 'shared', kind: 'disk', dependencies: [], aliases: [] },
        {
          physical_id: 'foreign',
          kind: 'workspace',
          managed: false,
          dependencies: [],
          aliases: [],
        },
      ],
    },
  ] as unknown as StorageRuntime[]
  const wrapper = mount(RunnerDependencyMap, { props: { runtimes, generations: [] } })
  expect(wrapper.text()).toContain('Other resources (2)')
  expect(wrapper.find('section[aria-label="Other resources"]').exists()).toBe(false)
  await wrapper
    .findAll('button')
    .find((button) => button.text().includes('Other resources (2)'))!
    .trigger('click')
  expect(wrapper.get('section[aria-label="Other resources"]').text()).toContain('foreign')
  expect(wrapper.find('button[aria-label="Inspect shared"]').exists()).toBe(true)
  await wrapper
    .findAll('button')
    .find((button) => button.text().includes('Other resources (2)'))!
    .trigger('click')
  await wrapper.setProps({ selectedKey: JSON.stringify(['qemu', 'physical', 'shared']) })
  expect(wrapper.find('button[aria-label="Inspect shared"]').exists()).toBe(true)
  wrapper.unmount()
})
