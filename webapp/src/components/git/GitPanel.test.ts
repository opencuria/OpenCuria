import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitPanel from './GitPanel.vue'
import * as gitApi from '@/services/git.api'
import { useGitStore } from '@/stores/git'
import {
  makeRawChange,
  makeRepoSnapshot,
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
const getDiff = vi.mocked(gitApi.getGitWorkingDiff)
const getDetails = vi.mocked(gitApi.getGitCommitDetails)
const runOp = vi.mocked(gitApi.runGitOperation)

const sectionStubs = {
  GitChangesSection: { template: '<div data-testid="stub-changes" />' },
  GitGraphSection: { template: '<div data-testid="stub-graph" />' },
}

function stubPointerCapture(): void {
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => true)
}

function stubContainerRect(): void {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    top: 0,
    height: 400,
    left: 0,
    right: 400,
    bottom: 400,
    width: 400,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect)
}

function mountPanel() {
  return mount(GitPanel, {
    props: { workspaceId: 'workspace-1' },
    global: { stubs: sectionStubs },
  })
}

async function dispatchPointer(
  element: Element,
  type: string,
  init: PointerEventInit = {},
): Promise<void> {
  element.dispatchEvent(new PointerEvent(type, { bubbles: true, ...init }))
  await nextTick()
}

describe('GitPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubPointerCapture()
    stubContainerRect()
    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot()])
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

  it('renders the repo header and both sections at a 50/50 split', async () => {
    const wrapper = mountPanel()
    // Panel mounts `initialize` itself — wait for the snapshot to land.
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="side-panel-git"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-repo-select"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-current-branch"]').text()).toBe('main')
    expect(wrapper.find('[data-testid="git-ahead"]').text()).toContain('2')
    expect(wrapper.find('[data-testid="stub-changes"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stub-graph"]').exists()).toBe(true)
    expect(
      wrapper.get('[data-testid="git-changes-container"]').attributes('style'),
    ).toContain('height: 50%')
    wrapper.unmount()
  })

  it('shows a loading state before the snapshot resolves', async () => {
    getRepos.mockReturnValue(new Promise(() => {}))
    const store = useGitStore()
    const wrapper = mountPanel()
    await nextTick()
    await nextTick()
    // initialize() is in flight: loading=true with empty repos → spinner.
    expect(store.loading).toBe(true)
    expect(store.repos).toHaveLength(0)
    expect(wrapper.find('[data-testid="git-loading"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows an error state with retry', async () => {
    const { ApiRequestError } = await import('@/services/api')
    getRepos.mockRejectedValue(new ApiRequestError(500, 'boom', 'error'))
    const wrapper = mountPanel()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="git-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-error"]').text()).toContain('boom')

    // Retry re-requests the snapshot.
    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot()])
    await wrapper.find('[data-testid="git-retry"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(getRepos.mock.calls.length).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })

  it('shows an empty state with refresh', async () => {
    setupGitRepos(getRepos, getRepo, getHistory, [])
    const wrapper = mountPanel()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="git-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-retry"]').exists()).toBe(true)

    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot()])
    await wrapper.find('[data-testid="git-retry"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(getRepos.mock.calls.length).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })

  it('updates the branch label when switching repositories', async () => {
    const second = makeRepoSnapshot({
      id: '/workspace/docs',
      path: '/workspace/docs',
      name: 'docs',
      currentBranch: 'main',
      headHash: 'd1o2c3s',
      branches: [
        { name: 'main', tip_hash: 'd1o2c3s', upstream: 'origin/main', ahead: 0, behind: 1 },
      ],
      changes: [
        makeRawChange('docs/readme.md', {
          status: 'M',
          staged: false,
          staged_kind: null,
          unstaged: 'M',
        }),
      ],
    })
    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot(), second])
    const store = useGitStore()
    const wrapper = mountPanel()
    await flushPromises()
    await nextTick()

    store.selectRepo('/workspace/docs')
    await flushPromises()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="git-current-branch"]').text()).toBe('main')
    expect(wrapper.find('[data-testid="git-behind"]').text()).toContain('1')
    expect(wrapper.text()).toContain('/workspace/docs')
    wrapper.unmount()
  })

  it('refreshes via the header button and fetches the remote', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await nextTick()
    const callsBefore = getRepos.mock.calls.length

    await wrapper.find('[data-testid="git-refresh"]').trigger('click')
    await flushPromises()
    expect(getRepos.mock.calls.length).toBeGreaterThan(callsBefore)

    await wrapper.find('[data-testid="git-fetch"]').trigger('click')
    await flushPromises()
    expect(runOp).toHaveBeenCalled()
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'fetch',
    })
    wrapper.unmount()
  })

  it('resizes the sections via the drag handle and clamps the split', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await nextTick()
    const handle = wrapper.get('[data-testid="git-split-handle"]')

    await dispatchPointer(handle.element, 'pointerdown', {
      clientY: 200,
      pointerId: 1,
    })
    expect(
      wrapper.get('[data-testid="git-changes-container"]').attributes('style'),
    ).toContain('height: 50%')

    await dispatchPointer(handle.element, 'pointermove', { clientY: 100 })
    expect(
      wrapper.get('[data-testid="git-changes-container"]').attributes('style'),
    ).toContain('height: 25%')

    // Clamped at 80%
    await dispatchPointer(handle.element, 'pointermove', { clientY: 390 })
    expect(
      wrapper.get('[data-testid="git-changes-container"]').attributes('style'),
    ).toContain('height: 80%')

    await dispatchPointer(handle.element, 'pointerup', { pointerId: 1 })
    wrapper.unmount()
  })

  it('stops polling on unmount', async () => {
    const wrapper: VueWrapper = mountPanel()
    await flushPromises()
    await nextTick()
    const store = useGitStore()
    expect(store.polling).toBe(true)
    wrapper.unmount()
    expect(store.polling).toBe(false)
  })
})
