import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitDeleteBranchDialog from './GitDeleteBranchDialog.vue'
import * as gitApi from '@/services/git.api'
import { ApiRequestError } from '@/services/api'
import { useGitStore } from '@/stores/git'
import { makeRepoSnapshot } from '@/stores/git.fixtures'

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
const runOp = vi.mocked(gitApi.runGitOperation)

function mountDialog(branch = 'feature/git-panel') {
  return mount(GitDeleteBranchDialog, {
    props: { open: true, branch },
    global: {
      stubs: {
        Dialog: { template: '<div><slot /></div>' },
        DialogContent: {
          inheritAttrs: false,
          template: '<div v-bind="$attrs"><slot /></div>',
        },
        DialogHeader: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
        DialogFooter: { template: '<div><slot /></div>' },
      },
    },
  })
}

async function initStore() {
  const store = useGitStore()
  await store.initialize('workspace-1')
  return store
}

describe('GitDeleteBranchDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    getSnapshot.mockResolvedValue({ ok: true, repos: [makeRepoSnapshot()] })
    runOp.mockImplementation(async (_ws, payload) => ({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
      operation: payload.operation,
    }) as never)
  })

  it('shows the branch name and the safe-delete hint', async () => {
    await initStore()
    const wrapper = mountDialog()
    await nextTick()

    expect(wrapper.find('[data-testid="git-delete-branch-dialog"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('feature/git-panel')
    expect(wrapper.text()).toContain('git branch -d')
  })

  it('deletes the branch with a typed payload and closes on success', async () => {
    await initStore()
    const wrapper = mountDialog()
    await nextTick()

    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
    } as never)
    await wrapper.find('[data-testid="git-delete-branch-confirm"]').trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'delete_branch',
      branch: 'feature/git-panel',
    })
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('stays open on failure and prevents double submits', async () => {
    await initStore()
    const wrapper = mountDialog()
    await nextTick()

    runOp.mockRejectedValueOnce(new ApiRequestError(400, 'not fully merged', 'not_merged'))
    await wrapper.find('[data-testid="git-delete-branch-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()

    vi.clearAllMocks()
    let release!: (value: unknown) => void
    runOp.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve as (value: unknown) => void
      }),
    )
    await wrapper.find('[data-testid="git-delete-branch-confirm"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="git-delete-branch-confirm"]').trigger('click')
    await nextTick()
    release({ ok: true, snapshot: makeRepoSnapshot(), repo_path: '/workspace/repo-app' })
    await flushPromises()

    expect(runOp).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('disables confirm for the current branch', async () => {
    await initStore()
    const wrapper = mountDialog('main')
    await nextTick()

    expect(
      wrapper.find('[data-testid="git-delete-branch-confirm"]').attributes('disabled'),
    ).toBeDefined()
    expect(runOp).not.toHaveBeenCalled()
  })
})
