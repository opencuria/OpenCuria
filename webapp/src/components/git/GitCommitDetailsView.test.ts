import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitCommitDetailsView from './GitCommitDetailsView.vue'
import { useGitStore } from '@/stores/git'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

function stubPointerCapture(): void {
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => true)
}

function mountDetails(hash: string) {
  return mount(GitCommitDetailsView, { props: { hash } })
}

describe('GitCommitDetailsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    stubPointerCapture()
    localStorage.clear()
  })

  it('renders summary and files for the expanded commit', () => {
    const store = useGitStore()
    const hash = store.currentRepo!.commits[0]!.hash
    store.toggleCommitDetails(hash)
    const wrapper = mountDetails(hash)

    expect(wrapper.find('[data-testid="git-commit-details"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-cdv-subject"]').text()).toBe(
      store.currentRepo!.commits[0]!.message,
    )
    expect(wrapper.find('[data-testid="git-cdv-hash"]').text()).toContain(hash)
    expect(wrapper.find('[data-testid="git-cdv-copy-hash"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-cdv-parents"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Files (')
    const fileCount = store.expandedCommitDetails!.fileChanges.length
    expect(fileCount).toBeGreaterThan(0)
    for (const file of store.expandedCommitDetails!.fileChanges) {
      expect(
        wrapper.find(`[data-testid="git-cdv-file-${file.newPath}"]`).exists(),
      ).toBe(true)
    }
  })

  it('shows parent navigation, body and diff stats in the summary', () => {
    const store = useGitStore()
    store.toggleCommitDetails('9c2f1e7')
    const wrapper = mountDetails('9c2f1e7')
    const details = store.expandedCommitDetails!

    for (const parent of details.parents) {
      expect(wrapper.find(`[data-testid="git-cdv-parent-${parent}"]`).exists()).toBe(
        true,
      )
    }
    const additions = details.fileChanges.reduce((s, f) => s + f.additions, 0)
    const deletions = details.fileChanges.reduce((s, f) => s + f.deletions, 0)
    expect(wrapper.text()).toContain(`+${additions}`)
    expect(wrapper.text()).toContain(`−${deletions}`)
  })

  it('selects a file for diff in the main area and toggles selection', async () => {
    const store = useGitStore()
    const hash = store.currentRepo!.commits[0]!.hash
    store.toggleCommitDetails(hash)
    const wrapper = mountDetails(hash)
    const file = store.expandedCommitDetails!.fileChanges[0]!

    await wrapper.find(`[data-testid="git-cdv-file-${file.newPath}"]`).trigger('click')
    expect(store.expandedFilePath).toBe(file.newPath)
    expect(store.viewingCommitDiff?.hash).toBe(hash)
    // Working-tree diffs stay exclusive.
    expect(store.viewingDiffPath).toBeNull()

    await wrapper.find(`[data-testid="git-cdv-file-${file.newPath}"]`).trigger('click')
    expect(store.expandedFilePath).toBeNull()
    expect(store.viewingCommitDiff).toBeNull()
  })

  it('switches between tree and list views', async () => {
    const store = useGitStore()
    const hash = store.currentRepo!.commits[0]!.hash
    store.toggleCommitDetails(hash)
    const wrapper = mountDetails(hash)

    await wrapper.find('[data-testid="git-cdv-view-list"]').trigger('click')
    await nextTick()
    expect(localStorage.getItem('opencuria:git:cdvViewType')).toBe('list')

    await wrapper.find('[data-testid="git-cdv-view-tree"]').trigger('click')
    await nextTick()
    expect(localStorage.getItem('opencuria:git:cdvViewType')).toBe('tree')
  })

  it('resizes the details height and persists it in the store', async () => {
    const store = useGitStore()
    const hash = store.currentRepo!.commits[0]!.hash
    store.toggleCommitDetails(hash)
    const wrapper = mountDetails(hash)
    const handle = wrapper.find('[data-testid="git-cdv-resize"]')
    const startHeight = store.cdvHeight

    const down = new PointerEvent('pointerdown', { bubbles: true })
    Object.defineProperty(down, 'clientY', { value: 200 })
    handle.element.dispatchEvent(down)
    await nextTick()

    const move = new PointerEvent('pointermove', { bubbles: true })
    Object.defineProperty(move, 'clientY', { value: 260 })
    handle.element.dispatchEvent(move)
    await nextTick()
    handle.element.dispatchEvent(new PointerEvent('pointerup'))
    await nextTick()

    expect(store.cdvHeight).toBe(startHeight + 60)
    expect(localStorage.getItem('opencuria:git:cdvHeight')).toBe(
      String(startHeight + 60),
    )
  })

  it('closes the details via the close button', async () => {
    const store = useGitStore()
    const hash = store.currentRepo!.commits[0]!.hash
    store.toggleCommitDetails(hash)
    const wrapper = mountDetails(hash)

    await wrapper.find('[data-testid="git-cdv-close"]').trigger('click')
    expect(store.expandedCommitHash).toBeNull()
  })

  it('renders an empty state for commits without files', () => {
    const store = useGitStore()
    store.commit('Add git panel')
    const created = store.currentRepo!.commits[0]!
    store.toggleCommitDetails(created.hash)
    const wrapper = mountDetails(created.hash)

    expect(wrapper.text()).toContain('No files changed in this commit.')
  })
})
