import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ApiKeyCard from '@/components/api-keys/ApiKeyCard.vue'
import ImageDefinitionModal from '@/components/images/ImageDefinitionModal.vue'
import SkillsPanel from './SkillsPanel.vue'
import { updateImageDefinition } from '@/services/workspaces.api'
import type { APIKey, ImageDefinition, Skill } from '@/types'

const stores = vi.hoisted(() => ({
  skills: {
    skills: [] as Skill[],
    loading: false,
    error: null,
    fetchSkills: vi.fn(),
    updateSkill: vi.fn(),
    createSkill: vi.fn(),
    deleteSkill: vi.fn(),
  },
  keys: {
    availablePermissions: ['workspaces:read', 'workspaces:create'].map((value) => ({
      value,
      label: value,
      description: '',
      group: 'Workspaces',
    })),
    fetchAvailablePermissions: vi.fn(),
    updateKeyPermissions: vi.fn(),
  },
}))
vi.mock('@/stores/skills', () => ({ useSkillStore: () => stores.skills }))
vi.mock('@/stores/apiKeys', () => ({ useApiKeyStore: () => stores.keys }))
vi.mock('@/services/workspaces.api', () => ({ updateImageDefinition: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { error: vi.fn() } }))

const dialogStubs = Object.fromEntries(
  [
    'DialogContent',
    'DialogHeader',
    'DialogTitle',
    'DialogDescription',
    'DialogBody',
    'DialogFooter',
  ].map((name) => [name, { template: '<div><slot /></div>' }]),
)
const global = {
  stubs: {
    ...dialogStubs,
    Dialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' },
  },
}
const skill: Skill = {
  id: 'skill-1',
  name: 'Basics',
  body: 'Use it well.',
  scope: 'personal',
  created_by_email: null,
  created_at: '',
  updated_at: '',
}
const apiKey: APIKey = {
  id: 'key-1',
  name: 'Integration',
  key_prefix: 'kai_test',
  is_active: true,
  permissions: ['workspaces:read', 'workspaces:create'],
  created_at: '2026-01-01',
  last_used_at: null,
  expires_at: null,
}
const definition: ImageDefinition = {
  id: 'image-1',
  organization_id: 'org-1',
  name: 'Dev',
  description: '',
  is_standard: false,
  runtime_type: 'docker',
  base_distro: 'ubuntu:22.04',
  packages: [],
  env_vars: {},
  custom_dockerfile: '',
  custom_init_script: '',
  is_active: true,
  status: 'active',
  created_at: '',
  updated_at: '',
}

enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())
beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  vi.clearAllMocks()
  stores.skills.skills = [{ ...skill }]
  stores.skills.updateSkill.mockResolvedValue(true)
  stores.keys.updateKeyPermissions.mockResolvedValue(true)
  vi.mocked(updateImageDefinition).mockResolvedValue(definition)
})
async function autosave() {
  await vi.advanceTimersByTimeAsync(500)
  await flushPromises()
}

describe('skill autosave', () => {
  it('keeps the editor open and saves edits after a pause', async () => {
    const wrapper = mount(SkillsPanel, { global })
    await wrapper.get('[title="Edit skill"]').trigger('click')
    await autosave()
    expect(stores.skills.updateSkill).not.toHaveBeenCalled()
    await wrapper.get('#edit-skill-name').setValue('Updated')
    await autosave()
    expect(stores.skills.updateSkill).toHaveBeenCalledExactlyOnceWith(
      'skill-1',
      { name: 'Updated', body: skill.body },
      { silent: true },
    )
    expect(wrapper.find('#edit-skill-name').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Save Changes')
  })

  it('keeps invalid or failed changes visible and allows a retry', async () => {
    const wrapper = mount(SkillsPanel, { global })
    await wrapper.get('[title="Edit skill"]').trigger('click')
    await wrapper.get('#edit-skill-body').setValue('')
    await autosave()
    expect(stores.skills.updateSkill).not.toHaveBeenCalled()
    stores.skills.updateSkill.mockResolvedValueOnce(false)
    await wrapper.get('#edit-skill-body').setValue('Updated body')
    await autosave()
    expect(wrapper.text()).toContain('Changes not saved')
    await wrapper.get('[role="status"] button').trigger('click')
    await flushPromises()
    expect(stores.skills.updateSkill).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('All changes saved')
  })

  it('does not expose organization skill editing to members', () => {
    stores.skills.skills = [{ ...skill, scope: 'organization' }]
    const wrapper = mount(SkillsPanel, { global })
    expect(wrapper.find('[title="Edit skill"]').exists()).toBe(false)
  })
})

describe('API key permission autosave', () => {
  it('saves restricted permissions without closing and requires explicit full access', async () => {
    const wrapper = mount(ApiKeyCard, { props: { apiKey } })
    await wrapper.get('button:not([title])').trigger('click')
    await wrapper.findAll('[role="checkbox"]')[0]!.trigger('click')
    await autosave()
    expect(stores.keys.updateKeyPermissions).toHaveBeenCalledExactlyOnceWith(
      'key-1',
      ['workspaces:create'],
      { silent: true },
    )
    expect(wrapper.findAll('[role="checkbox"]')[1]!.attributes('disabled')).toBeDefined()
    await wrapper.get('[role="switch"]').trigger('click')
    await autosave()
    expect(stores.keys.updateKeyPermissions).toHaveBeenLastCalledWith('key-1', [], { silent: true })
    expect(wrapper.text()).not.toMatch(/Saving|Save Changes/)
  })

  it('retains failed edits for retry and never edits revoked keys', async () => {
    stores.keys.updateKeyPermissions.mockResolvedValueOnce(false)
    const wrapper = mount(ApiKeyCard, { props: { apiKey } })
    await wrapper.get('button:not([title])').trigger('click')
    await wrapper.findAll('[role="checkbox"]')[0]!.trigger('click')
    await autosave()
    expect(wrapper.text()).toContain('Changes not saved')
    await wrapper.get('[role="status"] button').trigger('click')
    await flushPromises()
    expect(stores.keys.updateKeyPermissions).toHaveBeenCalledTimes(2)
    const revoked = mount(ApiKeyCard, { props: { apiKey: { ...apiKey, is_active: false } } })
    expect(revoked.find('[role="switch"]').exists()).toBe(false)
  })
})

describe('image definition autosave', () => {
  it('updates an existing definition without closing or writing initial values', async () => {
    const wrapper = mount(ImageDefinitionModal, {
      props: { open: true, imageDefinition: definition },
      global,
    })
    await autosave()
    expect(updateImageDefinition).not.toHaveBeenCalled()
    await wrapper.findAll('input')[0]!.setValue('Updated image')
    await autosave()
    expect(updateImageDefinition).toHaveBeenCalledWith(
      'image-1',
      expect.objectContaining({ name: 'Updated image' }),
    )
    expect(wrapper.emitted('updated')).toHaveLength(1)
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(wrapper.findAll('button').some((button) => /^Save/.test(button.text()))).toBe(false)
  })

  it('blocks invalid environment lines and retries failed updates', async () => {
    const wrapper = mount(ImageDefinitionModal, {
      props: { open: true, imageDefinition: definition },
      global,
    })
    const environment = wrapper.findAll('textarea')[1]!
    await environment.setValue('INCOMPLETE')
    await autosave()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Close')!
      .trigger('click')
    expect(updateImageDefinition).not.toHaveBeenCalled()
    expect(wrapper.emitted('update:open')).toBeUndefined()
    vi.mocked(updateImageDefinition).mockRejectedValueOnce(new Error('offline'))
    await environment.setValue('NODE_ENV=development')
    await autosave()
    expect(wrapper.text()).toContain('offline')
    await wrapper.get('[role="status"] button').trigger('click')
    await flushPromises()
    expect(updateImageDefinition).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('All changes saved')
  })
})
