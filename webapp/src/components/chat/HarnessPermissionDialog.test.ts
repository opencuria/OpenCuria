import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessPermissionDialog from './HarnessPermissionDialog.vue'
import { useHarnessStore } from '@/stores/harness'
import * as harnessApi from '@/services/harness.api'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    resolveHarnessPermission: vi.fn(),
  }
})

const resolveMock = vi.mocked(harnessApi.resolveHarnessPermission)

function makeRequest() {
  return {
    request_id: 'req-1',
    session_id: 'session-1',
    workspace_id: 'workspace-1',
    tool: 'bash',
    pattern: 'rm -rf /tmp/x',
    title: '$ rm -rf /tmp/x',
    call_id: 'call-9',
  }
}

const dialogStubs = {
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

describe('HarnessPermissionDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('shows tool, title and pattern preview for the pending request', () => {
    const wrapper = mount(HarnessPermissionDialog, {
      props: { request: makeRequest() },
      global: { stubs: dialogStubs },
    })

    expect(wrapper.text()).toContain('bash')
    expect(wrapper.text()).toContain('$ rm -rf /tmp/x')
    expect(wrapper.text()).toContain('rm -rf /tmp/x')
  })

  it('approve once resolves via the M6 permission endpoint', async () => {
    const store = useHarnessStore()
    store.handlePermissionRequired(makeRequest())
    resolveMock.mockResolvedValue({
      request_id: 'req-1',
      decision: 'allow',
      remember: 'once',
    })

    const wrapper = mount(HarnessPermissionDialog, {
      props: { request: store.pendingPermissions['req-1']! },
      global: { stubs: dialogStubs },
    })
    const buttons = wrapper.findAll('button')
    const approveOnce = buttons.find((b) => b.text().includes('Approve once'))
    expect(approveOnce).toBeTruthy()

    // Resolve through the store (the flow the panel wires to the dialog).
    await store.resolvePermission('session-1', 'req-1', 'once')

    expect(resolveMock).toHaveBeenCalledWith(
      'session-1',
      expect.objectContaining({ request_id: 'req-1' }),
      'once',
    )
    expect(store.pendingPermissions['req-1']).toBeUndefined()
  })

  it('reject resolves with reject and clears the pending request', async () => {
    const store = useHarnessStore()
    store.handlePermissionRequired(makeRequest())
    resolveMock.mockResolvedValue({
      request_id: 'req-1',
      decision: 'reject',
      remember: 'once',
    })

    await store.resolvePermission('session-1', 'req-1', 'reject')

    expect(resolveMock).toHaveBeenCalledWith(
      'session-1',
      expect.objectContaining({ request_id: 'req-1' }),
      'reject',
    )
    expect(store.pendingPermissions['req-1']).toBeUndefined()
  })
})
