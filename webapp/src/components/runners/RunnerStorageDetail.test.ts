import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest'
import { defineComponent, ref } from 'vue'
import type { Runner } from '@/types'
import { RunnerStatus } from '@/types'
import type {
  RunnerStorage,
  StorageGeneration,
  StorageResource,
  DeletionRequest,
} from '@/types/runnerStorage'
import RunnerStorageDetail from './RunnerStorageDetail.vue'
import RunnerDependencyMap from './RunnerDependencyMap.vue'
import RunnerStorageDonut from './RunnerStorageDonut.vue'
import RunnerNodeInspector from './RunnerNodeInspector.vue'
import type { RunnerTopology } from '@/lib/runnerTopology'
import { buildRunnerTopology } from '@/lib/runnerTopology'

const state = vi.hoisted(() => ({
  load: vi.fn(),
  refresh: vi.fn(),
  rebuild: vi.fn(),
  cancel: vi.fn(),
  auth: { isAdmin: true },
}))
const data = ref<RunnerStorage | null>(null)
const deletions = ref<DeletionRequest[]>([])
const error = ref('')
const loading = ref(false)
const refreshPending = ref(false)
vi.mock('@/composables/useRunnerStorage', async (original) => ({
  ...(await original<object>()),
  useRunnerStorage: () => ({
    data,
    deletions,
    error,
    loading,
    refreshPending,
    load: state.load,
    refresh: state.refresh,
  }),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => state.auth }))
vi.mock('@/services/workspaces.api', () => ({ updateRunnerImageBuild: state.rebuild }))
vi.mock('@/services/runnerStorage.api', () => ({ cancelDeletion: state.cancel }))
vi.mock('./RunnerResourceOverview.vue', () => ({
  default: { template: '<section aria-label="Resource overview">Resource overview</section>' },
}))
vi.mock('./RunnerDependencyMap.vue', () => ({
  default: defineComponent({
    props: ['runtimes', 'generations', 'selectedKey', 'topology'],
    emits: ['select'],
    template: '<section aria-label="Dependency map">Dependency map</section>',
  }),
}))
vi.mock('./RunnerOperationsPanel.vue', () => ({ default: { template: '<div />' } }))
vi.mock('./EditRunnerResourcesDialog.vue', () => ({
  default: { template: '<button>Edit runner resources</button>' },
}))
vi.mock('@/components/images/ImageDeletionDialog.vue', () => ({
  default: defineComponent({
    props: ['target', 'name'],
    emits: ['close', 'requested'],
    template:
      '<section v-if="target" aria-label="Deletion confirmation"><span>{{ target.target_type }}:{{ target.target_id }} · {{ name }}</span><button @click="$emit(\'requested\')">Request deletion</button><button @click="$emit(\'close\')">Close deletion</button></section>',
  }),
}))

function generation(patch: Partial<StorageGeneration> = {}): StorageGeneration {
  return {
    id: 'current',
    name: 'Base v2',
    owner_label: 'Admin',
    runtime_type: 'qemu',
    definition_id: 'definition',
    definition_name: 'Recipe',
    build_job_id: 'assignment',
    generation: 2,
    status: 'ready',
    assignment_status: 'active',
    runner_ref: '/images/base.qcow2',
    size_bytes: null,
    size_source: 'unknown',
    origin_type: 'definition_build',
    revision_id: 'revision-secret',
    is_legacy: false,
    is_current: true,
    is_pending: false,
    observed_state: 'unknown',
    dependencies: [],
    ...patch,
  }
}
function resource(patch: Partial<StorageResource> = {}): StorageResource {
  return {
    physical_id: '/images/base.qcow2',
    kind: 'image',
    managed: true,
    state: 'unknown',
    allocated_bytes: 0,
    logical_bytes: null,
    virtual_bytes: null,
    shared_bytes: null,
    reclaimable_bytes: null,
    aliases: [],
    dependencies: [],
    image_id: 'current',
    workspace: null,
    provenance: 'confirmed inventory',
    ...patch,
  }
}
function deletion(patch: Partial<DeletionRequest> = {}): DeletionRequest {
  return {
    id: 'request',
    target_type: 'image',
    target_id: 'current',
    mode: 'deferred',
    phase: 'waiting',
    diagnostic: '',
    can_cancel: true,
    fingerprint: 'fingerprint',
    approval: {
      fingerprint: 'fingerprint',
      blockers: [],
      counts: { images: 1, workspaces: 0 },
      images: [{ id: 'current', name: 'Base v2', owner_id: null, runner_id: 'r' }],
      workspaces: [],
    },
    ...patch,
  }
}
const runner: Runner = {
  id: 'r',
  name: 'Runner',
  status: RunnerStatus.OFFLINE,
  available_runtimes: ['qemu'],
  qemu_min_vcpus: 1,
  qemu_max_vcpus: 8,
  qemu_default_vcpus: 2,
  qemu_min_memory_mb: 512,
  qemu_max_memory_mb: 8192,
  qemu_default_memory_mb: 2048,
  qemu_min_disk_size_gb: 1,
  qemu_max_disk_size_gb: 100,
  qemu_default_disk_size_gb: 20,
  qemu_max_active_vcpus: null,
  qemu_max_active_memory_mb: null,
  qemu_max_active_disk_size_gb: null,
  organization_id: 'org',
  connected_at: null,
  disconnected_at: null,
  created_at: '2026-10-01',
  updated_at: '2026-10-01',
}
let w: VueWrapper
beforeEach(() => {
  vi.resetAllMocks()
  state.auth.isAdmin = true
  error.value = ''
  loading.value = false
  refreshPending.value = false
  deletions.value = []
  data.value = {
    runner_id: 'r',
    runner_online: false,
    latest_complete: false,
    latest_snapshot_id: 4,
    runtimes: [
      {
        runtime_type: 'qemu',
        fresh: false,
        snapshot_id: 4,
        collected_at: '2026-10-01',
        received_at: '2026-10-01',
        filesystems: [
          { path: '/images', used_bytes: null, capacity_bytes: 0, available_bytes: null },
        ],
        resources: [resource()],
        diagnostics: {
          complete: false,
          errors: ['inspection failed'],
          foreign_resource_count: 3,
          collected_at: '2026-10-01',
        },
      },
    ],
    generations: [
      generation({
        id: 'old',
        name: 'Base v1',
        generation: 1,
        is_current: false,
        runner_ref: '/images/old',
        dependencies: [
          {
            id: 'ws',
            name: 'Pinned workspace',
            owner_id: 'alice',
            owner_label: 'Alice',
            status: 'stopped',
            observed_state: 'unknown',
            last_activity_at: '2026-10-01',
          },
        ],
      }),
      generation(),
    ],
    operations: [],
    capture_requests: [],
  }
})
afterEach(() => {
  w?.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
})
async function setup(selectedRunner: Runner = runner) {
  w = mount(RunnerStorageDetail, {
    props: { runner: selectedRunner },
    attachTo: document.body,
    global: {
      stubs: {
        RouterLink: defineComponent({
          props: ['to'],
          template: '<a :href="to" @click.prevent><slot /></a>',
        }),
      },
    },
  })
  await flushPromises()
}
function button(text: string) {
  return w.findAll('button').find((b) => b.text().includes(text))!
}
async function tab(name: string) {
  const target = w.findAll('[role="tab"]').find((t) => t.text().startsWith(name))!
  await target.trigger('mousedown', { button: 0 })
  await target.trigger('click')
  await flushPromises()
}
async function inspect(name = 'Base v2') {
  await tab('Inventory')
  await w.get(`[aria-label="Inspect image ${name}"]`).trigger('click')
  await flushPromises()
}
async function menu(text: string) {
  await w.get('[aria-label="Image actions"]').trigger('keydown', { key: 'Enter' })
  await flushPromises()
  const item = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((el) =>
    el.textContent?.includes(text),
  )!
  expect(item).toBeTruthy()
  item.click()
  await flushPromises()
}
function deferred() {
  let resolve!: () => void
  let reject!: (e: Error) => void
  const promise = new Promise<void>((a, b) => {
    resolve = a
    reject = b
  })
  return { promise, resolve, reject }
}

describe('runner cockpit progressive disclosure and safety', () => {
  it('defaults to Overview with cached offline evidence; selection reveals owner/activity, not upfront identifiers', async () => {
    await setup()
    expect(w.get('[role="tab"][data-state="active"]').text()).toBe('Overview')
    expect(w.text()).toContain('Runner offline · Last confirmed inventory')
    expect(w.text()).not.toContain('inspection failed')
    expect(w.text()).not.toContain('revision-secret')
    expect(w.find('[aria-label="Selected resource"]').exists()).toBe(false)
    await inspect('Base v1')
    const inspector = w.get('[aria-label="Selected resource"]')
    for (const text of [
      'History',
      'Observed: unknown',
      'Lifecycle: ready',
      'Pinned workspace',
      'Alice',
      '2026-10-01',
      'Unknown',
    ])
      expect(inspector.text()).toContain(text)
    expect(inspector.text()).not.toContain('Current default')
    expect(inspector.text()).not.toContain('revision-secret')
    await button('Back to runners').trigger('click')
    expect(w.emitted('back')).toHaveLength(1)
  })
  it('keeps diagnostics collapsed until the attention shortcut and evidence expansion', async () => {
    await setup()
    await button('Inventory diagnostics').trigger('click')
    await flushPromises()
    expect(w.get('[role="tab"][data-state="active"]').text()).toBe('Inventory')
    expect(w.text()).not.toContain('inspection failed')
    const trigger = button('Runner offline')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    await trigger.trigger('click')
    await flushPromises()
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(w.text()).toContain('inspection failed')
    expect(w.text()).toContain('cannot prove absence')
  })
  it('excludes tombstones from stored counts and exposes read-only deleted history', async () => {
    data.value!.generations.push(
      generation({
        id: 'deleted',
        name: 'Deleted base',
        status: 'deleted',
        is_pending: true,
        assignment_status: 'deleted',
      }),
    )
    await setup()
    expect(w.text()).toContain('2 images')
    await tab('Inventory')
    expect(w.text()).toContain('Base / build images · 2')
    expect(w.text()).not.toContain('Deleted base')
    await button('Show deleted history').trigger('click')
    await inspect('Deleted base')
    const inspector = w.get('[aria-label="Selected resource"]')
    for (const text of [
      'Current default',
      'Pending attempt',
      'Build new generation',
      'Delete this generation',
      'Delete runner assignment',
    ])
      expect(inspector.text()).not.toContain(text)
    expect(inspector.find('[aria-label="Image actions"]').exists()).toBe(false)
  })
  it.each(['deleted', 'retired', 'deactivated'])(
    'does not rebuild or remove a %s assignment',
    async (assignment_status) => {
      data.value!.generations[1] = generation({ assignment_status })
      await setup()
      await inspect()
      expect(w.get('[aria-label="Selected resource"]').text()).not.toContain('Current default')
      expect(button('Build new generation')).toBeUndefined()
      await menu('Delete this generation')
      expect(w.get('[aria-label="Deletion confirmation"]').text()).toContain('image:current')
      expect(document.body.textContent).not.toContain('Delete runner assignment')
    },
  )
  it('never invents capture revisions even when technical details are expanded', async () => {
    data.value!.generations.push(
      generation({
        id: 'capture',
        name: 'Independent capture',
        origin_type: 'workspace_capture',
        is_current: false,
        generation: null,
        revision_id: null,
        definition_id: null,
        definition_name: null,
        build_job_id: null,
        runner_ref: '/capture',
      }),
    )
    await setup()
    await inspect('Independent capture')
    await button('Technical details').trigger('click')
    await flushPromises()
    const inspector = w.get('[aria-label="Selected resource"]')
    expect(inspector.text()).toContain('Capture')
    for (const text of ['History', 'Revision', 'Definition', 'Unknown legacy recipe'])
      expect(inspector.text()).not.toContain(text)
  })
  it.each([
    ['Delete this generation', 'image:current', 'Base v2'],
    ['Delete runner assignment', 'assignment:assignment', 'Recipe'],
  ])(
    'routes %s to the correct dialog target/name and reloads after request',
    async (action, target, name) => {
      await setup()
      await inspect()
      expect(w.find('[aria-label="Deletion confirmation"]').exists()).toBe(false)
      await menu(action)
      expect(w.get('[aria-label="Deletion confirmation"]').text()).toContain(`${target} · ${name}`)
      await button('Request deletion').trigger('click')
      expect(state.load).toHaveBeenCalledTimes(1)
      await button('Close deletion').trigger('click')
      expect(w.find('[aria-label="Deletion confirmation"]').exists()).toBe(false)
    },
  )
  it('gates duplicate rebuilds while pending and shows deferred failure without reloading', async () => {
    const request = deferred()
    state.rebuild.mockReturnValue(request.promise)
    await setup()
    await inspect()
    await button('Build new generation').trigger('click')
    await button('Build new generation').trigger('click')
    expect(state.rebuild).toHaveBeenCalledExactlyOnceWith('definition', 'r', { action: 'rebuild' })
    expect(button('Build new generation').attributes('disabled')).toBeDefined()
    request.reject(new Error('Rebuild refused'))
    await flushPromises()
    expect(w.get('[role="alert"]').text()).toContain('Rebuild refused')
    expect(state.load).not.toHaveBeenCalled()
  })
  it('disables rebuilding when the same assignment already has a pending attempt', async () => {
    data.value!.generations.push(
      generation({
        id: 'pending',
        name: 'Next attempt',
        is_current: false,
        is_pending: true,
        status: 'building',
      }),
    )
    await setup()
    await inspect()
    expect(button('Build new generation').attributes('disabled')).toBeDefined()
    await button('Build new generation').trigger('click')
    expect(state.rebuild).not.toHaveBeenCalled()
  })
  it('refreshes only once while queued and surfaces inspection errors with retry', async () => {
    const request = deferred()
    state.refresh.mockReturnValue(request.promise)
    await setup()
    await button('Refresh inventory').trigger('click')
    await button('Refresh inventory').trigger('click')
    expect(state.refresh).toHaveBeenCalledTimes(1)
    refreshPending.value = true
    request.resolve()
    await flushPromises()
    expect(button('Scan queued').attributes('disabled')).toBeDefined()
    expect(w.text()).toContain('awaiting a new complete inventory')
    await button('Scan queued').trigger('click')
    expect(state.refresh).toHaveBeenCalledTimes(1)
    error.value = 'Scan transport failed'
    await flushPromises()
    expect(w.get('[role="alert"]').text()).toContain('Scan transport failed')
    await button('Retry inspection').trigger('click')
    expect(state.load).toHaveBeenCalledTimes(1)
    data.value = { ...data.value!, latest_snapshot_id: 5, latest_complete: true }
    refreshPending.value = false
    error.value = ''
    await flushPromises()
    expect(button('Refresh inventory').attributes('disabled')).toBeUndefined()
  })
  it('shows terminal deletion truth and capture restart suppression only on Activities', async () => {
    deletions.value = [
      deletion({ id: 'cancelled', phase: 'cancelled', can_cancel: false }),
      deletion({ id: 'completed', phase: 'completed', can_cancel: false }),
    ]
    data.value!.capture_requests = [
      {
        id: 'capture-request',
        workspace_id: 'ws',
        image_id: 'current',
        phase: 'intervention_required',
        diagnostic: 'Capture evidence changed',
        resume_suppressed: true,
      },
    ]
    await setup()
    expect(w.text()).not.toContain('Cancelled — resources retained')
    await tab('Activities')
    for (const text of [
      'Cancelled — resources retained',
      'Completed',
      'Restart suppressed',
      'Capture evidence changed',
    ])
      expect(w.text()).toContain(text)
    expect(w.text()).not.toContain('Waiting for server evidence')
    expect(button('Cancel pending request')).toBeUndefined()
  })
  it('guards duplicate cancellation and preserves deferred failure truth', async () => {
    deletions.value = [deletion()]
    const request = deferred()
    state.cancel.mockReturnValue(request.promise)
    await setup()
    await tab('Activities')
    await button('Cancel pending request').trigger('click')
    await button('Cancel pending request').trigger('click')
    expect(state.cancel).toHaveBeenCalledExactlyOnceWith('request')
    expect(button('Cancel pending request').attributes('disabled')).toBeDefined()
    request.reject(new Error('Cancellation refused'))
    await flushPromises()
    expect(w.get('[role="alert"]').text()).toContain('Cancellation refused')
    expect(state.load).not.toHaveBeenCalled()
  })
  it('reloads successful rebuild/cancellation and resolves a selected stable key against the latest snapshot', async () => {
    await setup()
    await inspect()
    await button('Build new generation').trigger('click')
    await flushPromises()
    expect(state.load).toHaveBeenCalledTimes(1)
    data.value = {
      ...data.value!,
      runtimes: [
        {
          ...data.value!.runtimes[0]!,
          resources: [resource({ allocated_bytes: 1024, state: 'present' })],
        },
      ],
    }
    await flushPromises()
    expect(w.get('[aria-label="Selected resource"]').text()).toContain('Observed: present')
    expect(w.get('[aria-label="Selected resource"]').text()).toContain('1.0 KiB')
    deletions.value = [deletion()]
    await tab('Activities')
    await button('Cancel pending request').trigger('click')
    await flushPromises()
    expect(state.load).toHaveBeenCalledTimes(2)
  })
  it('does not mount admin resource editing for a non-admin', async () => {
    state.auth.isAdmin = false
    await setup()
    expect(button('Edit runner resources')).toBeUndefined()
  })
})

describe('retained generation selection', () => {
  it('selects a generation without its runtime snapshot from Inventory and from the map', async () => {
    data.value!.runtimes = [{ ...data.value!.runtimes[0]!, runtime_type: 'docker', resources: [] }]
    await setup()
    const map = w.findComponent(RunnerDependencyMap)
    expect(map.props('generations')).toEqual(data.value!.generations)
    await inspect()
    expect(w.get('[aria-label="Selected resource"]').text()).toContain('Base v2')
    expect(button('Build new generation')).toBeDefined()
    await w.get('[aria-label="Close resource details"]').trigger('click')
    await tab('Overview')
    const node = buildRunnerTopology(data.value!.runtimes, data.value!.generations).nodes.find(
      (n) => n.generation?.id === 'current',
    )!
    expect(node.resource).toBeUndefined()
    w.findComponent(RunnerDependencyMap).vm.$emit('select', node)
    await flushPromises()
    expect(w.get('[aria-label="Selected resource"]').text()).toContain('Base v2')
    expect(button('Build new generation').attributes('disabled')).toBeUndefined()
    await menu('Delete this generation')
    expect(w.get('[aria-label="Deletion confirmation"]').text()).toContain(
      'image:current · Base v2',
    )
  })

  it('inspects the active generation when a deleted-first tombstone shares its physical reference', async () => {
    data.value!.generations.unshift(
      generation({
        id: 'deleted',
        name: 'Deleted collision',
        status: 'deleted',
        assignment_status: 'deleted',
      }),
    )
    data.value!.runtimes[0]!.resources = [resource({ image_id: null, state: 'present' })]
    await setup()
    const map = w.findComponent(RunnerDependencyMap)
    const graph = buildRunnerTopology(map.props('runtimes'), data.value!.generations)
    const physical = graph.nodes.find((n) => n.resource?.physical_id === '/images/base.qcow2')!
    expect(physical.generation?.id).toBe('current')
    map.vm.$emit('select', physical)
    await flushPromises()
    const inspector = w.get('[aria-label="Selected resource"]')
    expect(inspector.text()).toContain('Base v2')
    expect(inspector.text()).toContain('Current default')
    expect(inspector.text()).not.toContain('Deleted collision')
    await menu('Delete this generation')
    expect(w.get('[aria-label="Deletion confirmation"]').text()).toContain(
      'image:current · Base v2',
    )
  })
})

describe('effective cached inventory freshness', () => {
  beforeEach(() => {
    // Only the clock and its interval are fake: menu/flushPromises scheduling stays real.
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(new Date('2026-10-03T12:00:00Z'))
    data.value!.runner_online = true
    data.value!.latest_complete = true
    const timestamp = new Date().toISOString()
    data.value!.runtimes[0] = {
      ...data.value!.runtimes[0]!,
      fresh: true,
      collected_at: timestamp,
      received_at: timestamp,
    }
  })

  async function onlineSetup() {
    await setup()
    await w.setProps({ runner: { ...runner, status: RunnerStatus.ONLINE } })
  }
  function effectiveRuntime() {
    return w.findComponent(RunnerDependencyMap).props('runtimes')[0]!
  }

  it('expires confirmed freshness as the clock advances without a new snapshot', async () => {
    await onlineSetup()
    expect(effectiveRuntime().fresh).toBe(true)
    expect(w.text()).toContain('Recent inventory · Cached, not live')
    await vi.advanceTimersByTimeAsync(315_000)
    expect(effectiveRuntime().fresh).toBe(false)
    expect(effectiveRuntime().resources).toEqual(data.value!.runtimes[0]!.resources)
    expect(data.value!.runtimes[0]!.fresh).toBe(true)
    expect(w.text()).toContain('Stale inventory')
    expect(w.text()).not.toContain('Recent inventory')
  })

  it.each(['runner offline', 'server offline', 'server stale', 'inspection error'])(
    'mutes server freshness for %s while retaining graph evidence',
    async (reason) => {
      await onlineSetup()
      expect(effectiveRuntime().fresh).toBe(true)
      if (reason === 'runner offline') await w.setProps({ runner })
      if (reason === 'server offline') data.value!.runner_online = false
      if (reason === 'server stale') data.value!.runtimes[0]!.fresh = false
      if (reason === 'inspection error') error.value = 'Transport failed'
      await flushPromises()
      expect(effectiveRuntime().fresh).toBe(false)
      expect(effectiveRuntime().resources).toEqual(data.value!.runtimes[0]!.resources)
      const map = w.findComponent(RunnerDependencyMap)
      const selected = buildRunnerTopology(
        map.props('runtimes'),
        map.props('generations'),
      ).nodes.find((n) => n.generation?.id === 'current')!
      map.vm.$emit('select', selected)
      await flushPromises()
      expect(w.get('[aria-label="Selected resource"]').text()).toContain('Observed: unknown')
      expect(w.text()).not.toContain('Recent inventory')
      await tab('Inventory')
      await button(reason.includes('offline') ? 'Runner offline' : 'Stale inventory').trigger(
        'click',
      )
      await flushPromises()
      expect(w.text()).toContain('Stale / unknown inventory')
      expect(w.text()).not.toContain('Recent confirmed inventory')
    },
  )

  it.each([
    ['collected_at', null],
    ['received_at', null],
    ['collected_at', 'invalid'],
    ['received_at', 'invalid'],
    ['collected_at', '2026-10-03T11:54:59Z'],
    ['received_at', '2026-10-03T11:54:59Z'],
    ['collected_at', '2026-10-03T12:00:01Z'],
    ['received_at', '2026-10-03T12:00:01Z'],
  ] as const)('requires recent non-future %s (%s)', async (field, timestamp) => {
    data.value!.runtimes[0]![field] = timestamp
    await onlineSetup()
    expect(effectiveRuntime().fresh).toBe(false)
    expect(w.text()).toContain('Stale inventory')
    expect(data.value!.runtimes[0]!.fresh).toBe(true)
  })
})

describe('storage chart selection integration', () => {
  const KiB = 1024
  const workspaceKey = JSON.stringify(['qemu', 'workspace', 'ws'])
  const imageKey = JSON.stringify(['qemu', 'generation', 'current'])

  beforeEach(() => {
    const workspace = {
      id: 'ws',
      name: 'Measured workspace',
      owner_id: 'alice',
      owner_label: 'Alice',
      status: 'stopped',
      observed_state: 'present',
      last_activity_at: '2026-10-01',
    }
    data.value!.generations = [generation({ dependencies: [workspace] })]
    data.value!.runtimes[0] = {
      ...data.value!.runtimes[0]!,
      filesystems: [
        {
          filesystem_id: 'disk-fs',
          path: '/images',
          capacity_bytes: 100 * KiB,
          used_bytes: 50 * KiB,
          available_bytes: 50 * KiB,
        },
      ],
      resources: [
        resource({
          filesystem_id: 'disk-fs',
          file_identity: 'base-inode',
          allocated_bytes: 10 * KiB,
          state: 'present',
        }),
        resource({
          physical_id: 'domain:ws',
          kind: 'workspace',
          image_id: null,
          allocated_bytes: null,
          workspace,
          state: 'present',
          dependencies: ['/workspaces/ws/disk.qcow2', '/workspaces/ws/seed.iso'],
        }),
        resource({
          physical_id: '/workspaces/ws/disk.qcow2',
          kind: 'disk',
          image_id: null,
          filesystem_id: 'disk-fs',
          file_identity: 'disk-inode',
          allocated_bytes: 20 * KiB,
          logical_bytes: 40 * KiB,
          virtual_bytes: 80 * KiB,
          workspace,
          state: 'present',
          dependencies: ['/images/base.qcow2'],
        }),
        resource({
          physical_id: '/workspaces/ws/seed.iso',
          kind: 'file',
          image_id: null,
          filesystem_id: 'disk-fs',
          file_identity: 'seed-inode',
          allocated_bytes: 2 * KiB,
          logical_bytes: 2 * KiB,
          virtual_bytes: 2 * KiB,
          workspace,
          state: 'present',
        }),
      ],
    }
  })

  function metric(label: string) {
    const term = w
      .get('[aria-label="Selected resource"]')
      .findAll('dt')
      .find((item) => item.text() === label)!
    return term.element.nextElementSibling?.textContent?.trim()
  }
  function graph(): RunnerTopology {
    return w.getComponent(RunnerDependencyMap).props('topology')!
  }
  function assertSharedSelection(key: string, label: string) {
    const donut = w.getComponent(RunnerStorageDonut)
    expect(w.getComponent(RunnerDependencyMap).props('selectedKey')).toBe(key)
    expect(donut.props('selectedKey')).toBe(key)
    expect(donut.props('topology')).toBe(graph())
    expect(donut.find('path[aria-pressed="true"]').attributes('aria-label')).toContain(`${label}:`)
  }
  function assertGraph() {
    expect(
      graph()
        .nodes.map((node) => node.key)
        .sort(),
    ).toEqual([imageKey, workspaceKey].sort())
    expect(graph().edges).toEqual([{ from: workspaceKey, to: imageKey, kind: 'physical' }])
  }
  async function selectSlice(label: string) {
    const donut = w.getComponent(RunnerStorageDonut)
    const slice = donut
      .findAll('path[role="button"]')
      .find((path) => path.attributes('aria-label')?.startsWith(`${label}:`))!
    expect(slice).toBeDefined()
    await slice.trigger('click')
    await flushPromises()
    return donut
  }

  it('resolves real donut selection to a projected workspace with real inspector metrics and shared map props', async () => {
    await setup()
    const donut = await selectSlice('Measured workspace')
    const projected = graph().nodes.find((node) => node.key === workspaceKey)!
    expect(donut.emitted('select')![0]![0]).toBe(projected)
    expect(w.getComponent(RunnerNodeInspector).props('node')).toBe(projected)
    expect(projected.storage!.resources).toHaveLength(2)
    expect(projected.resource!.physical_id).toBe('domain:ws')
    expect(metric('Used storage')).toBe('22.0 KiB')
    expect(metric('Disk capacity')).toBe('80.0 KiB')
    expect(metric('File size')).toBe('40.0 KiB')
    assertSharedSelection(workspaceKey, 'Measured workspace')
    assertGraph()
  })

  it('updates selected metrics from replacement resources without duplicating graph nodes or edges', async () => {
    await setup()
    await selectSlice('Measured workspace')
    const previousNode = w.getComponent(RunnerNodeInspector).props('node')
    data.value = {
      ...data.value!,
      latest_snapshot_id: 5,
      runtimes: data.value!.runtimes.map((runtime) => ({
        ...runtime,
        snapshot_id: 5,
        resources: runtime.resources.map((member) => ({
          ...member,
          ...(member.kind === 'disk'
            ? { allocated_bytes: 30 * KiB, logical_bytes: 45 * KiB, virtual_bytes: 90 * KiB }
            : {}),
        })),
      })),
    }
    await flushPromises()
    const selected = w.getComponent(RunnerNodeInspector).props('node')
    expect(selected).not.toBe(previousNode)
    expect(selected.key).toBe(workspaceKey)
    expect(selected).toBe(graph().nodes.find((node) => node.key === workspaceKey))
    expect(metric('Used storage')).toBe('32.0 KiB')
    expect(metric('Disk capacity')).toBe('90.0 KiB')
    expect(metric('File size')).toBe('45.0 KiB')
    assertSharedSelection(workspaceKey, 'Measured workspace')
    assertGraph()
    expect(
      w.getComponent(RunnerStorageDonut).find('path[aria-pressed="true"]').attributes('aria-label'),
    ).toContain('32.0 KiB')
  })

  it('inspects disk and seed separately in Inventory and highlights their projected workspace back on Overview', async () => {
    await setup()
    for (const [label, id, allocated] of [
      ['Measured workspace · Workspace disk', '/workspaces/ws/disk.qcow2', '20.0 KiB'],
      ['Measured workspace · Boot seed', '/workspaces/ws/seed.iso', '2.0 KiB'],
    ]) {
      await tab('Inventory')
      if (!w.find(`[aria-label="Inspect resource ${label}"]`).exists()) {
        await button('Physical resources').trigger('click')
        await flushPromises()
      }
      await w.get(`[aria-label="Inspect resource ${label}"]`).trigger('click')
      await flushPromises()
      const selected = w.getComponent(RunnerNodeInspector).props('node')
      expect(selected.resource!.physical_id).toBe(id)
      expect(selected.storage).toBeUndefined()
      expect(metric('Allocated')).toBe(allocated)
      const physicalKey = selected.key
      await tab('Overview')
      expect(graph().physicalToPresentation!.get(physicalKey)).toBe(workspaceKey)
      assertSharedSelection(workspaceKey, 'Measured workspace')
      // Physical evidence remains selected in the inspector; the graph highlights its workspace.
      expect(w.getComponent(RunnerNodeInspector).props('node').key).toBe(physicalKey)
    }
  })

  it('retains a donut-selected image when generation-only evidence gains a physical resource', async () => {
    const base = data.value!.runtimes[0]!.resources[0]!
    data.value!.runtimes[0]!.resources = data.value!.runtimes[0]!.resources.slice(1)
    await setup()
    const donut = w.getComponent(RunnerStorageDonut)
    // An unmeasured image still has an inspectable legend entry.
    await donut
      .findAll('button')
      .find((item) => item.text().includes('Base v2'))!
      .trigger('click')
    await flushPromises()
    await donut.get('[aria-label="Inspect storage Base v2"]').trigger('click')
    await flushPromises()
    expect(donut.emitted('select')![0]![0]).toBe(
      graph().nodes.find((node) => node.key === imageKey),
    )
    expect(w.getComponent(RunnerNodeInspector).props('node').key).toBe(imageKey)
    expect(w.getComponent(RunnerNodeInspector).props('node').resource).toBeUndefined()
    data.value = {
      ...data.value!,
      runtimes: data.value!.runtimes.map((runtime) => ({
        ...runtime,
        resources: [{ ...base }, ...runtime.resources],
      })),
    }
    await flushPromises()
    const selected = w.getComponent(RunnerNodeInspector).props('node')
    expect(selected.key).toBe(imageKey)
    expect(selected.generation!.id).toBe('current')
    expect(selected.resource!.physical_id).toBe(base.physical_id)
    expect(metric('Allocated')).toBe('10.0 KiB')
    assertSharedSelection(imageKey, 'Base v2')
    assertGraph()
  })

  it('never mounts the QEMU chart for a Docker-only runner', async () => {
    data.value!.generations = [generation({ runtime_type: 'docker', dependencies: [] })]
    data.value!.runtimes = [{ ...data.value!.runtimes[0]!, runtime_type: 'docker', resources: [] }]
    await setup({ ...runner, available_runtimes: ['docker'] })
    expect(w.findComponent(RunnerStorageDonut).exists()).toBe(false)
    expect(w.find('[aria-label="QEMU storage breakdown"]').exists()).toBe(false)
    await tab('Inventory')
    await tab('Overview')
    expect(w.findComponent(RunnerStorageDonut).exists()).toBe(false)
  })
})
