import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect, vi } from 'vitest'
import { ref } from 'vue'
import RunnerStorageDetail from './RunnerStorageDetail.vue'
const deletions = ref([])
const data = ref({
  runner_online: false,
  latest_complete: false,
  latest_snapshot_id: 4,
  runtimes: [
    {
      runtime_type: 'qemu',
      fresh: false,
      collected_at: 'yesterday',
      received_at: 'yesterday',
      filesystems: [
        { path: '/images', used_bytes: null, capacity_bytes: 0, available_bytes: null },
      ],
      resources: [],
      diagnostics: { complete: false, errors: ['inspection failed'], foreign_resource_count: 3 },
    },
  ],
  generations: [
    {
      id: 'old',
      name: 'Base v1',
      origin_type: 'definition_build',
      is_current: false,
      is_pending: false,
      generation: 1,
      status: 'ready',
      observed_state: 'unknown',
      size_bytes: null,
      definition_name: 'Recipe',
      dependencies: [
        {
          id: 'ws',
          name: 'Pinned workspace',
          owner_label: 'Alice',
          status: 'stopped',
          observed_state: 'unknown',
          last_activity_at: '2026-10-01',
        },
      ],
    },
    {
      id: 'current',
      name: 'Base v2',
      origin_type: 'definition_build',
      is_current: true,
      is_pending: false,
      generation: 2,
      status: 'ready',
      dependencies: [],
    },
  ],
  operations: [],
  capture_requests: [],
})
vi.mock('@/composables/useRunnerStorage', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useRunnerStorage: () => ({
    data,
    deletions,
    error: ref(''),
    loading: ref(false),
    refreshPending: ref(false),
    load: vi.fn(),
    refresh: vi.fn(),
  }),
}))
vi.mock('./RunnerOperationsPanel.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/images/ImageDeletionDialog.vue', () => ({ default: { template: '<div />' } }))
it('shows cached offline/partial evidence, unknown sizes, history and pinned owner/activity', async () => {
  const w = mount(RunnerStorageDetail, {
    props: { runner: { id: 'r', name: 'Runner' } as never },
    global: { stubs: { CollapsibleContent: { template: '<div><slot /></div>' } } },
  })
  await flushPromises()
  expect(w.text()).toContain('Runner offline')
  expect(w.text()).toContain('History')
  expect(w.text()).toContain('Current default')
  expect(w.text()).toContain('Unknown')
  expect(w.text()).toContain('0 B')
  expect(w.text()).toContain('Pinned workspace')
  expect(w.text()).toContain('Alice')
  expect(w.text()).toContain('2026-10-01')
  expect(w.text()).toContain('inspection failed')
  await w
    .findAll('button')
    .find((b) => b.text() === 'Back to runners')!
    .trigger('click')
  expect(w.emitted('back')).toHaveLength(1)
  w.unmount()
})

it('labels captures without inventing a recipe and shows terminal deletion truth', async () => {
  data.value.generations.push({
    id: 'capture',
    name: 'Independent capture',
    origin_type: 'workspace_capture',
    is_current: false,
    is_pending: false,
    generation: null,
    revision_id: null,
    runtime_type: 'qemu',
    status: 'ready',
    dependencies: [],
  } as never)
  deletions.value = [
    { id: 'cancelled', phase: 'cancelled', diagnostic: '', can_cancel: false },
    { id: 'completed', phase: 'completed', diagnostic: '', can_cancel: false },
  ] as never
  const w = mount(RunnerStorageDetail, {
    props: { runner: { id: 'r', name: 'Runner' } as never },
  })
  await flushPromises()
  const card = w
    .findAll('[data-slot="card"]')
    .find((c) => c.text().includes('Independent capture'))!
  expect(card.text()).toContain('Capture')
  expect(card.text()).not.toContain('History')
  expect(card.text()).not.toContain('Revision')
  expect(card.text()).not.toContain('unknown legacy recipe')
  expect(w.text()).toContain('Cancelled — resources retained')
  expect(w.text()).toContain('Completed')
  expect(w.text()).not.toContain('Waiting for server evidence')
  w.unmount()
  data.value.generations.pop()
  deletions.value = []
})

it('hides tombstones from stored counts and exposes read-only deleted history', async () => {
  data.value.generations.push({
    id: 'deleted',
    name: 'Deleted base',
    origin_type: 'definition_build',
    status: 'deleted',
    is_current: true,
    is_pending: true,
    build_job_id: 'assignment',
    definition_id: 'definition',
    assignment_status: 'deleted',
    dependencies: [],
  } as never)
  const w = mount(RunnerStorageDetail, { props: { runner: { id: 'r' } as never } })
  await flushPromises()
  expect(w.text()).toContain('Base / build images · 2')
  expect(w.text()).not.toContain('Deleted base')
  await w
    .findAll('button')
    .find((b) => b.text().includes('Show deleted history'))!
    .trigger('click')
  const card = w.findAll('[data-slot="card"]').find((c) => c.text().includes('Deleted base'))!
  expect(card.text()).not.toContain('Current default')
  expect(card.text()).not.toContain('Pending attempt')
  expect(card.text()).not.toContain('Delete this generation')
  expect(card.text()).not.toContain('Delete runner assignment')
  expect(card.text()).not.toContain('Build new generation')
  w.unmount()
  data.value.generations.pop()
})

it.each(['deleted', 'retired', 'deactivated'])(
  'does not label a %s assignment current',
  async (status) => {
    data.value.generations.push({
      id: 'retired',
      name: 'Retired assignment',
      origin_type: 'definition_build',
      status: 'ready',
      is_current: true,
      assignment_status: status,
      build_job_id: 'assignment',
      definition_id: 'definition',
      dependencies: [],
    } as never)
    const w = mount(RunnerStorageDetail, { props: { runner: { id: 'r' } as never } })
    await flushPromises()
    const card = w
      .findAll('[data-slot="card"]')
      .find((c) => c.text().includes('Retired assignment'))!
    expect(card.text()).not.toContain('Current default')
    expect(card.text()).not.toContain('Delete runner assignment')
    expect(card.text()).not.toContain('Build new generation')
    w.unmount()
    data.value.generations.pop()
  },
)
