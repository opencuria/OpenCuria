import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import { useNotificationStore } from '@/stores/notifications'
import PublicKeyDialog from './PublicKeyDialog.vue'
import type { Credential } from '@/types'

const credentialStore = vi.hoisted(() => ({ getPublicKey: vi.fn() }))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => credentialStore }))

const credential: Credential = {
  id: 'cred-1',
  name: 'SSH key',
  scope: 'personal',
  service_id: 'svc-1',
  service_name: 'GitHub',
  service_slug: 'github',
  credential_type: 'ssh_key',
  env_var_name: '',
  target_path: '',
  has_public_key: true,
  created_by_id: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  oauth_connected: false,
  oauth_status: '',
  oauth_reconnect_required: false,
  oauth_expires_at: null,
}

const dialogStubs = {
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

describe('PublicKeyDialog copy controls', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    credentialStore.getPublicKey.mockResolvedValue('ssh-ed25519 AAAA public-key')
  })

  it('provides independent copy feedback for both public-key controls', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const notificationInfo = vi.spyOn(useNotificationStore(), 'info')
    const wrapper = mount(PublicKeyDialog, {
      props: { credential: null },
      global: { stubs: dialogStubs },
    })
    await wrapper.setProps({ credential })
    await flushPromises()

    const copies = wrapper.findAllComponents({ name: 'CopyButton' })
    expect(copies).toHaveLength(2)
    expect(copies.map((copy) => copy.props('label'))).toEqual(['public key', 'public key'])
    await copies[0]!.trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith('ssh-ed25519 AAAA public-key')
    expect(copies[0]!.attributes('aria-label')).toBe('Copied')
    expect(copies[1]!.attributes('aria-label')).toBe('Copy public key')
    expect(notificationInfo).not.toHaveBeenCalled()
  })
})
