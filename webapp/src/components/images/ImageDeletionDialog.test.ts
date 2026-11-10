import { describe, it, vi, beforeEach, expect } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ImageDeletionDialog from './ImageDeletionDialog.vue'
const api = vi.hoisted(() => ({
  listDeletions: vi.fn(),
  previewDeletion: vi.fn(),
  requestDeletion: vi.fn(),
  cancelDeletion: vi.fn(),
}))
vi.mock('@/services/runnerStorage.api', () => api)
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ isAdmin: true }) }))
const graph = {
  fingerprint: 'exact-graph',
  blockers: [],
  counts: { images: 1, workspaces: 1 },
  images: [{ id: 'i', name: 'Base', owner_id: '1' }],
  // Other users' workspace names never reach the client; only their owner does.
  workspaces: [{ id: 'w', name: '', owner_id: '2', owner_label: 'Bob' }],
}
const stubs = Object.fromEntries(
  [
    'Dialog',
    'DialogContent',
    'DialogBody',
    'DialogHeader',
    'DialogTitle',
    'DialogDescription',
    'DialogFooter',
  ].map((n) => [n, { template: '<div><slot /></div>' }]),
)
const mountDialog = () =>
  mount(ImageDeletionDialog, {
    props: { target: { target_type: 'image', target_id: 'i' } },
    global: {
      stubs: {
        ...stubs,
        Checkbox: {
          props: ['modelValue', 'disabled'],
          emits: ['update:modelValue'],
          template:
            '<button :disabled="disabled" @click="$emit(\'update:modelValue\', !modelValue)">Approve</button>',
        },
      },
    },
  })
describe('durable deletion confirmation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listDeletions.mockResolvedValue([])
    api.previewDeletion.mockResolvedValue(graph)
    api.requestDeletion.mockResolvedValue({
      id: 'd',
      phase: 'waiting_dependency',
      can_cancel: true,
      diagnostic: 'Waiting on pins',
    })
  })
  it('defaults deferred, displays waiting diagnostic, cancels only server-authorized request', async () => {
    const w = mountDialog()
    await flushPromises()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Delete')!
      .trigger('click')
    await flushPromises()
    expect(api.requestDeletion).toHaveBeenCalledWith(
      { target_type: 'image', target_id: 'i' },
      'deferred',
      '',
    )
    expect(w.text()).toContain('Waiting until unused')
    expect(w.text()).toContain('Waiting on pins')
    // A pending request cannot be requested again; the dialog only closes.
    const labels = () => w.findAll('button').map((b) => b.text())
    expect(labels()).not.toContain('Delete')
    expect(labels()).toContain('Close')
    api.cancelDeletion.mockResolvedValue({ id: 'd', phase: 'cancelled', can_cancel: false })
    await w
      .findAll('button')
      .find((b) => b.text() === 'Cancel deletion')!
      .trigger('click')
    await flushPromises()
    expect(api.cancelDeletion).toHaveBeenCalledWith('d')
    expect(w.text()).not.toContain('Cancel deletion')
    expect(labels()).toContain('Delete')
    w.unmount()
  })
  it('names what is deleted and removes runner images instead of deleting them', async () => {
    const w = mount(ImageDeletionDialog, {
      props: { target: { target_type: 'assignment', target_id: 'b' }, name: 'Python on edge-1' },
      global: { stubs },
    })
    await flushPromises()
    expect(w.text()).toContain('Remove Python on edge-1?')
    expect(w.text()).toContain(
      'It is removed from the runner once no workspace uses it. Workspaces are kept.',
    )
    expect(w.findAll('button').map((b) => b.text())).toEqual(['Force delete…', 'Cancel', 'Remove'])
    w.unmount()
  })
  it('requires explicit fingerprint approval and a second approval for a changed graph', async () => {
    const w = mountDialog()
    await flushPromises()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Force delete…')!
      .trigger('click')
    expect(w.text()).toContain('Workspace · Bob')
    const confirm = () => w.findAll('button').find((b) => b.text() === 'Delete permanently')!
    expect(confirm().attributes('disabled')).toBeDefined()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Approve')!
      .trigger('click')
    api.requestDeletion.mockRejectedValue(new Error('Graph changed'))
    api.previewDeletion.mockResolvedValue({ ...graph, fingerprint: 'new-graph' })
    await confirm().trigger('click')
    await flushPromises()
    expect(confirm().attributes('disabled')).toBeDefined()
    expect(api.requestDeletion).toHaveBeenCalledWith(
      { target_type: 'image', target_id: 'i' },
      'force',
      'exact-graph',
    )
    await w
      .findAll('button')
      .find((b) => b.text() === 'Approve')!
      .trigger('click')
    api.requestDeletion.mockResolvedValue({ id: 'd', phase: 'executing', can_cancel: false })
    await confirm().trigger('click')
    await flushPromises()
    expect(api.requestDeletion).toHaveBeenLastCalledWith(
      { target_type: 'image', target_id: 'i' },
      'force',
      'new-graph',
    )
    w.unmount()
  })
  it('blocks force on stale/incomplete preview', async () => {
    api.previewDeletion.mockResolvedValue({ ...graph, blockers: ['Runner offline / stale scan'] })
    const w = mountDialog()
    await flushPromises()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Force delete…')!
      .trigger('click')
    expect(w.text()).toContain('Runner offline / stale scan')
    expect(
      w
        .findAll('button')
        .find((b) => b.text() === 'Delete permanently')!
        .attributes('disabled'),
    ).toBeDefined()
    w.unmount()
  })
})
