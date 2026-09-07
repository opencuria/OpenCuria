import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import WorkspaceChatHeader from './WorkspaceChatHeader.vue'
import { WorkspaceStatus } from '@/types'
import type { WorkspaceDetail } from '@/types'

const sidebarStubs = {
  SidebarTrigger: { template: '<button data-testid="sidebar-trigger" />' },
}

const dropdownStubs = {
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: {
    inheritAttrs: false,
    template:
      '<button type="button" v-bind="$attrs" :disabled="disabled" @click="$emit(\'select\'); $emit(\'click\')"><slot /></button>',
    props: ['disabled'],
  },
  DropdownMenuSeparator: { template: '<hr />' },
}

function makeWorkspace(): WorkspaceDetail {
  return {
    id: 'ws-1',
    name: 'Alpha workspace',
    status: WorkspaceStatus.RUNNING,
    runner_online: true,
    active_operation: null,
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
  } as WorkspaceDetail
}

function baseProps(overrides: Record<string, unknown> = {}) {
  return {
    workspace: makeWorkspace(),
    activeChatTitle: 'First chat',
    transitionLabel: null,
    autoStopLabel: null,
    runnerOffline: false,
    sidePanelOpen: false,
    processesActive: false,
    runningProcessCount: 0,
    canPrompt: true,
    ...overrides,
  }
}

function mountHeader(props: Record<string, unknown> = {}) {
  return mount(WorkspaceChatHeader, {
    props: baseProps(props),
    global: { stubs: { ...sidebarStubs, ...dropdownStubs } },
  })
}

describe('WorkspaceChatHeader', () => {
  it('renders the chat name large and the workspace name as a subtle subline', () => {
    const wrapper = mountHeader()
    expect(wrapper.find('[data-testid="workspace-chat-header-chat-title"]').text()).toBe(
      'First chat',
    )
    expect(wrapper.find('[data-testid="workspace-chat-header-name"]').text()).toBe(
      'Alpha workspace',
    )
    expect(wrapper.find('[data-testid="workspace-chat-header-status"]').text()).toContain(
      'Alpha workspace',
    )
    expect(wrapper.find('[data-testid="workspace-chat-header"]').classes()).not.toContain('border-b')
  })

  it('falls back to New chat when no session title is set', () => {
    const wrapper = mountHeader({ activeChatTitle: null })
    expect(wrapper.find('[data-testid="workspace-chat-header-chat-title"]').text()).toBe('New chat')
  })

  it('renames the workspace inline', async () => {
    const wrapper = mountHeader()
    await wrapper.find('[data-testid="workspace-chat-header-name"]').trigger('click')
    const input = wrapper.find('[data-testid="workspace-chat-header-name-input"]')
    expect(input.exists()).toBe(true)
    await input.setValue('Beta workspace')
    await input.trigger('keydown.enter')
    expect(wrapper.emitted('save-workspace-name')?.[0]).toEqual(['Beta workspace'])
  })

  it('shows transition status as muted text with a dot, not badges', () => {
    const wrapper = mountHeader({ transitionLabel: 'Stopping…' })
    const status = wrapper.find('[data-testid="workspace-chat-header-status"]')
    expect(status.text()).toContain('Stopping…')
    expect(wrapper.findAllComponents({ name: 'Badge' })).toHaveLength(0)
  })

  it('shows a stop action in the overflow menu for a running workspace', async () => {
    const wrapper = mountHeader()
    const stop = wrapper.find('[data-testid="workspace-chat-header-stop"]')
    expect(stop.exists()).toBe(true)
    expect(wrapper.find('[data-testid="workspace-chat-header-more"]').exists()).toBe(true)
    await stop.trigger('click')
    expect(wrapper.emitted('stop-workspace')).toEqual([[]])
  })

  it('shows a start action in the overflow menu for a stopped workspace', async () => {
    const stopped = makeWorkspace()
    stopped.status = WorkspaceStatus.STOPPED
    const wrapper = mountHeader({ workspace: stopped })
    expect(wrapper.find('[data-testid="workspace-chat-header-stop"]').exists()).toBe(false)
    const start = wrapper.find('[data-testid="workspace-chat-header-start"]')
    expect(start.exists()).toBe(true)
    await start.trigger('click')
    expect(wrapper.emitted('start-workspace')).toEqual([[]])
  })

  it('disables the power button while a transition is running', () => {
    const wrapper = mountHeader({ transitionLabel: 'Stopping…' })
    const stop = wrapper.find('[data-testid="workspace-chat-header-stop"]')
    expect(stop.exists()).toBe(true)
    expect(stop.attributes('disabled')).toBeDefined()
    expect(wrapper.emitted('stop-workspace')).toBeUndefined()
  })
  it('emits side panel toggle, processes toggle and new chat', async () => {
    const wrapper = mountHeader()
    await wrapper.find('[data-testid="workspace-chat-header-new-chat"]').trigger('click')
    await wrapper.find('[data-testid="workspace-chat-header-toggle-side-panel"]').trigger('click')
    await wrapper.find('[data-testid="workspace-chat-header-toggle-processes"]').trigger('click')
    expect(wrapper.emitted('new-chat')).toEqual([[]])
    expect(wrapper.emitted('toggle-side-panel')).toEqual([[]])
    expect(wrapper.emitted('toggle-processes')).toEqual([[]])
  })

  it('places the side panel toggle right of the overflow menu', () => {
    const wrapper = mountHeader()
    const actions = wrapper.find('[data-testid="workspace-chat-header-toggle-side-panel"]').element
      .parentElement
    const children = Array.from(actions?.children ?? [])
    const moreIndex = children.findIndex(
      (el) => el.querySelector('[data-testid="workspace-chat-header-more"]') !== null,
    )
    const sidePanelIndex = children.findIndex(
      (el) => el.getAttribute('data-testid') === 'workspace-chat-header-toggle-side-panel',
    )
    expect(moreIndex).toBeGreaterThanOrEqual(0)
    expect(sidePanelIndex).toBe(moreIndex + 1)
  })

  it('places background processes immediately left of the overflow menu', () => {
    const wrapper = mountHeader({ runningProcessCount: 2 })
    const actions = wrapper.find('[data-testid="workspace-chat-header-toggle-processes"]').element
      .parentElement
    const children = Array.from(actions?.children ?? [])
    const processesIndex = children.findIndex(
      (el) => el.getAttribute('data-testid') === 'workspace-chat-header-toggle-processes',
    )
    const moreIndex = children.findIndex(
      (el) => el.querySelector('[data-testid="workspace-chat-header-more"]') !== null,
    )
    expect(processesIndex).toBeGreaterThanOrEqual(0)
    expect(moreIndex).toBe(processesIndex + 1)
    expect(wrapper.find('[data-testid="workspace-chat-header-toggle-processes"]').text()).toContain(
      '2',
    )
  })

  it('routes overflow actions through emits', async () => {
    const wrapper = mountHeader()
    const buttons = wrapper.findAll('button')
    const byText = (label: string) =>
      buttons.find((button) => button.text().includes(label))!

    expect(byText('Background processes')).toBeUndefined()
    await byText('Stop workspace').trigger('click')
    await byText('Capture image').trigger('click')
    await byText('Delete workspace').trigger('click')

    expect(wrapper.emitted('stop-workspace')).toEqual([[]])
    expect(wrapper.emitted('capture-image')).toEqual([[]])
    expect(wrapper.emitted('delete-workspace')).toEqual([[]])
  })
})
