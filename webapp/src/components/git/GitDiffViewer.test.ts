import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitDiffViewer from './GitDiffViewer.vue'
import * as gitApi from '@/services/git.api'
import { ApiRequestError } from '@/services/api'
import { useGitStore } from '@/stores/git'
import {
  makeCommitDetails,
  makeCommitFile,
  makeRepoSnapshot,
  mockGitApi,
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
  getGitHistory: vi.fn(),
  getGitRepo: vi.fn(),
  getGitRepos: vi.fn(),
  getGitWorkingDiff: vi.fn(),
  runGitOperation: vi.fn(),
}))

const getDiff = vi.mocked(gitApi.getGitWorkingDiff)
const getDetails = vi.mocked(gitApi.getGitCommitDetails)

function mountViewer() {
  return mount(GitDiffViewer, { props: { workspaceId: 'workspace-1' } })
}

async function initStore() {
  const store = useGitStore()
  await store.initialize('workspace-1')
  return store
}

describe('GitDiffViewer', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    await mockGitApi([makeRepoSnapshot()])
  })

  it('renders a working-tree diff with staged badge', async () => {
    const store = await initStore()
    const path = store.stagedChanges[0]!.path
    const stagedRaw = makeCommitFile(path, {
      status: 'M',
      diff: [
        {
          header: '@@ -1,1 +1,2 @@ staged',
          old_start: 1,
          new_start: 1,
          lines: [
            { type: 'context', content: '// staged context' },
            { type: 'add', content: 'staged-line-marker' },
          ],
        },
      ],
    })
    const unstagedRaw = makeCommitFile('other/unstaged.ts', {
      diff: [
        {
          header: '@@ -1,1 +1,2 @@ unstaged',
          old_start: 1,
          new_start: 1,
          lines: [{ type: 'add', content: 'unstaged-line-marker' }],
        },
      ],
    })
    getDiff.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: { staged: [stagedRaw], unstaged: [unstagedRaw] },
    })

    await store.openDiff(path, true)
    expect(getDiff).toHaveBeenCalledWith('workspace-1', '/workspace/repo-app')
    const wrapper = mountViewer()
    await nextTick()

    expect(wrapper.find('[data-testid="git-diff-viewer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-name"]').text()).toBe(
      path.split('/').pop(),
    )
    expect(wrapper.find('[data-testid="git-diff-path"]').text()).toContain(
      path.split('/').slice(0, -1).join('/'),
    )
    // Snapshot row is `A` (Added) with a staged side.
    expect(wrapper.find('[data-testid="git-diff-status"]').text()).toContain(
      'Added',
    )
    expect(wrapper.find('[data-testid="git-diff-status"]').text()).toContain(
      'Staged',
    )
    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-code"]').text()).toContain(
      'staged-line-marker',
    )
    expect(wrapper.find('[data-testid="git-diff-code"]').text()).not.toContain(
      'unstaged-line-marker',
    )
  })

  it('renders a commit file diff with the commit hash badge', async () => {
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    getDetails.mockImplementation(async (_ws, repo, h) => ({
      ok: true,
      repo_path: repo,
      details: makeCommitDetails(h),
    }))

    await store.toggleCommitDetails(hash)
    const file = store.expandedCommitDetails!.fileChanges[0]!
    store.selectCommitFile(file.newPath)
    const wrapper = mountViewer()
    await nextTick()

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
    expect(wrapper.find('[data-testid="git-diff-code"]').text()).toContain(
      'const after = 2',
    )
  })

  it('closes a commit diff via selection reset and a working diff via closeDiff', async () => {
    const store = await initStore()
    const hash = store.currentRepo!.commits[0]!.hash
    await store.toggleCommitDetails(hash)
    store.selectCommitFile(store.expandedCommitDetails!.fileChanges[0]!.newPath)
    const commitWrapper = mountViewer()
    await nextTick()
    await commitWrapper.find('[data-testid="git-diff-close"]').trigger('click')
    expect(store.viewingCommitDiff).toBeNull()
    expect(store.expandedCommitHash).toBe(hash)

    const path = store.stagedChanges[0]!.path
    getDiff.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: { staged: [makeCommitFile(path)], unstaged: [] },
    })
    await store.openDiff(path, true)
    const workingWrapper = mountViewer()
    await nextTick()
    await workingWrapper.find('[data-testid="git-diff-close"]').trigger('click')
    expect(store.viewingDiffChange).toBeNull()
  })

  it('shows a loading state while the working diff is fetching', async () => {
    const store = await initStore()
    const path = store.stagedChanges[0]!.path
    let release!: (value: Awaited<ReturnType<typeof getDiff>>) => void
    getDiff.mockReturnValueOnce(
      new Promise<Awaited<ReturnType<typeof getDiff>>>((resolve) => {
        release = resolve
      }),
    )
    const pending = store.openDiff(path, true)
    await nextTick()
    await nextTick()
    const wrapper = mountViewer()
    await nextTick()

    expect(wrapper.find('[data-testid="git-diff-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(false)

    release({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: { staged: [makeCommitFile(path)], unstaged: [] },
    })
    await pending
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="git-diff-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(true)
  })

  it('shows an error with retry that re-requests the diff', async () => {
    const store = await initStore()
    const path = store.stagedChanges[0]!.path
    getDiff.mockRejectedValueOnce(new ApiRequestError(500, 'diff failed', 'error'))
    await store.openDiff(path, true)
    const wrapper = mountViewer()
    await nextTick()

    expect(wrapper.find('[data-testid="git-diff-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-error"]').text()).toContain(
      'diff failed',
    )

    const callsBefore = getDiff.mock.calls.length
    getDiff.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: { staged: [makeCommitFile(path)], unstaged: [] },
    })
    await wrapper.find('[data-testid="git-diff-retry"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(getDiff.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(wrapper.find('[data-testid="git-diff-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(true)
  })

  it('renders a binary notice without code', async () => {
    const store = await initStore()
    const path = store.stagedChanges[0]!.path
    getDiff.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: {
        staged: [
          makeCommitFile(path, { binary: true, has_textual_diff: false, diff: [] }),
        ],
        unstaged: [],
      },
    })

    await store.openDiff(path, true)
    const wrapper = mountViewer()
    await nextTick()

    expect(wrapper.find('[data-testid="git-diff-binary"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(false)
  })

  it('shows a truncated hint alongside partial hunks', async () => {
    const store = await initStore()
    const path = store.stagedChanges[0]!.path
    getDiff.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: {
        staged: [makeCommitFile(path, { truncated: true })],
        unstaged: [],
      },
    })

    await store.openDiff(path, true)
    const wrapper = mountViewer()
    await nextTick()

    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-code"]').text()).toContain(
      'const after = 2',
    )
    expect(wrapper.find('[data-testid="git-diff-truncated"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-truncated"]').text()).toContain(
      'truncated',
    )
  })

  it('shows an empty state when there is no textual diff', async () => {
    const store = await initStore()
    const path = store.stagedChanges[0]!.path
    getDiff.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: {
        staged: [
          makeCommitFile(path, { has_textual_diff: false, diff: [] }),
        ],
        unstaged: [],
      },
    })

    await store.openDiff(path, true)
    const wrapper = mountViewer()
    await nextTick()

    expect(wrapper.find('[data-testid="git-diff-code"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-diff-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-diff-empty"]').text()).toContain(
      'No textual diff',
    )
  })
})
