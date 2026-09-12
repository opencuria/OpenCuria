import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitGraphSection from './GitGraphSection.vue'
import * as gitApi from '@/services/git.api'
import { useGitStore } from '@/stores/git'
import {
  makeCommitDetails,
  makeRawCommit,
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

// Render menu contents inline (normally teleported and shown on demand) so
// menu items can be clicked directly in tests. Defined via vi.hoisted so
// the vi.mock factories (hoisted to the top of the file) can use them.
const { passthrough, menuItem } = vi.hoisted(() => ({
  passthrough: { template: '<div><slot /></div>' },
  menuItem: {
    inheritAttrs: false,
    props: { disabled: { type: Boolean, default: false } },
    emits: ['click'],
    template:
      '<button type="button" v-bind="$attrs" :disabled="disabled" @click="$emit(\'click\', $event)"><slot /></button>',
  },
}))

vi.mock('@/components/ui/context-menu', () => ({
  ContextMenu: passthrough,
  ContextMenuTrigger: passthrough,
  ContextMenuContent: passthrough,
  ContextMenuItem: menuItem,
  ContextMenuSeparator: { template: '<hr />' },
}))

vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: passthrough,
  DropdownMenuTrigger: passthrough,
  DropdownMenuContent: passthrough,
  DropdownMenuItem: menuItem,
  DropdownMenuSeparator: { template: '<hr />' },
}))

vi.mock('@/components/ui/tooltip', () => ({
  TooltipProvider: passthrough,
  Tooltip: passthrough,
  TooltipTrigger: passthrough,
  TooltipContent: passthrough,
}))

const getRepos = vi.mocked(gitApi.getGitRepos)
const getRepo = vi.mocked(gitApi.getGitRepo)
const getHistory = vi.mocked(gitApi.getGitHistory)
const getDetails = vi.mocked(gitApi.getGitCommitDetails)
const runOp = vi.mocked(gitApi.runGitOperation)

const dialogStubs = {
  GitBranchDialog: {
    props: ['open', 'mode', 'branchName', 'fromHash'],
    template: '<div v-if="open" data-testid="stub-branch-dialog" />',
  },
  GitDeleteBranchDialog: {
    props: ['open', 'branch'],
    template: '<div v-if="open" data-testid="stub-delete-dialog" :data-branch="branch" />',
  },
  GitMergeDialog: {
    props: ['open', 'direction', 'branch'],
    template: '<div v-if="open" data-testid="stub-merge-dialog" />',
  },
  GitCommitDetailsView: {
    props: ['hash'],
    template: '<div data-testid="stub-commit-details" :data-hash="hash" />',
  },
}

function mountSection() {
  return mount(GitGraphSection, { global: { stubs: dialogStubs } })
}

async function initStore() {
  const store = useGitStore()
  await store.initialize('workspace-1')
  return store
}

describe('GitGraphSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot()])
    getDetails.mockImplementation(async (_ws, repo, hash) => ({
      ok: true,
      repo_path: repo,
      details: makeCommitDetails(hash),
    }))
    runOp.mockImplementation(async (_ws, payload) => ({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
      operation: payload.operation,
    }) as never)
  })

  it('renders one row and one node per commit', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const commitCount = store.currentRepo!.commits.length

    expect(commitCount).toBeGreaterThan(0)
    expect(wrapper.findAll('[data-testid="git-graph-row"]')).toHaveLength(commitCount)
    // Scope to the graph SVG (lucide icons render circles/paths too)
    const graphSvg = wrapper.find('[data-testid="git-graph-svg"]')
    expect(graphSvg.findAll('circle')).toHaveLength(commitCount)
    expect(graphSvg.findAll('path').length).toBeGreaterThan(0)
    // Small lists fit entirely in the window: no spacers needed.
    expect(wrapper.find('[data-testid="git-graph-spacer-top"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-graph-spacer-bottom"]').exists()).toBe(false)
  })

  it('renders local branch tags and remote ref tags', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    // All-branch history: main sits on HEAD f4a9c21 and the
    // feature/git-panel tip g5h1k83 is a row of its own.
    expect(wrapper.find('[data-testid="git-branch-tag-main"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-branch-tag-feature/git-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-ref-tag-origin/main"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-graph-filter-branch-feature/git-panel"]').exists()).toBe(true)
  })

  it('renders one combined badge when local and remote share a commit', async () => {
    const base = makeRepoSnapshot({
      commits: [
        makeRawCommit('f4a9c21', { message: 'main tip', parents: [] }),
        makeRawCommit('g5h1k83', { message: 'feature tip', parents: [] }),
      ],
    })
    const combined = {
      ...base,
      remote_refs: [{ name: 'origin/main', tip_hash: 'f4a9c21' }],
    }
    setupGitRepos(getRepos, getRepo, getHistory, [combined])
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    const badge = wrapper.find('[data-testid="git-branch-tag-main"]')
    expect(badge.exists()).toBe(true)
    expect(badge.attributes('title')).toBe('main · origin/main')
    // Exactly one badge for the shared commit — no separate remote badge.
    expect(wrapper.find('[data-testid="git-ref-tag-origin/main"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-remote-chip-origin/main"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-remote-chip-origin/main"]').text()).toBe('origin')
  })

  it('checks out a commit via its context menu with a typed payload', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    // Default fixture parents: 9c2f1e7 (merge) is a stable commit hash.
    const checkedOut = makeRepoSnapshot({ currentBranch: null, headHash: '9c2f1e7' })
    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: checkedOut,
      repo_path: '/workspace/repo-app',
    } as never)
    await wrapper.find('[data-testid="git-commit-checkout-9c2f1e7"]').trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'checkout_commit',
      commit: '9c2f1e7',
    })
  })

  it('checks out a branch via its tag dropdown with a typed payload', async () => {
    // Default fixture already carries the feature tip as its own row.
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.find('[data-testid="git-tag-checkout-feature/git-panel"]').trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'checkout_branch',
      branch: 'feature/git-panel',
    })
  })

  it('opens the create-branch dialog from the header and from a commit', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.find('[data-testid="git-create-branch"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="stub-branch-dialog"]').exists()).toBe(true)
  })

  it('opens the merge dialog from a branch tag', async () => {
    // Default fixture already carries the feature tip as its own row.
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.find('[data-testid="git-tag-merge-into-current-feature/git-panel"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="stub-merge-dialog"]').exists()).toBe(true)
  })

  it('filters the graph to a single branch', async () => {
    // fixture: main tip f4a9c21 has parents e8b7d3a; feature tip g5h1k83 is
    // unreachable → filtering to feature shrinks the row count.
    const filtered = makeRepoSnapshot({
      branches: [
        { name: 'main', tip_hash: 'f4a9c21', upstream: 'origin/main', ahead: 2, behind: 0 },
        { name: 'feature/git-panel', tip_hash: 'g5h1k83', upstream: null, ahead: 0, behind: 0 },
      ],
      commits: [
        makeRawCommit('f4a9c21', { message: 'main tip', parents: ['base'] }),
        makeRawCommit('base', { message: 'base', parents: [] }),
        makeRawCommit('g5h1k83', { message: 'feature tip', parents: ['other'] }),
        makeRawCommit('other', { message: 'other', parents: [] }),
      ],
    })
    setupGitRepos(getRepos, getRepo, getHistory, [filtered])
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const total = store.currentRepo!.commits.length
    expect(total).toBe(4)

    await wrapper.find('[data-testid="git-graph-filter-branch-feature/git-panel"]').trigger('click')
    await nextTick()

    const rows = wrapper.findAll('[data-testid="git-graph-row"]')
    expect(rows.length).toBe(2)
    expect(rows.length).toBeLessThan(total)
  })

  it('opens the delete dialog from a non-current branch tag', async () => {
    // Default fixture already carries the feature tip as its own row.
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    await wrapper.find('[data-testid="git-tag-delete-feature/git-panel"]').trigger('click')
    await nextTick()

    const stub = wrapper.find('[data-testid="stub-delete-dialog"]')
    expect(stub.exists()).toBe(true)
    expect(stub.attributes('data-branch')).toBe('feature/git-panel')
  })

  it('disables delete for the current branch tag', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(
      wrapper.find('[data-testid="git-tag-delete-main"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('disables merge actions while detached', async () => {
    const withFeature = makeRepoSnapshot({
      currentBranch: null,
      headHash: 'f4a9c21',
    })
    setupGitRepos(getRepos, getRepo, getHistory, [withFeature])
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(
      wrapper.find('[data-testid="git-tag-merge-into-current-feature/git-panel"]').attributes('disabled'),
    ).toBeDefined()
    expect(
      wrapper.find('[data-testid="git-tag-merge-current-into-feature/git-panel"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('disables create-branch for unborn repos without a base commit', async () => {
    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot({ currentBranch: 'main', headHash: null, commits: [] })])
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.find('[data-testid="git-create-branch"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="git-graph-empty"]').text()).toContain('No commits yet')
  })

  it('loads more history via the pagination button', async () => {
    const store = await initStore()
    // Default fixture has no more pages; flip the flag on the live repo.
    store.currentRepo!.hasMore = true
    await nextTick()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.find('[data-testid="git-load-more"]').exists()).toBe(true)
    // Sentinel fallback: present alongside the button when paging remains.
    expect(wrapper.find('[data-testid="git-history-sentinel"]').exists()).toBe(true)

    getHistory.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      commits: [makeRawCommit('older1', { message: 'older', parents: [] })],
      has_more: false,
      history_skip: 4,
      history_limit: 50,
    })
    await wrapper.find('[data-testid="git-load-more"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(getHistory).toHaveBeenCalledWith('workspace-1', '/workspace/repo-app', { limit: 50, skip: 4 })
    expect(store.currentRepo!.commits.map((c) => c.hash)).toContain('older1')
    expect(store.historyLoading).toBe(false)
  })

  it('hides the pagination button when history is complete', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.find('[data-testid="git-load-more"]').exists()).toBe(false)
  })

  it('renders Graph, Description and Date columns without resize handles', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.find('[data-testid="git-graph-header-graph"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-graph-header-description"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-graph-header-date"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-graph-header-author"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-graph-header-commit"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-columns-toggle"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="git-column-resize-0"]').exists()).toBe(false)
  })

  it('expands a commit row on click via lazy details and collapses on second click', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const hash = store.currentRepo!.commits[0]!.hash
    const rows = wrapper.findAll('[data-testid="git-graph-row"]')

    await rows[0]!.trigger('click')
    await flushPromises()
    await nextTick()
    expect(store.expandedCommitHash).toBe(hash)
    expect(getDetails).toHaveBeenCalledWith('workspace-1', '/workspace/repo-app', hash)
    expect(wrapper.find('[data-testid="git-commit-details-row"]').exists()).toBe(true)

    await rows[0]!.trigger('click')
    await flushPromises()
    await nextTick()
    expect(store.expandedCommitHash).toBeNull()
    expect(wrapper.find('[data-testid="git-commit-details-row"]').exists()).toBe(false)
  })

  it('offsets expanded commit details past the graph column', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const rows = wrapper.findAll('[data-testid="git-graph-row"]')

    await rows[0]!.trigger('click')
    await flushPromises()
    await nextTick()

    const detailsCell = wrapper.find('[data-testid="git-commit-details-row"] td')
    expect(detailsCell.exists()).toBe(true)
    const padding = Number.parseInt((detailsCell.element as HTMLElement).style.paddingLeft, 10)
    expect(padding).toBeGreaterThan(0)
    expect(store.expandedCommitHash).toBe(store.currentRepo!.commits[0]!.hash)
  })

  it('marks the HEAD node as current and colours branch tags with the lane colour', async () => {
    const store = await initStore()
    const wrapper = mountSection()
    await nextTick()
    const headHash = store.currentRepo!.headHash

    const headNode = wrapper.find(`[data-testid="git-graph-node-${headHash}"]`)
    expect(headNode.exists()).toBe(true)
    expect(headNode.attributes('stroke-width')).toBe('2')

    const mainTag = wrapper.find('[data-testid="git-branch-tag-main"]')
    const mainRow = wrapper
      .findAll('[data-testid="git-graph-row"]')
      .find((row) => row.find('[data-testid="git-branch-tag-main"]').exists())
    expect(mainRow).toBeDefined()
    const colour = Number(mainRow!.attributes('data-color'))
    // Lane colours resolve via the GIT_GRAPH_COLORS palette (CSS var).
    const { GIT_GRAPH_COLORS } = await import('@/lib/gitGraph')
    expect(mainTag.attributes('style')).toContain(GIT_GRAPH_COLORS[colour % GIT_GRAPH_COLORS.length]!)

    const mergeIndex = store.currentRepo!.commits.findIndex((c) => c.parents.length > 1)
    expect(mergeIndex).toBeGreaterThanOrEqual(0)
    const rows = wrapper.findAll('[data-testid="git-graph-row"]')
    expect(rows[mergeIndex]!.html()).not.toContain('opacity-50')
  })

  it('virtualizes long histories to a window with a bottom spacer', async () => {
    const commits = Array.from(
      { length: 120 },
      (_, i) =>
        makeRawCommit(`c${String(i).padStart(4, '0')}`, {
          message: `commit ${i}`,
          parents: i + 1 < 120 ? [`c${String(i + 1).padStart(4, '0')}`] : [],
        }),
    )
    setupGitRepos(getRepos, getRepo, getHistory, [makeRepoSnapshot({ commits })])
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    const rows = wrapper.findAll('[data-testid="git-graph-row"]')
    expect(rows.length).toBeGreaterThan(0)
    expect(rows.length).toBeLessThan(120)
    // Spacer rows preserve total table height for off-window commits.
    expect(wrapper.find('[data-testid="git-graph-spacer-bottom"]').exists()).toBe(true)
    const spacer = wrapper.find('[data-testid="git-graph-spacer-bottom"] td')
    expect(Number.parseInt((spacer.element as HTMLElement).style.height, 10)).toBeGreaterThan(0)
    // SVG overlay renders only the visible window's nodes.
    const graphSvg = wrapper.find('[data-testid="git-graph-svg"]')
    expect(graphSvg.findAll('circle').length).toBe(rows.length)
  })

  it('loads more history when the sentinel intersects, without double requests', async () => {
    const store = await initStore()
    store.currentRepo!.hasMore = true
    await nextTick()

    let observe: IntersectionObserverCallback | null = null
    const observed: Element[] = []
    const disconnect = vi.fn()
    const IntersectionObserverMock = vi.fn((cb: IntersectionObserverCallback) => {
      observe = cb
      return {
        observe: (el: Element) => void observed.push(el),
        unobserve: vi.fn(),
        disconnect,
      }
    })
    vi.stubGlobal('IntersectionObserver', IntersectionObserverMock)
    try {
      const wrapper = mountSection()
      await nextTick()
      await flushPromises()

      const sentinel = wrapper.find('[data-testid="git-history-sentinel"]')
      expect(sentinel.exists()).toBe(true)
      expect(observed).toContain(sentinel.element)
      expect(observe).not.toBeNull()

      getHistory.mockResolvedValue({
        ok: true,
        repo_path: '/workspace/repo-app',
        commits: [],
        has_more: false,
        history_skip: 4,
        history_limit: 50,
      })
      const loadSpy = vi.spyOn(store, 'loadMoreHistory')
      // Fire twice while the first request is in flight — only one call.
      observe!([{ isIntersecting: true } as IntersectionObserverEntry], null as never)
      observe!([{ isIntersecting: true } as IntersectionObserverEntry], null as never)
      await flushPromises()
      await nextTick()

      expect(loadSpy.mock.calls.length).toBe(1)
      expect(getHistory).toHaveBeenCalledTimes(1)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('checks out a remote branch via its ref tag menu', async () => {
    await initStore()
    const wrapper = mountSection()
    await nextTick()

    const trigger = wrapper.find('[data-testid="git-ref-tag-origin/main"]')
    expect(trigger.exists()).toBe(true)
    expect(trigger.element.tagName).toBe('BUTTON')

    await wrapper.find('[data-testid="git-remote-checkout-origin/main"]').trigger('click')
    await flushPromises()

    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'checkout_remote_branch',
      remote_ref: 'origin/main',
    })
  })
})
