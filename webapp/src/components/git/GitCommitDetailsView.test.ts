import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitCommitDetailsView from './GitCommitDetailsView.vue'
import * as gitApi from '@/services/git.api'
import { useGitStore } from '@/stores/git'
import {
  makeCommitDetails,
  makeCommitFile,
  makeRepoSnapshot,
} from '@/stores/git.fixtures'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('@/services/git.api', () => ({
  conflictSnapshotOf: vi.fn(() => null),
  getGitCommitDetails: vi.fn(),
  getGitSnapshot: vi.fn(),
  getGitWorkingDiff: vi.fn(),
  runGitOperation: vi.fn(),
}))

const getSnapshot = vi.mocked(gitApi.getGitSnapshot)
const getDetails = vi.mocked(gitApi.getGitCommitDetails)

function stubPointerCapture(): void {
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => true)
}

function mountDetails(hash: string) {
  return mount(GitCommitDetailsView, { props: { hash } })
}

async function initStore() {
  const store = useGitStore()
  await store.initialize('workspace-1')
  return store
}

describe('GitCommitDetailsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubPointerCapture()
    localStorage.clear()
    getSnapshot.mockResolvedValue({ ok: true, repos: [makeRepoSnapshot()] })
    getDetails.mockImplementation(async (_ws, repo, hash) => ({
      ok: true,
      repo_path: repo,
      details: makeCommitDetails(hash),
    }))
  })

  it('renders summary and files for the expanded commit (lazy getDetails)', async () => {
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    await store.toggleCommitDetails(hash)
    await flushPromises()
    const wrapper = mountDetails(hash)
    await nextTick()

    expect(getDetails).toHaveBeenCalledWith('workspace-1', '/workspace/repo-app', hash)
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
      expect(wrapper.find(`[data-testid="git-cdv-file-${file.newPath}"]`).exists()).toBe(true)
    }
  })

  it('falls back to the details subject when the snapshot row is paged out', async () => {
    const store = await initStore()
    // Hash not in the snapshot history → subject from lazy details.
    getDetails.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      details: makeCommitDetails('paged-out', { message: 'Paged out subject' }),
    })
    await store.toggleCommitDetails('paged-out')
    await flushPromises()
    const wrapper = mountDetails('paged-out')
    await nextTick()

    expect(wrapper.find('[data-testid="git-cdv-subject"]').text()).toBe('Paged out subject')
  })

  it('shows parent navigation, body and diff stats in the summary', async () => {
    const store = await initStore()
    getDetails.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      details: makeCommitDetails('9c2f1e7', {
        parents: ['3d8e5b2', 'b7c4a19'],
        body: 'Extended description',
        file_changes: [makeCommitFile('a.ts', { additions: 5, deletions: 2 })],
      }),
    })
    await store.toggleCommitDetails('9c2f1e7')
    await flushPromises()
    const wrapper = mountDetails('9c2f1e7')
    await nextTick()
    const details = store.expandedCommitDetails!

    for (const parent of details.parents) {
      expect(wrapper.find(`[data-testid="git-cdv-parent-${parent}"]`).exists()).toBe(true)
    }
    expect(wrapper.find('[data-testid="git-cdv-body"]').text()).toContain('Extended description')
    const additions = details.fileChanges.reduce((s, f) => s + f.additions, 0)
    const deletions = details.fileChanges.reduce((s, f) => s + f.deletions, 0)
    expect(wrapper.text()).toContain(`+${additions}`)
    expect(wrapper.text()).toContain(`−${deletions}`)
  })

  it('navigates to a parent commit via lazy getDetails', async () => {
    const store = await initStore()
    await store.toggleCommitDetails('9c2f1e7')
    await flushPromises()
    const wrapper = mountDetails('9c2f1e7')
    await nextTick()

    getDetails.mockClear()
    await wrapper.find('[data-testid="git-cdv-parent-parent-1"]').trigger('click')
    await flushPromises()
    expect(getDetails).toHaveBeenCalledWith('workspace-1', '/workspace/repo-app', 'parent-1')
    expect(store.expandedCommitHash).toBe('parent-1')
  })

  it('shows a root loading state while details are fetching', async () => {
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    let release!: (value: unknown) => void
    getDetails.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve as (value: unknown) => void
      }),
    )
    const pending = store.toggleCommitDetails(hash)
    await nextTick()
    await nextTick()
    const wrapper = mountDetails(hash)
    await nextTick()

    expect(wrapper.find('[data-testid="git-commit-details-loading"]').exists()).toBe(true)
    release({ ok: true, repo_path: '/workspace/repo-app', details: makeCommitDetails(hash) })
    await pending
    await flushPromises()
    expect(store.expandedCommitDetails?.hash).toBe(hash)
  })

  it('shows a root error state with retry', async () => {
    const { ApiRequestError } = await import('@/services/api')
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    getDetails.mockRejectedValueOnce(new ApiRequestError(404, 'unknown commit', 'unknown_commit'))
    await store.toggleCommitDetails(hash)
    await flushPromises()
    const wrapper = mountDetails(hash)
    await nextTick()

    expect(wrapper.find('[data-testid="git-commit-details-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-commit-details-error"]').text()).toContain('unknown commit')

    getDetails.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      details: makeCommitDetails(hash),
    })
    await wrapper.find('[data-testid="git-cdv-retry"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(store.expandedCommitDetails?.hash).toBe(hash)
  })

  it('selects a file for diff in the main area and toggles selection', async () => {
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    await store.toggleCommitDetails(hash)
    await flushPromises()
    const wrapper = mountDetails(hash)
    await nextTick()
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
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    await store.toggleCommitDetails(hash)
    await flushPromises()
    const wrapper = mountDetails(hash)
    await nextTick()

    await wrapper.find('[data-testid="git-cdv-view-list"]').trigger('click')
    await nextTick()
    expect(localStorage.getItem('opencuria:git:cdvViewType')).toBe('list')

    await wrapper.find('[data-testid="git-cdv-view-tree"]').trigger('click')
    await nextTick()
    expect(localStorage.getItem('opencuria:git:cdvViewType')).toBe('tree')
  })

  it('resizes the details height and persists it in the store', async () => {
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    await store.toggleCommitDetails(hash)
    await flushPromises()
    const wrapper = mountDetails(hash)
    await nextTick()
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
    expect(localStorage.getItem('opencuria:git:cdvHeight')).toBe(String(startHeight + 60))
  })

  it('closes the details via the close button', async () => {
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    await store.toggleCommitDetails(hash)
    await flushPromises()
    const wrapper = mountDetails(hash)
    await nextTick()

    await wrapper.find('[data-testid="git-cdv-close"]').trigger('click')
    expect(store.expandedCommitHash).toBeNull()
  })

  it('renders an empty state for commits without files', async () => {
    const store = await initStore()
    const hash = 'empty-commit'
    getDetails.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      details: makeCommitDetails(hash, { file_changes: [] }),
    })
    await store.toggleCommitDetails(hash)
    await flushPromises()
    const wrapper = mountDetails(hash)
    await nextTick()

    expect(wrapper.text()).toContain('No files changed in this commit.')
  })
})
