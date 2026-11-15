import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { ApiRequestError } from '@opencuria/client/api'
import type { WindowsLicensing, WindowsProductKey } from '@opencuria/client'
import WindowsLicensingTab from './WindowsLicensingTab.vue'
import * as licensingApi from '@/services/licensing.api'
import { useAuthStore } from '@/stores/auth'

vi.mock('@/services/licensing.api', () => ({
  getWindowsLicensing: vi.fn(),
  updateWindowsLicensing: vi.fn(),
  addWindowsProductKey: vi.fn(),
  updateWindowsProductKey: vi.fn(),
  deleteWindowsProductKey: vi.fn(),
  setWindowsUserLicensed: vi.fn(),
}))
vi.mock('vue-sonner', () => ({ toast: { error: vi.fn() } }))
enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())

const api = vi.mocked(licensingApi)

const usedKey: WindowsProductKey = {
  id: 'key-1',
  label: 'Office volume',
  key_suffix: 'Q7J3K',
  seats: 5,
  used_seats: 2,
  created_at: '2026-11-01T00:00:00Z',
  assignments: [
    { owner_label: 'Ada L.', activation_status: 'activated' },
    { owner_label: 'Bob M.', activation_status: 'failed' },
  ],
}
const freeKey: WindowsProductKey = {
  id: 'key-2',
  label: '',
  key_suffix: 'ZZ9P2',
  seats: 1,
  used_seats: 0,
  created_at: '2026-11-02T00:00:00Z',
  assignments: [],
}

function state(patch: Partial<WindowsLicensing> = {}): WindowsLicensing {
  return {
    mode: 'per_device',
    kms_host: '',
    user_license_seats: 2,
    max_running: null,
    product_keys: [usedKey, freeKey],
    members: [
      { user_id: '1', label: 'Ada L.', licensed: true },
      { user_id: '2', label: 'Bob M.', licensed: false },
    ],
    usage: { windows_workspaces: 3, running: 1, device_key: 2, user: 0, unlicensed: 1 },
    ...patch,
  }
}

const stubs = {
  Dialog: {
    props: ['open'],
    template: '<div v-if="open"><slot /></div>',
  },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
  Select: {
    name: 'Select',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div><slot /></div>',
  },
  SelectTrigger: { template: '<button type="button"><slot /></button>' },
  SelectValue: { template: '<span />' },
  SelectContent: { template: '<div><slot /></div>' },
  SelectItem: { props: ['value'], template: '<div role="option"><slot /></div>' },
  TooltipProvider: { template: '<div><slot /></div>' },
  Tooltip: { props: ['disabled'], template: '<div><slot /></div>' },
  TooltipTrigger: { template: '<div><slot /></div>' },
  TooltipContent: { template: '<div data-testid="tooltip"><slot /></div>' },
}

function mountTab() {
  return mount(WindowsLicensingTab, { global: { stubs } })
}

async function autosave() {
  await vi.advanceTimersByTimeAsync(500)
  await flushPromises()
}

async function selectMode(wrapper: ReturnType<typeof mountTab>, mode: string) {
  wrapper.getComponent({ name: 'Select' }).vm.$emit('update:modelValue', mode)
  await flushPromises()
}

describe('WindowsLicensingTab', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useAuthStore().activeOrganizationId = 'org-1'
    vi.useFakeTimers()
    vi.clearAllMocks()
    api.getWindowsLicensing.mockResolvedValue(state())
    api.updateWindowsLicensing.mockImplementation(async (_org, input) => ({
      ...state(),
      ...input,
    }))
  })

  it('lists product keys by suffix and seats without a write on load', async () => {
    const wrapper = mountTab()
    await flushPromises()
    await autosave()
    expect(api.getWindowsLicensing).toHaveBeenCalledWith('org-1')
    expect(api.updateWindowsLicensing).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="windows-mode-description"]').text()).toContain(
      'one key seat for its lifetime',
    )
    const row = wrapper.get('[data-testid="windows-key-key-1"]')
    expect(row.text()).toContain('Office volume')
    expect(row.text()).toContain('…Q7J3K')
    expect(row.text()).toContain('2/5')
    expect(wrapper.get('[data-testid="windows-key-key-2"]').text()).toContain('Product key')
    expect(wrapper.find('[data-testid="windows-key-add"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="windows-members"]').exists()).toBe(false)
    expect(wrapper.find('#windows-kms-host').exists()).toBe(false)
    expect(wrapper.get('[data-testid="windows-usage"]').text()).toBe(
      '3 Windows workspaces · 1 running · 2 on product keys · 1 without license',
    )
  })

  it('expands the owners and activation state of assigned seats', async () => {
    const wrapper = mountTab()
    await flushPromises()
    const row = wrapper.get('[data-testid="windows-key-key-1"]')
    expect(row.text()).not.toContain('Bob M.')
    await wrapper.get('[data-testid="windows-key-assignments-key-1"]').trigger('click')
    await flushPromises()
    expect(row.text()).toContain('Ada L.')
    expect(row.text()).toContain('Activated')
    expect(row.text()).toContain('Bob M.')
    expect(row.text()).toContain('Activation failed')
    expect(wrapper.find('[data-testid="windows-key-assignments-key-2"]').exists()).toBe(false)
  })

  it('autosaves only the changed license model', async () => {
    const wrapper = mountTab()
    await flushPromises()
    await selectMode(wrapper, 'unlicensed')
    expect(wrapper.get('[data-testid="windows-mode-description"]').text()).toBe(
      "Windows runs without activation. Microsoft's terms still require a license.",
    )
    await autosave()
    expect(api.updateWindowsLicensing).toHaveBeenCalledTimes(1)
    expect(api.updateWindowsLicensing).toHaveBeenCalledWith('org-1', { mode: 'unlicensed' })
    expect(wrapper.get('[data-testid="windows-licensing-status"]').text()).toBe('All changes saved')
    // Assigned seats stay visible, but new keys only make sense with product keys.
    expect(wrapper.find('[data-testid="windows-keys"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="windows-key-add"]').exists()).toBe(false)
  })

  it('hides the key pool in other models while no key exists', async () => {
    api.getWindowsLicensing.mockResolvedValue(state({ mode: 'unlicensed', product_keys: [] }))
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.find('[data-testid="windows-keys"]').exists()).toBe(false)
    await selectMode(wrapper, 'per_device')
    expect(wrapper.get('[data-testid="windows-keys"]').text()).toContain('No product keys')
  })

  it('edits user licenses, the KMS host and member licenses in per-user mode', async () => {
    api.getWindowsLicensing.mockResolvedValue(state({ mode: 'per_user' }))
    api.updateWindowsLicensing.mockImplementation(async (_org, input) => ({
      ...state({ mode: 'per_user' }),
      ...input,
    }))
    api.setWindowsUserLicensed.mockResolvedValue(
      state({
        mode: 'per_user',
        members: [
          { user_id: '1', label: 'Ada L.', licensed: true },
          { user_id: '2', label: 'Bob M.', licensed: true },
        ],
      }),
    )
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.get('#windows-kms-host').attributes('placeholder')).toBe('Automatic (DNS)')
    expect(wrapper.text()).toContain('1 assigned to members below.')

    await wrapper.get('#windows-user-seats').setValue('4')
    await wrapper.get('#windows-kms-host').setValue(' kms.example.com:1688 ')
    await autosave()
    expect(api.updateWindowsLicensing).toHaveBeenCalledWith('org-1', {
      kms_host: 'kms.example.com:1688',
      user_license_seats: 4,
    })
    expect((wrapper.get('#windows-kms-host').element as HTMLInputElement).value).toBe(
      'kms.example.com:1688',
    )

    const bob = wrapper.get('[data-testid="windows-member-2"] [role="switch"]')
    expect(bob.attributes('aria-checked')).toBe('false')
    await bob.trigger('click')
    await flushPromises()
    expect(api.setWindowsUserLicensed).toHaveBeenCalledWith('org-1', '2', true)
    expect(
      wrapper.get('[data-testid="windows-member-2"] [role="switch"]').attributes('aria-checked'),
    ).toBe('true')
    expect(wrapper.text()).toContain('2 assigned to members below.')
  })

  it('shows the server detail when no user license is left', async () => {
    api.getWindowsLicensing.mockResolvedValue(state({ mode: 'per_user' }))
    api.setWindowsUserLicensed.mockRejectedValue(
      new ApiRequestError(
        409,
        'All 2 Windows user licenses are assigned.',
        'windows_user_seats_exhausted',
      ),
    )
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="windows-member-2"] [role="switch"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="windows-action-error"]').text()).toContain(
      'All 2 Windows user licenses are assigned.',
    )
    expect(
      wrapper.get('[data-testid="windows-member-2"] [role="switch"]').attributes('aria-checked'),
    ).toBe('false')
  })

  it('keeps invalid input local and reports a rejected save with its detail', async () => {
    api.getWindowsLicensing.mockResolvedValue(state({ mode: 'per_user' }))
    api.updateWindowsLicensing.mockRejectedValueOnce(
      new ApiRequestError(409, '3 members still hold user licenses.', 'windows_user_seats_in_use'),
    )
    const wrapper = mountTab()
    await flushPromises()
    for (const value of ['', '-1', '1.5']) {
      await wrapper.get('#windows-user-seats').setValue(value)
      await autosave()
      expect(wrapper.find('[role="alert"]').text()).toContain('whole number of user licenses')
    }
    await wrapper.get('#windows-user-seats').setValue('2')
    await wrapper.get('#windows-kms-host').setValue('kms host')
    await autosave()
    expect(wrapper.find('[role="alert"]').text()).toContain('host name or IP address')
    expect(api.updateWindowsLicensing).not.toHaveBeenCalled()

    await wrapper.get('#windows-kms-host').setValue('')
    await wrapper.get('#windows-user-seats').setValue('1')
    await autosave()
    expect(api.updateWindowsLicensing).toHaveBeenCalledWith('org-1', { user_license_seats: 1 })
    expect(wrapper.get('[data-testid="windows-licensing-status"]').text()).toContain(
      '3 members still hold user licenses.',
    )
  })

  it('caps running Windows workspaces and clears the cap with null', async () => {
    api.updateWindowsLicensing.mockImplementation(async (_org, input) =>
      state({ max_running: input.max_running ?? null }),
    )
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('#windows-running-cap').trigger('click')
    await autosave()
    expect(api.updateWindowsLicensing).toHaveBeenLastCalledWith('org-1', { max_running: 1 })
    await wrapper.get('#windows-max-running').setValue('4')
    await autosave()
    expect(api.updateWindowsLicensing).toHaveBeenLastCalledWith('org-1', { max_running: 4 })
    expect(wrapper.text()).toContain('At most 4 run at the same time.')
    await wrapper.get('#windows-running-cap').trigger('click')
    await autosave()
    expect(api.updateWindowsLicensing).toHaveBeenLastCalledWith('org-1', { max_running: null })
    expect(wrapper.text()).toContain('No limit.')
  })

  it('adds a key once and never shows it again', async () => {
    const added: WindowsProductKey = {
      ...freeKey,
      id: 'key-3',
      label: 'Lab',
      key_suffix: 'T8DKJ',
      seats: 3,
    }
    api.addWindowsProductKey.mockResolvedValue(added)
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="windows-key-add"]').trigger('click')
    const dialog = wrapper.get('[data-testid="windows-key-dialog"]')
    expect(dialog.get('#windows-key-value').attributes('type')).toBe('password')
    await dialog.get('#windows-key-value').setValue('not a key')
    expect(dialog.text()).toContain('Enter the 25-character key')
    expect(dialog.get('[data-testid="windows-key-submit"]').attributes('disabled')).toBeDefined()

    await dialog.get('#windows-key-value').setValue(' nkjfk-gpf6w-ra3qj-xyt8d-t8dkj ')
    await dialog.get('#windows-key-label').setValue('Lab')
    await dialog.get('#windows-key-seats').setValue('3')
    await dialog.get('form').trigger('submit')
    await flushPromises()
    expect(api.addWindowsProductKey).toHaveBeenCalledWith('org-1', {
      product_key: 'NKJFK-GPF6W-RA3QJ-XYT8D-T8DKJ',
      label: 'Lab',
      seats: 3,
    })
    expect(wrapper.find('[data-testid="windows-key-dialog"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="windows-key-key-3"]').text()).toContain('…T8DKJ')
    expect(wrapper.html()).not.toContain('NKJFK')
  })

  it('shows why a key was rejected inside the dialog', async () => {
    api.addWindowsProductKey.mockRejectedValue(
      new ApiRequestError(409, 'This key is already in the pool.', 'windows_key_duplicate'),
    )
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="windows-key-add"]').trigger('click')
    const dialog = wrapper.get('[data-testid="windows-key-dialog"]')
    await dialog.get('#windows-key-value').setValue('NKJFKGPF6WRA3QJXYT8DT8DKJ')
    await dialog.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="windows-key-dialog"] [role="alert"]').text()).toBe(
      'This key is already in the pool.',
    )
    expect(wrapper.findAll('[data-testid^="windows-key-key-"]')).toHaveLength(2)
  })

  it('edits only the changed seats and reports seats still in use', async () => {
    api.updateWindowsProductKey
      .mockRejectedValueOnce(
        new ApiRequestError(409, 'Seats in use cannot be removed.', 'windows_key_seats_in_use'),
      )
      .mockResolvedValueOnce({ ...usedKey, seats: 8 })
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="windows-key-edit-key-1"]').trigger('click')
    const dialog = wrapper.get('[data-testid="windows-key-dialog"]')
    expect(dialog.find('#windows-key-value').exists()).toBe(false)
    expect((dialog.get('#windows-key-label').element as HTMLInputElement).value).toBe(
      'Office volume',
    )
    await dialog.get('#windows-key-seats').setValue('1')
    expect(dialog.text()).toContain('2 seats are in use.')
    await dialog.get('#windows-key-seats').setValue('3')
    await dialog.get('form').trigger('submit')
    await flushPromises()
    expect(api.updateWindowsProductKey).toHaveBeenCalledWith('org-1', 'key-1', { seats: 3 })
    expect(wrapper.get('[data-testid="windows-key-dialog"] [role="alert"]').text()).toBe(
      'Seats in use cannot be removed.',
    )
    await wrapper.get('[data-testid="windows-key-dialog"] #windows-key-seats').setValue('8')
    await wrapper.get('[data-testid="windows-key-dialog"] form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="windows-key-dialog"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="windows-key-key-1"]').text()).toContain('2/8')
  })

  it('deletes only unused keys after confirmation and shows a conflict', async () => {
    api.deleteWindowsProductKey
      .mockRejectedValueOnce(
        new ApiRequestError(409, '1 workspace now holds a seat of this key.', 'windows_key_in_use'),
      )
      .mockResolvedValueOnce(undefined)
    const wrapper = mountTab()
    await flushPromises()
    const usedDelete = wrapper.get('[data-testid="windows-key-delete-key-1"]')
    expect(usedDelete.attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="windows-key-key-1"]').text()).toContain(
      'In use by 2 workspaces',
    )

    await wrapper.get('[data-testid="windows-key-delete-key-2"]').trigger('click')
    const confirm = () => wrapper.get('[data-testid="windows-key-delete-confirm"]')
    expect(wrapper.get('[data-testid="windows-key-delete-dialog"]').text()).toContain('ZZ9P2')
    await confirm().trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="windows-key-delete-dialog"] [role="alert"]').text()).toBe(
      '1 workspace now holds a seat of this key.',
    )
    await confirm().trigger('click')
    await flushPromises()
    expect(api.deleteWindowsProductKey).toHaveBeenLastCalledWith('org-1', 'key-2')
    expect(wrapper.find('[data-testid="windows-key-delete-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="windows-key-key-2"]').exists()).toBe(false)
  })

  it('shows a load failure instead of the settings', async () => {
    api.getWindowsLicensing.mockRejectedValue(
      new ApiRequestError(403, 'Only organization admins can manage Windows licensing.'),
    )
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe(
      'Only organization admins can manage Windows licensing.',
    )
    expect(wrapper.find('[data-testid="windows-keys"]').exists()).toBe(false)
  })
})
