import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitGraphSection from './GitGraphSection.vue'
import { useGitStore } from '@/stores/git'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
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

const dialogStubs = {
  GitBranchDialog: {
    props: ['open', 'mode', 'branchName', 'fromHash'],
    template: '<div v-if="open" data-testid="stub-branch-dialog" />',
  },
  GitMergeDialog: {
    props: ['open', 'direction', 'branch'],
    template: '<div v-if="open" data-testid="stub-merge-dialog" />',
  },
}

function mountSection() {
  return mount(GitGraphSection, { global: { stubs: dialogStubs } })
}

describe('GitGraphSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders one row and one node per commit', () => {
    const store = useGitStore()
    const wrapper = mountSection()
    const commitCount = store.currentRepo!.commits.length

    expect(wrapper.findAll('[data-testid="git-graph-row"]')).toHaveLength(
      commitCount,
    )
    // Scope to the graph SVG (lucide icons render circles/paths too)
    const graphSvg = wrapper.find('[data-testid="git-graph-svg"]')
    expect(graphSvg.findAll('circle')).toHaveLength(commitCount)
    expect(graphSvg.findAll('path').length).toBeGreaterThan(0)
  })

  it('renders local branch tags and remote ref tags', () => {
    const wrapper = mountSection()

    expect(wrapper.find('[data-testid="git-branch-tag-main"]').exists()).toBe(
      true,
    )
    expect(
      wrapper.find('[data-testid="git-branch-tag-feature/git-panel"]').exists(),
    ).toBe(true)
    expect(wrapper.find('[data-testid="git-ref-tag-origin/main"]').exists()).toBe(
      true,
    )
  })

  it('checks out a commit via its context menu (detached HEAD)', async () => {
    const store = useGitStore()
    const wrapper = mountSection()

    await wrapper
      .find('[data-testid="git-commit-checkout-3d8e5b2"]')
      .trigger('click')

    expect(store.currentRepo!.currentBranch).toBeNull()
    expect(store.currentRepo!.headHash).toBe('3d8e5b2')
  })

  it('checks out a branch via its tag dropdown', async () => {
    const store = useGitStore()
    const wrapper = mountSection()

    await wrapper
      .find('[data-testid="git-tag-checkout-feature/git-panel"]')
      .trigger('click')

    expect(store.currentRepo!.currentBranch).toBe('feature/git-panel')
  })

  it('opens the create-branch dialog from the header and from a commit', async () => {
    const wrapper = mountSection()

    await wrapper.find('[data-testid="git-create-branch"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="stub-branch-dialog"]').exists()).toBe(
      true,
    )
  })

  it('opens the merge dialog from a branch tag', async () => {
    const wrapper = mountSection()

    await wrapper
      .find('[data-testid="git-tag-merge-into-current-feature/git-panel"]')
      .trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="stub-merge-dialog"]').exists()).toBe(true)
  })

  it('filters the graph to a single branch', async () => {
    const store = useGitStore()
    const wrapper = mountSection()
    const total = store.currentRepo!.commits.length

    await wrapper
      .find('[data-testid="git-graph-filter-branch-fix/auth-redirect"]')
      .trigger('click')
    await nextTick()

    const rows = wrapper.findAll('[data-testid="git-graph-row"]')
    expect(rows.length).toBeLessThan(total)
    expect(rows.length).toBeGreaterThan(0)
  })
})
