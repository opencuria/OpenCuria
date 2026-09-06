import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import HarnessChatPanel from './HarnessChatPanel.vue'
import { useHarnessStore } from '@/stores/harness'
import { markHarnessSessionRead } from '@/services/harness.api'
import type { HarnessSession } from '@/types/harness'

vi.mock('@/services/socket', () => ({
  subscribeToWorkspace: vi.fn(),
  unsubscribeFromWorkspace: vi.fn(),
  onEvent: vi.fn(() => () => {}),
}))

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    getProviderConfig: vi.fn().mockResolvedValue({
      base_url: '',
      default_model: 'model-big',
      small_model: 'model-small',
      computer_use_model: 'model-cu',
      has_api_key: true,
      api_key_hint: '',
    }),
    listProviderModels: vi.fn().mockResolvedValue([]),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

vi.mock('@/stores/skills', () => ({
  useSkillStore: () => ({
    skills: [],
    fetchSkills: vi.fn().mockResolvedValue(undefined),
  }),
}))

const HarnessChatInputStub = {
  name: 'HarnessChatInput',
  template: '<div data-testid="harness-chat-input" />',
  props: ['disabled', 'workspaceId', 'sessionId'],
}

const stubs = {
  HarnessChatContainer: true,
  HarnessChatInput: HarnessChatInputStub,
  HarnessSheetStack: true,
}

function makeSession(overrides: Partial<HarnessSession> = {}): HarnessSession {
  return {
    id: 'session-root',
    workspace_id: 'ws-1',
    parent_id: null,
    title: 'root',
    mode: 'build',
    agent_name: 'build',
    model: 'm',
    status: 'idle',
    cost: 0,
    tokens: {},
    ...overrides,
  }
}

describe('HarnessChatPanel', () => {
  const router = createRouter({
    history: createWebHistory(),
    routes: [{ path: '/workspaces/:id', component: { template: '<div />' } }],
  })

  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    await router.push('/workspaces/ws-1')
    await router.isReady()
  })

  it('keeps input enabled when no active session', () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
        showWorkspaceToolbar: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })

    const input = wrapper.findComponent(HarnessChatInputStub)
    expect(input.exists()).toBe(true)
    expect(input.props('disabled')).toBe(false)
  })

  it('renders workspace toolbar controls inline with the input row', () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
        showWorkspaceToolbar: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })

    const buttons = wrapper.findAll('button[title]')
    const titles = buttons.map((button) => button.attributes('title'))
    expect(titles).toContain('Open file explorer')
    expect(titles).toContain('Open terminal')
    expect(titles).toContain('Open desktop')
  })

  it('hides the input and workspace toolbar when viewing a subagent session', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
        showWorkspaceToolbar: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [
      makeSession(),
      makeSession({
        id: 'session-child',
        parent_id: 'session-root',
        title: 'subtask',
        agent_name: 'explore',
      }),
    ]
    store.setActiveSession('session-child')
    await wrapper.vm.$nextTick()

    expect(wrapper.findComponent(HarnessChatInputStub).exists()).toBe(false)
    const titles = wrapper.findAll('button[title]').map((button) => button.attributes('title'))
    expect(titles).not.toContain('Open file explorer')
    expect(titles).not.toContain('Open terminal')
    expect(titles).not.toContain('Open desktop')
  })

  it('keeps the input when viewing a root session', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
        showWorkspaceToolbar: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()

    expect(wrapper.findComponent(HarnessChatInputStub).exists()).toBe(true)
  })

  it('renders the composer sheet stack above the input', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
        showWorkspaceToolbar: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-root')
    store.todosBySession['session-root'] = [
      { id: 't1', content: 'Write tests', status: 'in_progress', priority: 'high', order: 0 },
    ]
    store.handlePermissionRequired({
      request_id: 'req-1',
      session_id: 'session-root',
      workspace_id: 'ws-1',
      tool: 'bash',
      pattern: 'ls',
      title: 'Run ls',
    })
    await wrapper.vm.$nextTick()

    const stack = wrapper.findComponent({ name: 'HarnessSheetStack' })
    const input = wrapper.findComponent(HarnessChatInputStub)
    expect(stack.exists()).toBe(true)
    expect(input.exists()).toBe(true)
    expect(stack.element.parentElement).toBe(input.element.parentElement)
    const sheets = stack.props('sheets') as Array<{ kind: string }>
    expect(sheets.map((sheet) => sheet.kind)).toEqual(['permission', 'todos'])
  })

  it('marks an idle session read when it becomes the active viewing chat', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [makeSession({ unread: true })]
    store.setActiveSession('session-root')
    await flushPromises()

    expect(store.viewingSessionId).toBe('session-root')
    expect(vi.mocked(markHarnessSessionRead)).toHaveBeenCalledWith('session-root')
    expect(store.sessions[0]?.unread).toBe(false)
    wrapper.unmount()
    expect(store.viewingSessionId).toBeNull()
  })

  it('surfaces abort and error notices in the composer sheet stack', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-root')
    store.messagesBySession['session-root'] = [
      {
        id: 'msg-user',
        session_id: 'session-root',
        role: 'user',
        content: 'hello',
        parts: [],
      },
      {
        id: 'msg-abort',
        session_id: 'session-root',
        role: 'assistant',
        content: '',
        finish: 'aborted',
        error: 'aborted by user',
        parts: [],
      },
    ]
    await wrapper.vm.$nextTick()

    const stack = wrapper.findComponent({ name: 'HarnessSheetStack' })
    const sheets = stack.props('sheets') as Array<{ kind: string; notice?: { text: string } }>
    expect(sheets.map((sheet) => sheet.kind)).toContain('notice')
    expect(sheets.find((sheet) => sheet.kind === 'notice')?.notice?.text).toBe(
      'Run stopped by user',
    )

    stack.vm.$emit('dismiss-notice', 'msg-abort')
    await wrapper.vm.$nextTick()
    const after = stack.props('sheets') as Array<{ kind: string }>
    expect(after.map((sheet) => sheet.kind)).not.toContain('notice')
  })

  it('includes the processes sheet when processesOpen is true', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
        processesOpen: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const stack = wrapper.findComponent({ name: 'HarnessSheetStack' })
    const sheets = stack.props('sheets') as Array<{ kind: string }>
    expect(sheets.map((sheet) => sheet.kind)).toContain('processes')

    stack.vm.$emit('close-processes')
    expect(wrapper.emitted('close-processes')).toEqual([[]])
    wrapper.unmount()
  })

  it('syncs the session query so a subtask click is not snapped back', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [
      makeSession(),
      makeSession({
        id: 'session-child',
        parent_id: 'session-root',
        title: 'Computer use',
        agent_name: 'computeruse',
      }),
    ]
    store.setActiveSession('session-root')
    await flushPromises()
    expect(router.currentRoute.value.query.session).toBe('session-root')

    store.setActiveSession('session-child')
    await flushPromises()
    expect(router.currentRoute.value.query.session).toBe('session-child')

    store.sessions = [...store.sessions]
    await wrapper.vm.$nextTick()
    expect(store.activeSessionId).toBe('session-child')
    expect(router.currentRoute.value.query.session).toBe('session-child')
  })

  it('opens a subtask immediately even if that session is not listed yet', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-root')
    await flushPromises()

    wrapper.findComponent({ name: 'HarnessChatContainer' }).vm.$emit('open-subtask', 'session-child')
    await flushPromises()
    expect(store.activeSessionId).toBe('session-child')
  })

  it('does not snap back to the query session when the session list refreshes', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: {
        workspaceId: 'ws-1',
        canPrompt: true,
      },
      global: {
        plugins: [router],
        stubs,
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [
      makeSession(),
      makeSession({
        id: 'session-child',
        parent_id: 'session-root',
        title: 'Computer use',
        agent_name: 'computeruse',
      }),
    ]
    store.setActiveSession('session-root')
    await flushPromises()
    expect(router.currentRoute.value.query.session).toBe('session-root')

    wrapper.findComponent({ name: 'HarnessChatContainer' }).vm.$emit('open-subtask', 'session-child')
    store.sessions = [...store.sessions]
    await wrapper.vm.$nextTick()
    expect(store.activeSessionId).toBe('session-child')
  })
})
