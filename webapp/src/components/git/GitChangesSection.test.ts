import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitChangesSection from './GitChangesSection.vue'
import * as gitApi from '@/services/git.api'
import { useGitStore } from '@/stores/git'
import {
  makeRawChange,
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
const getDiff = vi.mocked(gitApi.getGitWorkingDiff)
const getDetails = vi.mocked(gitApi.getGitCommitDetails)
const runOp = vi.mocked(gitApi.runGitOperation)

function mountSection() {
  return mount(GitChangesSection, {
    attachTo: document.body,
    global: {
      stubs: {
        // Reka dropdown/tooltip internals are teleported; passthrough stubs
        // keep menu items + tooltips inline for assertions.
        DropdownMenu: { template: '<div><slot /></div>' },
        DropdownMenuTrigger: { template: '<div><slot /></div>' },
        DropdownMenuContent: { template: '<div><slot /></div>' },
        DropdownMenuItem: {
          inheritAttrs: false,
          props: { disabled: { type: Boolean, default: false } },
          emits: ['click'],
          template:
            '<button type="button" v-bind="$attrs" :disabled="disabled" @click="$emit(\'click\', $event)"><slot /></button>',
        },
        TooltipProvider: { template: '<div><slot /></div>' },
        Tooltip: { template: '<div><slot /></div>' },
        TooltipTrigger: { template: '<div><slot /></div>' },
        TooltipContent: { template: '<div><slot /></div>' },
      },
    },
  })
}

/** Bind the store to a workspace snapshot before mounting the section. */
async function initStore() {
  const store = useGitStore()
  await store.initialize('workspace-1')
  return store
}

describe('GitChangesSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    getSnapshot.mockResolvedValue({ ok: true, repos: [makeRepoSnapshot()] })
    getDiff.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: { staged: [], unstaged: [] },
    })
    getDetails.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      details: {
        hash: 'f4a9c21',
        message: 'commit f4a9c21',
        body: '',
        parents: [],
        author: 'Timo Kamphaus',
        author_email: 'timo@opencuria.local',
        author_date: '2026-09-05T10:00:00Z',
        committer: 'Timo Kamphaus',
        committer_email: 'timo@opencuria.local',
        committer_date: '2026-09-05T10:00:00Z',
        file_changes: [],
      },
    })
    runOp.mockImplementation(async (_ws, payload) => ({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
      operation: payload.operation,
    }) as never)
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('groups changes into staged and unstaged lists', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    // Default fixture: 1 staged, 1 unstaged.
    expect(wrapper.get('[data-testid="git-staged-trigger"]').text()).toContain('1')
    expect(wrapper.get('[data-testid="git-changes-trigger"]').text()).toContain('1')
    expect(wrapper.findAll('[data-testid="git-staged-file"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="git-changed-file"]')).toHaveLength(1)
  })

  it('shows the same path on both sides with per-side selection', async () => {
    const both = makeRawChange('both/sides.ts', {
      status: 'M',
      staged: true,
      staged_kind: 'M',
      unstaged: 'M',
    })
    getSnapshot.mockResolvedValue({ ok: true, repos: [makeRepoSnapshot({ changes: [both] })] })
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.findAll('[data-testid="git-staged-file"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="git-changed-file"]')).toHaveLength(1)

    await wrapper.find('[data-testid="git-staged-file"]').trigger('click')
    await flushPromises()
    expect(store.viewingDiffPath).toBe('both/sides.ts')
    expect(store.viewingDiffStaged).toBe(true)

    await wrapper.find('[data-testid="git-changed-file"]').trigger('click')
    await flushPromises()
    expect(store.viewingDiffPath).toBe('both/sides.ts')
    expect(store.viewingDiffStaged).toBe(false)
  })

  it('stages a file via its hover action with a typed payload', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()

    const unstagedPath = store.unstagedChanges[0]!.path
    // After staging, the mutation snapshot (default fixture: same snapshot)
    // is applied — assert the API payload instead of local list mutation.
    await wrapper.find('[data-testid="git-stage-file"]').trigger('click')
    await flushPromises()

    expect(runOp).toHaveBeenCalled()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'stage',
      repo_path: '/workspace/repo-app',
      paths: [unstagedPath],
    })
  })

  it('unstages a staged file via its hover action', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()

    const stagedPath = store.stagedChanges[0]!.path
    await wrapper.find('[data-testid="git-unstage-file"]').trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'unstage',
      paths: [stagedPath],
    })
  })

  it('stages all and unstages all via the group headers', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.find('[data-testid="git-stage-all"]').trigger('click')
    await flushPromises()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'stage',
    })

    await wrapper.find('[data-testid="git-unstage-all"]').trigger('click')
    await flushPromises()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'unstage',
    })
  })

  it('keeps commit disabled without a message and commits when filled', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const commitButton = wrapper.get('[data-testid="git-commit"]')

    expect(commitButton.attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="git-commit-message"]').setValue('Add git panel')
    await nextTick()
    expect(commitButton.attributes('disabled')).toBeUndefined()

    // Mutation resolves with a fresh empty-changes snapshot.
    const empty = makeRepoSnapshot({ changes: [] })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: empty, repo_path: '/workspace/repo-app' } as never)
    await commitButton.trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'commit',
      message: 'Add git panel',
    })
    expect(store.currentRepo?.changes).toEqual([])
    expect(
      (wrapper.get('[data-testid="git-commit-message"]').element as HTMLTextAreaElement)
        .value,
    ).toBe('')
  })

  it('keeps commit disabled without staged changes', async () => {
    getSnapshot.mockResolvedValue({ ok: true, repos: [makeRepoSnapshot({ changes: [] })] })
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.get('[data-testid="git-commit-message"]').setValue('Work')
    await nextTick()
    expect(wrapper.get('[data-testid="git-commit"]').attributes('disabled')).toBeDefined()
  })

  it('commits via Cmd/Ctrl+Enter from the message box', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()

    const message = wrapper.get('[data-testid="git-commit-message"]')
    await message.setValue('Keyboard commit')
    const empty = makeRepoSnapshot({ changes: [] })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: empty, repo_path: '/workspace/repo-app' } as never)
    await message.trigger('keydown', { key: 'Enter', metaKey: true })
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'commit',
      message: 'Keyboard commit',
    })
    expect(store.currentRepo?.changes).toEqual([])
  })

  it('pushes via the push button', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.get('[data-testid="git-push"]').trigger('click')
    await flushPromises()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'push',
    })
  })

  it('disables push when up to date', async () => {
    const clean = makeRepoSnapshot({
      branches: [{ name: 'main', tip_hash: 'f4a9c21', upstream: 'origin/main', ahead: 0, behind: 0 }],
    })
    getSnapshot.mockResolvedValue({ ok: true, repos: [clean] })
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.get('[data-testid="git-push"]').attributes('disabled')).toBeDefined()
  })

  it('enables Publish for branches without upstream and publishes with set_upstream', async () => {
    const fresh = makeRepoSnapshot({
      currentBranch: 'feature/fresh',
      branches: [{ name: 'feature/fresh', tip_hash: 'f4a9c21', upstream: null, ahead: 0, behind: 0 }],
    })
    getSnapshot.mockResolvedValue({ ok: true, repos: [fresh] })
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    const push = wrapper.get('[data-testid="git-push"]')
    expect(push.attributes('disabled')).toBeUndefined()
    expect(push.text()).toContain('Publish')
    expect(push.attributes('title')).toContain('Publish')

    // Without an upstream, Pull/Sync stay disabled until the branch is published.
    expect(wrapper.find('[data-testid="git-pull"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="git-pull"]').attributes('title')).toContain('Publish branch first')
    expect(wrapper.find('[data-testid="git-sync"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="git-sync"]').attributes('title')).toContain('Publish branch first')

    await push.trigger('click')
    await flushPromises()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'push',
      set_upstream: true,
    })
  })

  it('shows ahead/behind and offers pull/sync remote actions', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.find('[data-testid="git-remote-actions"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-pull"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-sync"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-pull"]').attributes('title')).toContain('Fetch')
    expect(wrapper.find('[data-testid="git-sync"]').attributes('title')).toContain('Pull then push')

    await wrapper.find('[data-testid="git-pull"]').trigger('click')
    await flushPromises()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'pull',
      repo_path: '/workspace/repo-app',
    })

    await wrapper.find('[data-testid="git-sync"]').trigger('click')
    await flushPromises()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'sync',
      repo_path: '/workspace/repo-app',
    })
  })

  it('disables pull/sync/push while detached or busy', async () => {
    getSnapshot.mockResolvedValue({
      ok: true,
      repos: [makeRepoSnapshot({ currentBranch: null, headHash: 'e8b7d3a' })],
    })
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.find('[data-testid="git-pull"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="git-sync"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="git-push"]').attributes('disabled')).toBeDefined()
  })

  it('does not double-fire sync on parallel clicks (serialized)', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    let release!: (value: unknown) => void
    runOp.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve as (value: unknown) => void
      }),
    )
    const first = wrapper.find('[data-testid="git-sync"]').trigger('click')
    // The store serializes mutations: while the first op is queued/running,
    // `busyOperation` is set — a second click must not fire another op.
    await flushPromises()
    await nextTick()
    const store = useGitStore()
    expect(store.busyOperation).toBe('sync')
    await wrapper.find('[data-testid="git-sync"]').trigger('click')
    await flushPromises()
    release({ ok: true, snapshot: makeRepoSnapshot(), repo_path: '/workspace/repo-app' })
    await first
    await flushPromises()

    expect(runOp.mock.calls.filter((c) => c[1]?.operation === 'sync')).toHaveLength(1)
  })

  it('shows a merge banner with conflict count and aborts via confirmation', async () => {
    const merging = makeRepoSnapshot({
      mergeState: { merging: true, rebasing: false, cherry_picking: false },
      changes: [
        makeRawChange('conflicted.ts', {
          status: 'U',
          staged: true,
          staged_kind: 'U',
          unstaged: 'U',
          conflict: 'UU',
        }),
        makeRawChange('other.ts', {
          status: 'U',
          staged: true,
          staged_kind: 'U',
          unstaged: 'U',
          conflict: 'AA',
        }),
      ],
    })
    getSnapshot.mockResolvedValue({ ok: true, repos: [merging] })
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()

    const banner = wrapper.find('[data-testid="git-merge-in-progress"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('2 conflicts')
    // Status U is labelled Unresolved.
    expect(wrapper.html()).toContain('title="Unresolved"')

    await wrapper.find('[data-testid="git-merge-abort"]').trigger('click')
    await nextTick()
    const confirm = document.body.querySelector('[data-testid="git-merge-abort-confirm"]')
    expect(confirm).not.toBeNull()

    const cleared = makeRepoSnapshot({ changes: [] })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: cleared, repo_path: '/workspace/repo-app' } as never)
    ;(confirm as HTMLElement).click()
    await flushPromises()
    await nextTick()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'merge_abort',
    })
    expect(store.currentRepo?.mergeState.merging).toBe(false)
    expect(wrapper.find('[data-testid="git-merge-in-progress"]').exists()).toBe(false)
  })

  it('keeps the abort dialog open when abort fails', async () => {
    const { ApiRequestError } = await import('@/services/api')
    const merging = makeRepoSnapshot({
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
    getSnapshot.mockResolvedValue({ ok: true, repos: [merging] })
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.find('[data-testid="git-merge-abort"]').trigger('click')
    await nextTick()
    const confirm = document.body.querySelector(
      '[data-testid="git-merge-abort-confirm"]',
    )
    expect(confirm).not.toBeNull()

    runOp.mockRejectedValueOnce(new ApiRequestError(400, 'abort failed', 'abort_failed'))
    ;(confirm as HTMLElement).click()
    await flushPromises()
    await nextTick()

    // Dialog stays open (confirm still mounted), banner still visible.
    expect(
      document.body.querySelector('[data-testid="git-merge-abort-confirm"]'),
    ).not.toBeNull()
    expect(wrapper.find('[data-testid="git-merge-in-progress"]').exists()).toBe(true)
  })

  it('asks for confirmation before discarding a change', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const target = store.unstagedChanges[0]!.path

    await wrapper.find('[data-testid="git-discard-file"]').trigger('click')
    await nextTick()

    // Reka dialog content is teleported to body.
    const confirm = document.body.querySelector('[data-testid="git-discard-confirm"]')
    expect(confirm).not.toBeNull()

    const cleared = makeRepoSnapshot({ changes: [] })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: cleared, repo_path: '/workspace/repo-app' } as never)
    ;(confirm as HTMLElement).click()
    await flushPromises()
    await nextTick()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'discard',
      paths: [target],
    })
  })

  it('opens the diff view when a change is clicked (lazy getDiff)', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const row = wrapper.find('[data-testid="git-changed-file"]')
    const path = store.unstagedChanges[0]!.path

    await row.trigger('click')
    await flushPromises()

    expect(store.viewingDiffPath).toBe(path)
    expect(store.viewingDiffStaged).toBe(false)
    expect(getDiff).toHaveBeenCalledWith('workspace-1', '/workspace/repo-app')
  })
})
