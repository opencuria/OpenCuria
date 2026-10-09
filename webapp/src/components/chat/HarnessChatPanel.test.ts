import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import HarnessChatPanel from './HarnessChatPanel.vue'
import { useHarnessStore } from '@/stores/harness'
import { listHarnessSessions, markHarnessSessionRead } from '@/services/harness.api'
import { armComposerTransition, clearComposerTransition } from '@/lib/composerTransition'
import type { HarnessSession } from '@/types/harness'

vi.mock('@/services/socket', () => ({
  subscribeToWorkspace: vi.fn(() => () => {}),
  unsubscribeFromWorkspace: vi.fn(),
  onEvent: vi.fn(() => () => {}),
  onReconnect: vi.fn(() => () => {}),
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
      default_effort: '',
      small_effort: '',
      computer_use_effort: '',
      has_api_key: true,
      api_key_hint: '',
    }),
    listProviderModels: vi.fn().mockResolvedValue([]),
    listRecentModels: vi.fn().mockResolvedValue([]),
    saveRecentModel: vi.fn(),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
    dismissHarnessNotice: vi.fn().mockResolvedValue(undefined),
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
  template: '<div data-testid="harness-chat-input"><div data-testid="composer-card" /></div>',
  props: [
    'disabled',
    'workspaceId',
    'sessionId',
    'uploadDrag',
    'stoppable',
    'stopOnly',
    'harnessId',
    'engineLocked',
    'mode',
    'model',
    'effort',
  ],
  emits: ['prefill', 'send', 'stop', 'update:harnessId', 'update:mode'],
  methods: {
    setPrompt(prompt: string) {
      ;(this as unknown as { $emit: (event: string, ...args: unknown[]) => void }).$emit(
        'prefill',
        prompt,
      )
    },
    uploadChatFiles(_files: File[] | FileList) {
      return Promise.resolve()
    },
  },
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
    clearComposerTransition()
    await router.push('/workspaces/ws-1')
    await router.isReady()
  })

  it('keeps input enabled when no active session', () => {
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

    const input = wrapper.findComponent(HarnessChatInputStub)
    expect(input.exists()).toBe(true)
    expect(input.props('disabled')).toBe(false)
  })

  it('morphs the composer from the armed home rect', async () => {
    const rafCallbacks: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      rafCallbacks.push(cb)
      return 1
    })
    armComposerTransition({
      x: 100,
      y: 200,
      left: 100,
      top: 200,
      right: 400,
      bottom: 280,
      width: 300,
      height: 80,
      toJSON: () => ({}),
    } as DOMRect)

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

    // jsdom reports a zero rect for the card, so the delta matches the armed rect.
    const card = wrapper.get('[data-testid="composer-card"]')
    const cardEl = card.element as HTMLElement
    expect(cardEl.style.transform).toBe('translate(100px, 200px)')
    expect(cardEl.style.width).toBe('300px')
    expect(cardEl.style.transition).toBe('none')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.chat-content-enter').exists()).toBe(true)

    // Next frame: animate to the natural position, then clean up.
    vi.useFakeTimers()
    for (const cb of rafCallbacks) cb(0)
    expect(cardEl.style.transform).toBe('')
    expect(cardEl.style.width).toBe('')
    expect(cardEl.style.transition).toContain('transform')

    vi.advanceTimersByTime(400)
    await wrapper.vm.$nextTick()
    expect(cardEl.style.transition).toBe('')
    expect(wrapper.find('.chat-content-enter').exists()).toBe(false)

    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('renders without a morph when no transition is armed', () => {
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

    const cardEl = wrapper.get('[data-testid="composer-card"]').element as HTMLElement
    expect(cardEl.style.transform).toBe('')
    expect(cardEl.style.width).toBe('')
    expect(wrapper.find('.chat-content-enter').exists()).toBe(false)
  })

  it('hides composer panel chrome (toggles live in the chat header) when viewing any session', async () => {
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
    await wrapper.vm.$nextTick()

    const titles = wrapper.findAll('button[title]').map((button) => button.attributes('title'))
    expect(titles).not.toContain('Open file explorer')
    expect(titles).not.toContain('Open terminal')
    expect(titles).not.toContain('Open desktop')
  })

  it('forwards a selected engine and its skill ids when creating a session', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: { workspaceId: 'ws-1', canPrompt: true },
      global: { plugins: [router], stubs },
    })
    await flushPromises()
    const store = useHarnessStore()
    const create = vi
      .spyOn(store, 'createSession')
      .mockResolvedValue(makeSession({ harness_id: 'claude' }))
    const input = wrapper.findComponent(HarnessChatInputStub)
    input.vm.$emit('update:harnessId', 'claude')
    input.vm.$emit('send', 'prompt', 'plan', 'opus', ['skill-1'], 'low', 'claude')
    await flushPromises()

    expect(create).toHaveBeenCalledWith(
      'ws-1',
      'prompt',
      'plan',
      'opus',
      ['skill-1'],
      'low',
      'claude',
    )
  })

  it('hydrates a Claude session fetched on load before native composer defaults can replace it', async () => {
    const listSessions = vi.mocked(listHarnessSessions)
    listSessions.mockResolvedValueOnce([
      makeSession({
        harness_id: 'claude',
        mode: 'plan',
        model: 'opus',
        reasoning_effort: 'low',
      }),
    ])
    await router.push('/workspaces/ws-1?session=session-root')
    const wrapper = mount(HarnessChatPanel, {
      props: { workspaceId: 'ws-1', canPrompt: true },
      global: { plugins: [router], stubs },
    })
    await flushPromises()
    const input = wrapper.findComponent(HarnessChatInputStub)
    expect(input.props()).toMatchObject({
      harnessId: 'claude',
      engineLocked: true,
      mode: 'plan',
      model: 'opus',
      effort: 'low',
    })
    expect(listSessions).toHaveBeenCalledTimes(1)

    const session = useHarnessStore().activeSession!
    session.status = 'busy'
    await wrapper.vm.$nextTick()
    expect(listSessions).toHaveBeenCalledTimes(1)
    expect(useHarnessStore().modelInput).toBe('opus')
  })

  it('keeps follow-up messages on their loaded session engine', async () => {
    vi.mocked(listHarnessSessions).mockResolvedValueOnce([makeSession({ harness_id: 'claude' })])
    await router.push('/workspaces/ws-1?session=session-root')
    const wrapper = mount(HarnessChatPanel, {
      props: { workspaceId: 'ws-1', canPrompt: true },
      global: { plugins: [router], stubs },
    })
    await flushPromises()
    const store = useHarnessStore()
    const send = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined)
    const create = vi.spyOn(store, 'createSession')
    wrapper
      .findComponent(HarnessChatInputStub)
      .vm.$emit('send', 'follow up', 'plan', 'sonnet', ['skill-1'], 'high', 'native')
    await flushPromises()

    expect(send).toHaveBeenCalledWith('session-root', 'follow up', {
      mode: 'plan',
      model: 'sonnet',
      skillIds: ['skill-1'],
      reasoningEffort: 'high',
    })
    expect(create).not.toHaveBeenCalled()
  })

  it('clears Claude model state when starting a new native conversation', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: { workspaceId: 'ws-1', canPrompt: true },
      global: { plugins: [router], stubs },
    })
    await flushPromises()
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ harness_id: 'claude', model: 'sonnet', reasoning_effort: 'high' }),
    ]
    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()
    expect(store.modelInput).toBe('sonnet')

    store.setActiveSession(null)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(HarnessChatInputStub).props('harnessId')).toBe('native')
    expect(store.modelInput).toBe('')
    expect(store.effortInput).toBe('')
  })

  it('restores a Claude session engine and prevents changing its fixed engine', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: { workspaceId: 'ws-1', canPrompt: true },
      global: { plugins: [router], stubs },
    })
    await flushPromises()
    const store = useHarnessStore()
    store.sessions = [makeSession({ harness_id: 'claude', model: 'sonnet' })]
    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()

    const input = wrapper.findComponent(HarnessChatInputStub)
    expect(input.props('harnessId')).toBe('claude')
    expect(input.props('engineLocked')).toBe(true)
  })

  it('hides the input when viewing a subagent session', async () => {
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
        title: 'subtask',
        agent_name: 'explore',
      }),
    ]
    store.setActiveSession('session-child')
    await wrapper.vm.$nextTick()

    expect(wrapper.findComponent(HarnessChatInputStub).exists()).toBe(false)
  })

  it('shows stop for a busy subagent and aborts that session', async () => {
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
      makeSession({ status: 'busy' }),
      makeSession({
        id: 'session-child',
        parent_id: 'session-root',
        title: 'subtask',
        agent_name: 'explore',
        status: 'busy',
      }),
    ]
    store.setActiveSession('session-child')
    await wrapper.vm.$nextTick()

    const input = wrapper.findComponent(HarnessChatInputStub)
    expect(input.exists()).toBe(true)
    expect(input.props('stopOnly')).toBe(true)
    expect(input.props('stoppable')).toBe(true)
    expect(input.props('disabled')).toBe(true)
    const stop = vi.spyOn(store, 'abortSession').mockResolvedValue(undefined)
    input.vm.$emit('stop')
    await flushPromises()
    expect(stop).toHaveBeenCalledWith('session-child')

    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()
    const rootInput = wrapper.findComponent(HarnessChatInputStub)
    expect(rootInput.props('stopOnly')).toBeFalsy()
    expect(rootInput.props('stoppable')).toBe(true)
    rootInput.vm.$emit('stop')
    await flushPromises()
    expect(stop).toHaveBeenCalledWith('session-root')
  })

  it('keeps the input when viewing a root session', async () => {
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
    await wrapper.vm.$nextTick()

    expect(wrapper.findComponent(HarnessChatInputStub).exists()).toBe(true)
  })

  it('renders the composer sheet stack above the input', async () => {
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

  it.each([
    ['aborted', 'aborted by user', 'Run stopped by user', 'info'],
    ['aborted', 'Run interrupted unexpectedly', 'Run interrupted unexpectedly', 'error'],
    ['error', 'Run interrupted unexpectedly', 'Run interrupted unexpectedly', 'error'],
    ['error', 'aborted by user', 'aborted by user', 'error'],
  ] as const)('surfaces and dismisses %s / %s notices', async (finish, error, text, tone) => {
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
        finish,
        error,
        parts: [],
      },
    ]
    await wrapper.vm.$nextTick()

    const stack = wrapper.findComponent({ name: 'HarnessSheetStack' })
    const sheets = stack.props('sheets') as Array<{
      kind: string
      notice?: { text: string; tone: string }
    }>
    expect(sheets.map((sheet) => sheet.kind)).toContain('notice')
    expect(sheets.find((sheet) => sheet.kind === 'notice')?.notice).toMatchObject({
      text,
      tone,
    })

    stack.vm.$emit('dismiss-notice', 'msg-abort')
    await wrapper.vm.$nextTick()
    await flushPromises()
    const after = stack.props('sheets') as Array<{ kind: string }>
    expect(after.map((sheet) => sheet.kind)).not.toContain('notice')
  })

  it.each(['aborted', 'error'] as const)(
    'hides server-dismissed notices and auto-clears %s on a newer user message',
    async (finish) => {
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
          notice_dismissed_at: '2026-09-15T10:00:00.000Z',
          parts: [],
        },
      ]
      await wrapper.vm.$nextTick()

      const stack = wrapper.findComponent({ name: 'HarnessSheetStack' })
      const dismissed = stack.props('sheets') as Array<{ kind: string }>
      expect(dismissed.map((sheet) => sheet.kind)).not.toContain('notice')

      // An error message before the latest user message is auto-cleared.
      store.messagesBySession['session-root'] = [
        {
          id: 'msg-user-1',
          session_id: 'session-root',
          role: 'user',
          content: 'first',
          parts: [],
        },
        {
          id: 'msg-old-error',
          session_id: 'session-root',
          role: 'assistant',
          content: '',
          finish,
          error: 'Run interrupted unexpectedly',
          parts: [],
        },
        {
          id: 'msg-user-2',
          session_id: 'session-root',
          role: 'user',
          content: 'second',
          parts: [],
        },
      ]
      await wrapper.vm.$nextTick()
      const cleared = stack.props('sheets') as Array<{ kind: string }>
      expect(cleared.map((sheet) => sheet.kind)).not.toContain('notice')
    },
  )

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

  it('remounts the transcript component when selecting a different history', async () => {
    const TranscriptProbe = {
      name: 'HarnessChatContainer',
      props: ['messages'],
      template: '<div data-testid="transcript-probe" />',
    }
    const wrapper = mount(HarnessChatPanel, {
      props: { workspaceId: 'ws-1', canPrompt: true },
      global: {
        plugins: [router],
        stubs: { ...stubs, HarnessChatContainer: TranscriptProbe },
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [
      makeSession(),
      makeSession({ id: 'session-child', parent_id: 'session-root' }),
    ]
    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()
    const firstInstance = wrapper.findComponent(TranscriptProbe).vm.$.uid

    store.setActiveSession('session-child')
    await wrapper.vm.$nextTick()
    const secondInstance = wrapper.findComponent(TranscriptProbe).vm.$.uid
    expect(secondInstance).not.toBe(firstInstance)
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

    wrapper
      .findComponent({ name: 'HarnessChatContainer' })
      .vm.$emit('open-subtask', 'session-child')
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

    wrapper
      .findComponent({ name: 'HarnessChatContainer' })
      .vm.$emit('open-subtask', 'session-child')
    store.sessions = [...store.sessions]
    await wrapper.vm.$nextTick()
    expect(store.activeSessionId).toBe('session-child')
  })

  it('disables message edit/fork actions while the active session is busy', async () => {
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
    store.sessions = [makeSession({ status: 'busy' })]
    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()

    const container = wrapper.findComponent({ name: 'HarnessChatContainer' })
    expect(container.props('disabled')).toBe(true)
  })

  it('enables message edit/fork actions on idle root sessions', async () => {
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
    store.sessions = [makeSession({ status: 'idle' })]
    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()

    const container = wrapper.findComponent({ name: 'HarnessChatContainer' })
    expect(container.props('disabled')).toBe(false)
  })

  it('keeps unread history viewable but blocks mutations when the workspace cannot prompt', async () => {
    const wrapper = mount(HarnessChatPanel, {
      props: { workspaceId: 'ws-1', canPrompt: true },
      global: { plugins: [router], stubs },
    })
    await flushPromises()
    const store = useHarnessStore()
    store.sessions = [makeSession({ unread: true })]
    store.setActiveSession('session-root')
    await flushPromises()
    await wrapper.setProps({ canPrompt: false })
    expect(vi.mocked(markHarnessSessionRead)).toHaveBeenCalledWith('session-root')
    const container = wrapper.findComponent({ name: 'HarnessChatContainer' })
    const input = wrapper.findComponent(HarnessChatInputStub)
    const stack = wrapper.findComponent({ name: 'HarnessSheetStack' })
    expect(container.props('disabled')).toBe(true)
    expect(input.props('disabled')).toBe(true)
    expect(stack.props('permissionResolving')).toBe(true)
    expect(stack.props('questionSubmitting')).toBe(true)
    const edit = vi.spyOn(store, 'editMessage').mockResolvedValue(undefined)
    const fork = vi.spyOn(store, 'forkSession').mockResolvedValue(null)
    const send = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined)
    const stop = vi.spyOn(store, 'abortSession').mockResolvedValue(undefined)
    container.vm.$emit('edit', 'user-1', 'edited')
    container.vm.$emit('fork', 'user-1')
    input.vm.$emit('send', 'blocked', 'build', 'm', [], '')
    input.vm.$emit('stop')
    await flushPromises()
    for (const action of [edit, fork, send, stop]) expect(action).not.toHaveBeenCalled()
    await wrapper.setProps({ canPrompt: true })
    expect(container.props('disabled')).toBe(false)
    expect(input.props('disabled')).toBe(false)
    wrapper.unmount()
  })

  it('forwards container edit events to the store editMessage action', async () => {
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
    store.sessions = [makeSession({ status: 'idle' })]
    store.setActiveSession('session-root')
    await flushPromises()
    const spy = vi.spyOn(store, 'editMessage').mockResolvedValue(undefined)

    wrapper.findComponent({ name: 'HarnessChatContainer' }).vm.$emit('edit', 'user-1', 'edited')
    await flushPromises()

    expect(spy).toHaveBeenCalledWith('session-root', 'user-1', 'edited')
  })

  it('forks without auto-send and prefills the composer via setPrompt', async () => {
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
    store.sessions = [makeSession({ status: 'idle' })]
    store.setActiveSession('session-root')
    await flushPromises()
    const forked = makeSession({ id: 'session-fork', title: 'root (fork #1)' })
    const spy = vi
      .spyOn(store, 'forkSession')
      .mockResolvedValue({ session: forked, prefill: 'original' })

    wrapper.findComponent({ name: 'HarnessChatContainer' }).vm.$emit('fork', 'user-1')
    await flushPromises()

    expect(spy).toHaveBeenCalledWith('session-root', 'user-1')
    // Panel must not send a follow-up prompt after forking (no auto-send).
    const sendSpy = vi.spyOn(store, 'sendMessage')
    expect(sendSpy).not.toHaveBeenCalled()
    const input = wrapper.findComponent(HarnessChatInputStub)
    expect(input.emitted('prefill')).toEqual([['original']])
  })

  it('ignores edit/fork events from subagent sessions', async () => {
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
        title: 'subtask',
        agent_name: 'explore',
      }),
    ]
    store.setActiveSession('session-child')
    await flushPromises()
    const editSpy = vi.spyOn(store, 'editMessage').mockResolvedValue(undefined)
    const forkSpy = vi.spyOn(store, 'forkSession').mockResolvedValue(null)

    const container = wrapper.findComponent({ name: 'HarnessChatContainer' })
    expect(container.props('disabled')).toBe(true)
    container.vm.$emit('edit', 'user-1', 'edited')
    container.vm.$emit('fork', 'user-1')
    await flushPromises()

    expect(editSpy).not.toHaveBeenCalled()
    expect(forkSpy).not.toHaveBeenCalled()
  })

  function makeDropEvent(files: File[]): DragEvent {
    const list = {
      length: files.length,
      item: (index: number) => files[index] ?? null,
    } as unknown as FileList & { [index: number]: File }
    for (let i = 0; i < files.length; i++) {
      list[i] = files[i]!
    }
    const event = new Event('drop', { bubbles: true, cancelable: true }) as DragEvent
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: list as FileList, types: ['Files'] },
    })
    return event
  }

  it('forwards dropped files to the chat input upload', async () => {
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

    const input = wrapper.findComponent(HarnessChatInputStub)
    const uploadSpy = vi
      .spyOn(
        input.vm as unknown as { uploadChatFiles: (files: File[] | FileList) => Promise<void> },
        'uploadChatFiles',
      )
      .mockResolvedValue(undefined)
    const zone = wrapper.find('[data-testid="harness-chat-dropzone"]')
    expect(zone.exists()).toBe(true)
    zone.element.dispatchEvent(makeDropEvent([new File(['hi'], 'drop.txt')]))
    await flushPromises()

    expect(uploadSpy).toHaveBeenCalledTimes(1)
    const forwarded = uploadSpy.mock.calls[0]![0] as File[]
    expect(forwarded).toHaveLength(1)
    expect(forwarded[0]!.name).toBe('drop.txt')
    uploadSpy.mockRestore()
  })

  it('ignores drops already handled by the composer card (no double upload)', async () => {
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

    const input = wrapper.findComponent(HarnessChatInputStub)
    const uploadSpy = vi
      .spyOn(
        input.vm as unknown as { uploadChatFiles: (files: File[] | FileList) => Promise<void> },
        'uploadChatFiles',
      )
      .mockResolvedValue(undefined)
    const zone = wrapper.find('[data-testid="harness-chat-dropzone"]')
    // The composer card calls preventDefault + stopPropagation on drops it
    // handles; such an event bubbling up must not upload a second time.
    const event = makeDropEvent([new File(['hi'], 'drop.txt')])
    event.preventDefault()
    zone.element.dispatchEvent(event)
    await flushPromises()
    expect(uploadSpy).not.toHaveBeenCalled()
    uploadSpy.mockRestore()
  })

  it('mirrors the drag state as a composer highlight prop and ignores drops when not ready', async () => {
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

    const zone = wrapper.find('[data-testid="harness-chat-dropzone"]')
    const enter = new Event('dragenter', { bubbles: true, cancelable: true }) as DragEvent
    Object.defineProperty(enter, 'dataTransfer', { value: { types: ['Files'] } })
    zone.element.dispatchEvent(enter)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(HarnessChatInputStub).props('uploadDrag')).toEqual({
      active: true,
      uploading: false,
    })

    const leave = new Event('dragleave', { bubbles: true, cancelable: true }) as DragEvent
    Object.defineProperty(leave, 'dataTransfer', { value: { types: ['Files'] } })
    zone.element.dispatchEvent(leave)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(HarnessChatInputStub).props('uploadDrag')).toEqual({
      active: false,
      uploading: false,
    })

    await wrapper.setProps({ canPrompt: false })
    const input = wrapper.findComponent(HarnessChatInputStub)
    const uploadSpy = vi
      .spyOn(
        input.vm as unknown as { uploadChatFiles: (files: File[] | FileList) => Promise<void> },
        'uploadChatFiles',
      )
      .mockResolvedValue(undefined)
    zone.element.dispatchEvent(makeDropEvent([new File(['hi'], 'drop.txt')]))
    await flushPromises()
    expect(uploadSpy).not.toHaveBeenCalled()
    uploadSpy.mockRestore()
  })

  it('routes the live turn by message id so the fresh empty turn streams (not the previous answer)', async () => {
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
    store.sessions = [makeSession({ status: 'busy' })]
    store.setActiveSession('session-root')
    store.messagesBySession['session-root'] = [
      {
        id: 'user-1',
        session_id: 'session-root',
        role: 'user',
        content: 'first',
        parts: [],
      },
      {
        id: 'assistant-old',
        session_id: 'session-root',
        role: 'assistant',
        content: 'previous reply',
        parts: [],
        completed_at: '2026-03-29T10:00:01.000Z',
      },
      {
        id: 'user-2',
        session_id: 'session-root',
        role: 'user',
        content: 'follow up',
        parts: [],
      },
      {
        id: 'assistant-fresh',
        session_id: 'session-root',
        role: 'assistant',
        content: '',
        parts: [],
      },
    ]
    await wrapper.vm.$nextTick()

    const container = wrapper.findComponent({ name: 'HarnessChatContainer' })
    expect(container.props('streamingMessageId')).toBe('assistant-fresh')
  })

  it('does not pass loading to the container while chat exists (no skeleton flash)', async () => {
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
        id: 'user-1',
        session_id: 'session-root',
        role: 'user',
        content: 'hello',
        parts: [],
      },
    ]
    // A background subagent refresh flips the shared session list fetch;
    // the store must not raise `loading` for a same-workspace refresh.
    store.loading = true
    await wrapper.vm.$nextTick()

    const container = wrapper.findComponent({ name: 'HarnessChatContainer' })
    expect(container.props('loading')).toBe(false)
  })

  it('keeps chat visible across a same-workspace subagent session refresh', async () => {
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
    store.messagesBySession['session-root'] = [
      {
        id: 'user-1',
        session_id: 'session-root',
        role: 'user',
        content: 'hello',
        parts: [],
      },
    ]
    vi.mocked(listHarnessSessions).mockResolvedValueOnce([
      makeSession(),
      makeSession({
        id: 'session-child',
        parent_id: 'session-root',
        title: 'subtask',
        agent_name: 'explore',
      }),
    ])
    await store.fetchSessions('ws-1')
    await wrapper.vm.$nextTick()

    expect(store.loading).toBe(false)
    expect(store.messagesBySession['session-root']).toHaveLength(1)
    const container = wrapper.findComponent({ name: 'HarnessChatContainer' })
    expect(container.props('loading')).toBe(false)
  })
})
