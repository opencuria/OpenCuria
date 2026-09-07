import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitChangesSection from './GitChangesSection.vue'
import { useGitStore } from '@/stores/git'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

function mountSection() {
  return mount(GitChangesSection, { attachTo: document.body })
}

describe('GitChangesSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('groups changes into staged and unstaged lists', () => {
    const wrapper = mountSection()

    expect(wrapper.get('[data-testid="git-staged-trigger"]').text()).toContain(
      '2',
    )
    expect(wrapper.get('[data-testid="git-changes-trigger"]').text()).toContain(
      '3',
    )
    expect(wrapper.findAll('[data-testid="git-staged-file"]')).toHaveLength(2)
    expect(wrapper.findAll('[data-testid="git-changed-file"]')).toHaveLength(3)
  })

  it('stages a file via its hover action', async () => {
    const store = useGitStore()
    const wrapper = mountSection()
    const before = store.stagedChanges.length

    await wrapper.find('[data-testid="git-stage-file"]').trigger('click')

    expect(store.stagedChanges.length).toBe(before + 1)
  })

  it('keeps commit disabled without a message and commits when filled', async () => {
    const store = useGitStore()
    const wrapper = mountSection()
    const commitButton = wrapper.get('[data-testid="git-commit"]')

    expect(commitButton.attributes('disabled')).toBeDefined()

    await wrapper
      .get('[data-testid="git-commit-message"]')
      .setValue('Add git panel')
    expect(commitButton.attributes('disabled')).toBeUndefined()

    const commitsBefore = store.currentRepo!.commits.length
    await commitButton.trigger('click')

    expect(store.currentRepo!.commits.length).toBe(commitsBefore + 1)
    expect(store.currentRepo!.commits[0]!.message).toBe('Add git panel')
    expect(store.stagedChanges.length).toBe(0)
    expect(
      (wrapper.get('[data-testid="git-commit-message"]').element as HTMLTextAreaElement)
        .value,
    ).toBe('')
  })

  it('commits via Cmd/Ctrl+Enter from the message box', async () => {
    const store = useGitStore()
    const wrapper = mountSection()
    const commitsBefore = store.currentRepo!.commits.length

    const message = wrapper.get('[data-testid="git-commit-message"]')
    await message.setValue('Keyboard commit')
    await message.trigger('keydown', { key: 'Enter', metaKey: true })

    expect(store.currentRepo!.commits.length).toBe(commitsBefore + 1)
  })

  it('asks for confirmation before discarding a change', async () => {
    const store = useGitStore()
    const wrapper = mountSection()
    const target = store.unstagedChanges[0]!.path

    await wrapper.find('[data-testid="git-discard-file"]').trigger('click')
    await nextTick()

    const confirm = document.body.querySelector(
      '[data-testid="git-discard-confirm"]',
    )
    expect(confirm).not.toBeNull()

    ;(confirm as HTMLElement).click()
    await nextTick()

    expect(
      store.currentRepo!.changes.some((c) => c.path === target),
    ).toBe(false)
  })

  it('opens the diff view when a change is clicked', async () => {
    const store = useGitStore()
    const wrapper = mountSection()
    const row = wrapper.find('[data-testid="git-changed-file"]')
    const path = store.unstagedChanges[0]!.path

    await row.trigger('click')

    expect(store.viewingDiffPath).toBe(path)
  })
})
