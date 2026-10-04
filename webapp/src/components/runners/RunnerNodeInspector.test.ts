import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest'
import { defineComponent } from 'vue'
import type { StorageGeneration, StorageResource, StorageWorkspace } from '@/types/runnerStorage'
import type { RunnerTopologyNode } from '@/lib/runnerTopology'
import RunnerNodeInspector from './RunnerNodeInspector.vue'

// Keep Button and Collapsible real; inline menus isolate action gating from portals.
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
const workspace: StorageWorkspace = {
  id: 'workspace-secret',
  name: 'Pinned workspace',
  owner_id: 'alice',
  owner_label: 'Alice',
  status: 'stopped',
  observed_state: 'unknown',
  last_activity_at: '2026-10-01',
}
function image(patch: Partial<StorageGeneration> = {}): StorageGeneration {
  return {
    id: 'image-secret',
    name: 'Base v2',
    owner_label: 'Admin',
    runtime_type: 'qemu',
    definition_id: 'definition',
    definition_name: 'Recipe',
    build_job_id: 'assignment',
    generation: 2,
    status: 'ready',
    assignment_status: 'active',
    runner_ref: '/images/private.qcow2',
    size_bytes: null,
    size_source: 'unknown',
    origin_type: 'definition_build',
    revision_id: 'revision-secret',
    is_legacy: false,
    is_current: true,
    is_pending: false,
    observed_state: 'unknown',
    dependencies: [workspace],
    ...patch,
  }
}
const resource: StorageResource = {
  physical_id: '/images/private.qcow2',
  kind: 'image',
  managed: true,
  state: 'unknown',
  allocated_bytes: 0,
  logical_bytes: null,
  virtual_bytes: null,
  shared_bytes: null,
  reclaimable_bytes: null,
  aliases: ['private-alias'],
  dependencies: [],
  image_id: 'image-secret',
  workspace: null,
  provenance: 'confirmed',
}
let w: VueWrapper
let generation: StorageGeneration
beforeEach(() => {
  vi.restoreAllMocks()
  generation = image()
})
afterEach(() => {
  w?.unmount()
  document.body.innerHTML = ''
})
function setup(
  nodePatch: Partial<RunnerTopologyNode> = {},
  generations = [generation],
  busy = false,
) {
  w = mount(RunnerNodeInspector, {
    attachTo: document.body,
    props: {
      node: {
        key: 'stable-key',
        runtime: 'qemu',
        label: 'Base v2',
        column: 2,
        generation,
        ...nodePatch,
      },
      generations,
      busy,
    },
    global: {
      stubs: {
        RouterLink: defineComponent({
          props: ['to'],
          template: '<a :href="to" @click.prevent><slot /></a>',
        }),
      },
    },
  })
}
function button(text: string) {
  return w.findAll('button').find((b) => b.text().includes(text))!
}
describe('selected runner resource inspector', () => {
  it('separates current default, observed state and lifecycle while hiding identifiers until requested', async () => {
    setup({ resource })
    for (const text of [
      'Current default',
      'Observed: unknown',
      'Lifecycle: ready',
      'Pinned workspace',
      'Alice',
      '2026-10-01',
      'Unknown',
      '0 B',
    ])
      expect(w.text()).toContain(text)
    for (const text of [
      'image-secret',
      '/images/private.qcow2',
      'revision-secret',
      'private-alias',
    ])
      expect(w.text()).not.toContain(text)
    expect(button('Technical details').attributes('aria-expanded')).toBe('false')
    await button('Technical details').trigger('click')
    await flushPromises()
    for (const text of [
      'image-secret',
      '/images/private.qcow2',
      'revision-secret',
      'private-alias',
    ])
      expect(w.text()).toContain(text)
    await w.get('[aria-label="Close resource details"]').trigger('click')
    expect(w.emitted('close')).toHaveLength(1)
  })
  it('emits distinct generation and assignment deletion targets with human names', async () => {
    setup()
    await button('Delete this generation').trigger('click')
    await button('Delete runner assignment').trigger('click')
    expect(w.emitted('delete')).toEqual([
      [{ target_type: 'image', target_id: 'image-secret' }, 'Base v2'],
      [{ target_type: 'assignment', target_id: 'assignment' }, 'Recipe'],
    ])
    await button('Build new generation').trigger('click')
    expect(w.emitted('rebuild')).toEqual([['definition']])
  })
  it.each(['deleted', 'retired', 'deactivated'])(
    'does not promote a %s assignment or allow assignment-wide actions',
    async (assignment_status) => {
      generation = image({ assignment_status })
      setup()
      expect(w.text()).not.toContain('Current default')
      expect(button('Build new generation')).toBeUndefined()
      expect(button('Delete runner assignment')).toBeUndefined()
      await button('Delete this generation').trigger('click')
      expect(w.emitted('delete')).toEqual([
        [{ target_type: 'image', target_id: 'image-secret' }, 'Base v2'],
      ])
    },
  )
  it('makes deleted generations read-only despite stale current/pending flags', () => {
    generation = image({ status: 'deleted', is_pending: true })
    setup()
    expect(w.text()).toContain('Deleted')
    expect(w.text()).not.toContain('Current default')
    expect(w.text()).not.toContain('Pending attempt')
    expect(button('Build new generation')).toBeUndefined()
    expect(w.find('[aria-label="Image actions"]').exists()).toBe(false)
    expect(w.findAll('[role="menuitem"]')).toHaveLength(0)
  })
  it.each(['pending_deletion', 'deleting'])('does not rebuild a generation in %s', (status) => {
    generation = image({ status })
    setup()
    expect(button('Build new generation')).toBeUndefined()
  })
  it.each(['pending', 'building', 'creating'])(
    'blocks rebuild for a same-assignment %s attempt',
    async (status) => {
      setup({}, [generation, image({ id: 'pending', is_current: false, is_pending: true, status })])
      expect(button('Build new generation').attributes('disabled')).toBeDefined()
      await button('Build new generation').trigger('click')
      expect(w.emitted('rebuild')).toBeUndefined()
    },
  )
  it('ignores pending attempts on another assignment but gates globally busy actions', async () => {
    setup({}, [
      generation,
      image({
        id: 'other',
        build_job_id: 'other-assignment',
        is_pending: true,
        status: 'building',
      }),
    ])
    expect(button('Build new generation').attributes('disabled')).toBeUndefined()
    await w.setProps({ busy: true })
    expect(button('Build new generation').attributes('disabled')).toBeDefined()
    expect(w.get('[aria-label="Image actions"]').attributes('disabled')).toBeDefined()
    await button('Build new generation').trigger('click')
    expect(w.emitted('rebuild')).toBeUndefined()
  })
  it('never invents recipe or revision information for independent captures', async () => {
    generation = image({
      origin_type: 'workspace_capture',
      is_current: false,
      generation: null,
      revision_id: null,
      definition_id: null,
      definition_name: null,
      build_job_id: null,
    })
    setup()
    await button('Technical details').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('Capture')
    for (const text of [
      'History',
      'Revision',
      'Definition',
      'Unknown legacy recipe',
      'Build new generation',
      'Delete runner assignment',
    ])
      expect(w.text()).not.toContain(text)
  })
  it('preserves workspace navigation and selected owner/activity without upfront IDs', async () => {
    const closed = vi.fn()
    window.addEventListener('opencuria:close-settings', closed)
    try {
      setup({
        generation: undefined,
        workspace,
        resource: { ...resource, kind: 'workspace', workspace, image_id: null },
      })
      expect(w.text()).toContain('Alice')
      expect(w.text()).toContain('2026-10-01')
      expect(w.text()).toContain('Lifecycle: stopped')
      expect(w.text()).not.toContain('workspace-secret')
      expect(w.get('a').attributes('href')).toBe('/workspaces/workspace-secret')
      await w.get('a').trigger('click')
      expect(closed).toHaveBeenCalledTimes(1)
      expect(w.find('[aria-label="Image actions"]').exists()).toBe(false)
    } finally {
      window.removeEventListener('opencuria:close-settings', closed)
    }
  })
  it('warns about unconfirmed ownership and unresolved dependency evidence', () => {
    setup({ generation: undefined, resource: { ...resource, managed: false }, unresolved: true })
    expect(w.text()).toContain('ownership is not confirmed')
    expect(w.text()).toContain('not present in the confirmed inventory')
    expect(w.find('[aria-label="Image actions"]').exists()).toBe(false)
  })
})

it('shows aggregate sizes instead of domain metrics, with original members behind technical disclosure', async () => {
  const member = {
    ...resource,
    physical_id: '/workspace/disk.qcow2',
    kind: 'disk',
    image_id: null,
    workspace,
    state: 'present',
    allocated_bytes: 2048,
    logical_bytes: 4096,
    virtual_bytes: 8192,
    dependencies: ['base'],
    aliases: ['disk-alias'],
    provenance: 'workspace overlay',
  }
  setup({
    generation: undefined,
    label: workspace.name,
    column: 0,
    workspace,
    resource: {
      ...resource,
      kind: 'workspace',
      state: 'exited',
      allocated_bytes: 999999,
      virtual_bytes: 999999,
    },
    storage: {
      resources: [
        member,
        { ...member, physical_id: 'seed.iso', kind: 'file', allocated_bytes: null },
      ],
      allocatedBytes: 2048,
      logicalBytes: 4096,
      virtualBytes: 8192,
      unknownCount: 1,
    },
  })
  expect(w.text()).toContain('Observed: exited')
  expect(w.text()).toContain('Lifecycle: stopped')
  expect(w.text()).toContain('Used storage2.0 KiB')
  expect(w.text()).toContain('Partial known total · 1 resource(s) unknown')
  expect(w.text()).toContain('Disk capacity8.0 KiB')
  expect(w.text()).toContain('File size4.0 KiB')
  expect(w.text()).not.toContain('976.6 KiB')
  expect(w.text()).not.toContain('/workspace/disk.qcow2')
  await button('Technical details').trigger('click')
  await flushPromises()
  expect(w.text()).toContain('Storage resources (2)')
  expect(w.text()).not.toContain('/workspace/disk.qcow2')
  await button('Storage resources').trigger('click')
  await flushPromises()
  for (const text of [
    '/workspace/disk.qcow2',
    'seed.iso',
    'disk-alias',
    'Physical dependencies: base',
    'workspace overlay',
    'Allocated',
    'Logical',
    'Virtual',
    'Shared',
    'Reclaimable',
    'Observed: present',
  ])
    expect(w.text()).toContain(text)
  expect(w.find('[aria-label="Image actions"]').exists()).toBe(false)
})

it('does not infer observed workspace state from aggregate disk members or expose domain sizes', () => {
  setup({
    generation: undefined,
    workspace,
    resource: undefined,
    storage: {
      resources: [{ ...resource, kind: 'disk', state: 'running' }],
      allocatedBytes: null,
      logicalBytes: null,
      virtualBytes: null,
      unknownCount: 1,
    },
  })
  expect(w.text()).toContain('Observed: unknown')
  expect(w.text()).toContain('Used storageUnknown')
  expect(w.text()).toContain('Disk capacityUnknown')
  expect(w.text()).not.toContain('Observed: running')
})
