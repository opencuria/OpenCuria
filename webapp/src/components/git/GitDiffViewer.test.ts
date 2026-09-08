import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import GitDiffViewer from './GitDiffViewer.vue'
import { useGitStore } from '@/stores/git'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

function mountViewer() {
  return mount(GitDiffViewer, { props: { workspaceId: 'ws-1' } })
}

describe('GitDiffViewer', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('renders a working-tree diff with staged badge', () => {
    const store = useGitStore()
    store.openDiff(store.currentRepo!.changes[0]!.path)
    const wrapper = mountViewer()

    expect(wrapper.find('[data-testid="git-diff-viewer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-name"]').text()).toBe(
      store.viewingDiffChange!.path.split('/').pop(),
    )
    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-status"]').text()).toContain(
      'Staged',
    )
  })

  it('renders a commit file diff with the commit hash badge', () => {
    const store = useGitStore()
    const hash = store.currentRepo!.commits[0]!.hash
    store.toggleCommitDetails(hash)
    const file = store.expandedCommitDetails!.fileChanges[0]!
    store.selectCommitFile(file.newPath)
    const wrapper = mountViewer()

    expect(wrapper.find('[data-testid="git-diff-name"]').text()).toBe(
      file.newPath.split('/').pop(),
    )
    expect(wrapper.find('[data-testid="git-diff-path"]').text()).toContain(
      file.newPath.split('/').slice(0, -1).join('/'),
    )
    expect(wrapper.find('[data-testid="git-diff-status"]').text()).toContain(
      hash.slice(0, 7),
    )
    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(true)
  })

  it('closes a commit diff via selection reset and a working diff via closeDiff', async () => {
    const store = useGitStore()
    const hash = store.currentRepo!.commits[0]!.hash
    store.toggleCommitDetails(hash)
    store.selectCommitFile(store.expandedCommitDetails!.fileChanges[0]!.newPath)
    const commitWrapper = mountViewer()
    await commitWrapper.find('[data-testid="git-diff-close"]').trigger('click')
    expect(store.viewingCommitDiff).toBeNull()
    expect(store.expandedCommitHash).toBe(hash)

    store.openDiff(store.currentRepo!.changes[0]!.path)
    const workingWrapper = mountViewer()
    await workingWrapper.find('[data-testid="git-diff-close"]').trigger('click')
    expect(store.viewingDiffChange).toBeNull()
  })
})
