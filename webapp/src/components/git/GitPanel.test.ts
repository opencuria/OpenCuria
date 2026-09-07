import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import GitPanel from './GitPanel.vue'
import { useGitStore } from '@/stores/git'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

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
    props: { workspaceId: 'ws-1' },
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
    stubPointerCapture()
    stubContainerRect()
  })

  it('renders the repo header and both sections at a 50/50 split', () => {
    const wrapper = mountPanel()

    expect(wrapper.find('[data-testid="side-panel-git"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-repo-select"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-current-branch"]').text()).toBe(
      'main',
    )
    expect(wrapper.find('[data-testid="git-ahead"]').text()).toContain('2')
    expect(wrapper.find('[data-testid="stub-changes"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stub-graph"]').exists()).toBe(true)
    expect(
      wrapper.get('[data-testid="git-changes-container"]').attributes('style'),
    ).toContain('height: 50%')
  })

  it('updates the branch label when switching repositories', async () => {
    const store = useGitStore()
    const wrapper = mountPanel()

    store.selectRepo('repo-docs')
    await nextTick()

    expect(wrapper.find('[data-testid="git-current-branch"]').text()).toBe(
      'main',
    )
    expect(wrapper.find('[data-testid="git-behind"]').text()).toContain('1')

    store.selectRepo('repo-api-client')
    await nextTick()
    expect(wrapper.text()).toContain('/workspace/api-client')
  })

  it('resizes the sections via the drag handle and clamps the split', async () => {
    const wrapper = mountPanel()
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
  })
})
