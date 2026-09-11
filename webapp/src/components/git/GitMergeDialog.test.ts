import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitMergeDialog from './GitMergeDialog.vue'
import * as gitApi from '@/services/git.api'
import { ApiRequestError } from '@/services/api'
import { useGitStore } from '@/stores/git'
import { makeRawChange, makeRepoSnapshot } from '@/stores/git.fixtures'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('@/services/git.api', () => ({
  conflictSnapshotOf: vi.fn((data: unknown) => {
    const snapshot = (data as { snapshot?: unknown } | null)?.snapshot
    return snapshot && typeof snapshot === 'object' ? snapshot : null
  }),
  getGitCommitDetails: vi.fn(),
  getGitSnapshot: vi.fn(),
  getGitWorkingDiff: vi.fn(),
  runGitOperation: vi.fn(),
}))

const getSnapshot = vi.mocked(gitApi.getGitSnapshot)
const runOp = vi.mocked(gitApi.runGitOperation)

function mountDialog(props: Record<string, unknown> = {}) {
  return mount(GitMergeDialog, {
    props: { open: true, direction: 'into-current', branch: 'feature/git-panel', ...props },
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

describe('GitMergeDialog', () => {
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

  it('merges into the current branch and closes on success', async () => {
    await initStore()
    const wrapper = mountDialog()
    await nextTick()

    // Fast-forward wording, no unconditional merge-commit claim.
    expect(wrapper.text()).toContain('fast-forward')

    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
    } as never)
    await wrapper.find('[data-testid="git-merge-confirm"]').trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'merge_into_current',
      branch: 'feature/git-panel',
    })
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('merges the current branch into the target branch', async () => {
    await initStore()
    const wrapper = mountDialog({ direction: 'current-into' })
    await nextTick()

    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
    } as never)
    await wrapper.find('[data-testid="git-merge-confirm"]').trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'merge_current_into',
      target: 'feature/git-panel',
    })
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('closes on conflict after the snapshot was applied (guides to the banner)', async () => {
    const store = await initStore()
    const wrapper = mountDialog()
    await nextTick()

    const conflicted = makeRepoSnapshot({
      mergeState: { merging: true, rebasing: false, cherry_picking: false },
      changes: [
        makeRawChange('conflicted.ts', {
          status: 'U',
          staged: true,
          staged_kind: 'U',
          unstaged: 'U',
          conflict: 'UU',
        }),
      ],
    })
    runOp.mockRejectedValueOnce(
      new ApiRequestError(409, 'Merge conflict — resolve or run merge_abort', 'conflict', {
        ok: false,
        code: 'conflict',
        message: 'Merge conflict — resolve or run merge_abort',
        snapshot: conflicted,
      }),
    )
    await wrapper.find('[data-testid="git-merge-confirm"]').trigger('click')
    await flushPromises()

    expect(store.lastConflict).toBe(true)
    expect(store.currentRepo?.mergeState.merging).toBe(true)
    // Conflict closes the dialog (banner takes over), unlike plain failures.
    expect(wrapper.emitted('update:open')).toMatchObject([[false]])
  })

  it('stays open on generic failure and blocks double submits', async () => {
    await initStore()
    const wrapper = mountDialog()
    await nextTick()

    runOp.mockRejectedValueOnce(new ApiRequestError(400, 'merge failed', 'merge_failed'))
    await wrapper.find('[data-testid="git-merge-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()

    // Double submit guard.
    vi.clearAllMocks()
    let release!: (value: unknown) => void
    runOp.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve as (value: unknown) => void
      }),
    )
    await wrapper.find('[data-testid="git-merge-confirm"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="git-merge-confirm"]').trigger('click')
    await nextTick()
    release({ ok: true, snapshot: makeRepoSnapshot(), repo_path: '/workspace/repo-app' })
    await flushPromises()

    expect(runOp.mock.calls.filter((c) => String(c[1]?.operation).startsWith('merge'))).toHaveLength(1)
  })

  it('stays open on generic 409 without a merge snapshot (no conflict)', async () => {
    const { toast } = await import('vue-sonner')
    const store = await initStore()
    const wrapper = mountDialog()
    await nextTick()

    runOp.mockRejectedValueOnce(
      new ApiRequestError(409, 'Workspace is busy', 'workspace_conflict', {
        ok: false,
        detail: 'Workspace is busy',
        code: 'workspace_conflict',
      }),
    )
    await wrapper.find('[data-testid="git-merge-confirm"]').trigger('click')
    await flushPromises()

    expect(store.lastConflict).toBe(false)
    expect(store.currentRepo?.mergeState.merging).toBe(false)
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(toast.warning).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalled()
  })
})
