import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitBranchDialog from './GitBranchDialog.vue'
import * as gitApi from '@/services/git.api'
import { useGitStore } from '@/stores/git'
import { makeRepoSnapshot,
  setupGitRepos,
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

const getRepos = vi.mocked(gitApi.getGitRepos)
const getRepo = vi.mocked(gitApi.getGitRepo)
const getHistory = vi.mocked(gitApi.getGitHistory)
const runOp = vi.mocked(gitApi.runGitOperation)

function mountDialog(props: Record<string, unknown> = {}) {
  return mount(GitBranchDialog, {
    props: { open: true, mode: 'create', fromHash: 'f4a9c21', ...props },
    global: {
      stubs: {
        // Teleport-heavy reka Dialog → inline stub; props drive visibility.
        Dialog: { template: '<div><slot /></div>' },
        DialogContent: {
          inheritAttrs: false,
          template: '<div v-bind="$attrs"><slot /></div>',
        },
        DialogHeader: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
        DialogBody: { template: '<div><slot /></div>' },
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

describe('GitBranchDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot()])
    runOp.mockImplementation(async (_ws, payload) => ({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
      operation: payload.operation,
    }) as never)
  })

  it('creates a branch and closes on success', async () => {
    await initStore()
    const wrapper = mountDialog()
    await wrapper.find('[data-testid="git-branch-name"]').setValue('feature/new-ui')
    await nextTick()

    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
    } as never)
    await wrapper.find('#git-branch-form').trigger('submit')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'create_branch',
      branch: 'feature/new-ui',
      start_point: 'f4a9c21',
      checkout: true,
    })
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('renames a branch with old/new names and closes on success', async () => {
    await initStore()
    const wrapper = mountDialog({ mode: 'rename', branchName: 'old-name' })
    await nextTick()
    await wrapper.find('[data-testid="git-branch-name"]').setValue('new-name')
    await nextTick()

    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
    } as never)
    await wrapper.find('#git-branch-form').trigger('submit')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'rename_branch',
      new_branch: 'new-name',
      old_branch: 'old-name',
    })
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('keeps submit disabled for an empty name', async () => {
    await initStore()
    const wrapper = mountDialog()
    await nextTick()

    expect(wrapper.find('[data-testid="git-branch-submit"]').attributes('disabled')).toBeDefined()
    expect(runOp).not.toHaveBeenCalled()
  })

  it('stays open on failure and blocks double submits', async () => {
    await initStore()
    const wrapper = mountDialog()
    await wrapper.find('[data-testid="git-branch-name"]').setValue('feature/retry')
    await nextTick()

    let release!: (value: unknown) => void
    runOp.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve as (value: unknown) => void
      }),
    )
    await wrapper.find('#git-branch-form').trigger('submit')
    await nextTick()
    // Second click while submitting must not issue a second operation.
    await wrapper.find('#git-branch-form').trigger('submit')
    await nextTick()
    release({ ok: true, snapshot: makeRepoSnapshot(), repo_path: '/workspace/repo-app' })
    await flushPromises()

    const createCalls = runOp.mock.calls.filter((c) => c[1]?.operation === 'create_branch')
    expect(createCalls).toHaveLength(1)
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('stays open when the operation fails', async () => {
    const { ApiRequestError } = await import('@/services/api')
    await initStore()
    const wrapper = mountDialog()
    await wrapper.find('[data-testid="git-branch-name"]').setValue('feature/fails')
    await nextTick()

    runOp.mockRejectedValueOnce(new ApiRequestError(400, 'bad name', 'bad_name'))
    await wrapper.find('#git-branch-form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('update:open')).toBeUndefined()
    // Recovers: next submit works again.
    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
    } as never)
    await wrapper.find('#git-branch-form').trigger('submit')
    await flushPromises()
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })
})
