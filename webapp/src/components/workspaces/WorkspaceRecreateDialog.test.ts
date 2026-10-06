import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import WorkspaceRecreateDialog from './WorkspaceRecreateDialog.vue'
import type { ImageVersionRef, Workspace } from '@/types'

const store = vi.hoisted(() => ({ recreateWorkspace: vi.fn(async () => true) }))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => store }))

const stubs = {
  ...Object.fromEntries(
    [
      'Dialog',
      'DialogContent',
      'DialogBody',
      'DialogHeader',
      'DialogTitle',
      'DialogDescription',
      'DialogFooter',
      'SelectContent',
      'SelectTrigger',
      'SelectValue',
    ].map((n) => [n, { template: '<div><slot /></div>' }]),
  ),
  Select: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div data-testid="target" :data-value="modelValue"><slot /></div>',
  },
  SelectItem: {
    props: ['value'],
    template:
      '<button type="button" data-testid="option" @click="$parent.$emit(\'update:modelValue\', value)"><slot /></button>',
  },
  Checkbox: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<button type="button" data-testid="recreate-confirm" @click="$emit(\'update:modelValue\', !modelValue)" />',
  },
}

function ref(patch: Partial<ImageVersionRef> = {}): ImageVersionRef {
  return {
    id: 'v1',
    line_kind: 'captured',
    line_id: 'line',
    name: 'Node dev',
    version: 1,
    message: '',
    status: 'ready',
    latest_id: 'v1',
    latest_version: 1,
    update_available: false,
    ...patch,
  }
}

function workspace(patch: Partial<Workspace> = {}): Workspace {
  return {
    id: 'ws',
    name: 'feature-x',
    status: 'running',
    base_image: ref(),
    pending_base_image: null,
    repos: [],
    ...patch,
  } as Workspace
}

function render(ws: Workspace, preferLatest = false) {
  return mount(WorkspaceRecreateDialog, {
    props: { workspace: ws, open: true, preferLatest },
    global: { stubs },
  })
}

describe('WorkspaceRecreateDialog', () => {
  beforeEach(() => store.recreateWorkspace.mockClear())

  it('requires explicit confirmation of the data loss before resetting', async () => {
    const w = render(workspace({ repos: ['https://github.com/acme/app'] }))
    expect(w.get('[data-testid="recreate-data-loss-warning"]').text()).toContain(
      'permanently deleted',
    )
    expect(w.text()).toContain('Chats, settings, credentials and schedules are kept')
    expect(w.text()).toContain('https://github.com/acme/app')
    const submit = w.get('[data-testid="recreate-submit"]')
    expect(submit.text()).toBe('Reset workspace')
    expect(submit.attributes('disabled')).toBeDefined()
    await w.get('form').trigger('submit')
    expect(store.recreateWorkspace).not.toHaveBeenCalled()
    await w.get('[data-testid="recreate-confirm"]').trigger('click')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(store.recreateWorkspace).toHaveBeenCalledWith('ws', 'v1')
    expect(w.emitted('update:open')?.[0]).toEqual([false])
  })

  it('offers updating to the latest version and preselects it when asked', async () => {
    const w = render(
      workspace({ base_image: ref({ update_available: true, latest_id: 'v3', latest_version: 3 }) }),
      true,
    )
    expect(w.findAll('[data-testid="option"]').map((o) => o.text())).toEqual([
      'Reset to v1 (current)',
      'Update to v3 (latest)',
    ])
    expect(w.get('[data-testid="target"]').attributes('data-value')).toBe('v3')
    expect(w.get('[data-testid="recreate-submit"]').text()).toBe('Update workspace')
    await w.get('[data-testid="recreate-confirm"]').trigger('click')
    await w.get('form').trigger('submit')
    expect(store.recreateWorkspace).toHaveBeenCalledWith('ws', 'v3')
  })

  it('retries a failed reset on its pending target version', async () => {
    const w = render(
      workspace({ status: 'failed' as never, pending_base_image: ref({ id: 'v2', version: 2 }) }),
    )
    expect(w.text()).toContain('Reset to v2 (current)')
    await w.get('[data-testid="recreate-confirm"]').trigger('click')
    await w.get('form').trigger('submit')
    expect(store.recreateWorkspace).toHaveBeenCalledWith('ws', 'v2')
  })

  it('explains why a workspace on a deleted version cannot be reset', () => {
    const w = render(workspace({ base_image: ref({ status: 'pending_deletion' }) }))
    expect(w.get('[role="alert"]').text()).toContain('being deleted')
    expect(w.find('[data-testid="recreate-submit"]').exists()).toBe(false)
  })

  it('explains that untracked workspaces cannot be reset', () => {
    const w = render(workspace({ base_image: null }))
    expect(w.get('[role="alert"]').text()).toContain('predates image tracking')
  })
})
