import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import ChatNavbar from './ChatNavbar.vue'
import type { HarnessSession } from '@/types/harness'
import { WorkspaceStatus } from '@/types'
import type { WorkspaceDetail } from '@/types'

vi.mock('vue-router', () => ({
  RouterLink: {
    name: 'RouterLink',
    props: ['to'],
    template: '<a :data-to="JSON.stringify(to)"><slot /></a>',
  },
}))

const dropdownStubs = {
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: {
    template: '<button type="button" @click="$emit(\'select\'); $emit(\'click\')"><slot /></button>',
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

function makeSession(): HarnessSession {
  return {
    id: 'session-1',
    workspace_id: 'ws-1',
    parent_id: null,
    title: 'First chat',
    mode: 'build',
    agent_name: 'build',
    model: '',
    status: 'idle',
    cost: 0,
    tokens: {},
    updated_at: '2026-03-29T10:00:00.000Z',
  }
}

function baseProps(overrides: Record<string, unknown> = {}) {
  return {
    workspace: makeWorkspace(),
    activeSession: makeSession(),
    hasHarnessChats: true,
    transitionLabel: null,
    autoStopLabel: null,
    runnerOffline: false,
    fileExplorerOpen: false,
    terminalOpen: false,
    terminalMinimized: false,
    desktopOpen: false,
    desktopMinimized: false,
    processesActive: false,
    runningProcessCount: 0,
    canPrompt: true,
    renamingSession: false,
    ...overrides,
  }
}

function mountNavbar(props: Record<string, unknown> = {}) {
  setActivePinia(createPinia())
  return mount(ChatNavbar, {
    props: baseProps(props),
    global: { stubs: dropdownStubs },
  })
}

describe('ChatNavbar', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the active chat title with fallback to workspace pill', () => {
    const wrapper = mountNavbar()
    expect(wrapper.find('[data-testid="chat-navbar-title"]').text()).toBe('First chat')
    expect(wrapper.find('[data-testid="chat-navbar-workspace-pill"]').text()).toContain(
      'Alpha workspace',
    )
  })

  it('falls back to "New chat" without an active session', () => {
    const wrapper = mountNavbar({ activeSession: null })
    expect(wrapper.find('[data-testid="chat-navbar-title"]').text()).toBe('New chat')
    expect(wrapper.find('[data-testid="chat-navbar-rename-chat"]').exists()).toBe(false)
  })

  it('links the workspace pill to the workspace without session query', () => {
    const wrapper = mountNavbar()
    const pill = wrapper.find('[data-testid="chat-navbar-workspace-pill"]')
    expect(pill.attributes('data-to')).toBe(JSON.stringify('/workspaces/ws-1'))
  })

  it('emits back on the back button', async () => {
    const wrapper = mountNavbar()
    await wrapper.find('[data-testid="chat-navbar-back"]').trigger('click')
    expect(wrapper.emitted('back')).toEqual([[]])
  })

  it('renames the chat inline', async () => {
    const wrapper = mountNavbar()
    await wrapper.find('[data-testid="chat-navbar-rename-chat"]').trigger('click')
    const input = wrapper.find('[data-testid="chat-navbar-title-input"]')
    expect(input.exists()).toBe(true)
    await input.setValue('Renamed chat')
    await input.trigger('keydown.enter')
    expect(wrapper.emitted('rename-session')?.[0]).toEqual(['session-1', 'Renamed chat'])
  })

  it('emits panel toggles', async () => {
    const wrapper = mountNavbar()
    await wrapper.find('[data-testid="chat-navbar-toggle-files"]').trigger('click')
    await wrapper.find('[data-testid="chat-navbar-toggle-terminal"]').trigger('click')
    await wrapper.find('[data-testid="chat-navbar-toggle-desktop"]').trigger('click')
    expect(wrapper.emitted('toggle-files')).toEqual([[]])
    expect(wrapper.emitted('toggle-terminal')).toEqual([[]])
    expect(wrapper.emitted('toggle-desktop')).toEqual([[]])
  })

  it('shows transition status as muted text with a dot, not badges', () => {
    const wrapper = mountNavbar({ transitionLabel: 'Stopping…' })
    const status = wrapper.find('[data-testid="chat-navbar-status"]')
    expect(status.text()).toContain('Stopping…')
    expect(wrapper.findAllComponents({ name: 'Badge' })).toHaveLength(0)
  })

  it('routes overflow actions through emits', async () => {
    const wrapper = mountNavbar({ runningProcessCount: 2 })
    const buttons = wrapper.findAll('button')
    const byText = (label: string) =>
      buttons.find((button) => button.text().includes(label))!

    await byText('Background processes').trigger('click')
    await byText('Capture image').trigger('click')
    await byText('Delete workspace').trigger('click')

    expect(wrapper.emitted('toggle-processes')).toEqual([[]])
    expect(wrapper.emitted('capture-image')).toEqual([[]])
    expect(wrapper.emitted('delete-workspace')).toEqual([[]])
    expect(wrapper.text()).toContain('2')
  })

  it('saves the workspace name from the overflow menu', async () => {
    const wrapper = mountNavbar()
    const buttons = wrapper.findAll('button')
    const rename = buttons.find((button) => button.text().includes('Rename workspace'))!
    await rename.trigger('click')

    const input = wrapper.find('[data-testid="chat-navbar-workspace-name-input"]')
    expect(input.exists()).toBe(true)
    await input.setValue('Beta workspace')
    await input.trigger('keydown.enter')
    expect(wrapper.emitted('save-workspace-name')?.[0]).toEqual(['Beta workspace'])
  })
})
