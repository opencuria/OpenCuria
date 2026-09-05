import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import HarnessChatPanel from './HarnessChatPanel.vue'
import { useHarnessStore } from '@/stores/harness'
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
      has_api_key: true,
      api_key_hint: '',
    }),
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
        stubs: {
          HarnessChatContainer: true,
          HarnessChatInput: HarnessChatInputStub,
          HarnessPermissionDialog: true,
        },
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
        stubs: {
          HarnessChatContainer: true,
          HarnessChatInput: HarnessChatInputStub,
          HarnessPermissionDialog: true,
        },
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
        stubs: {
          HarnessChatContainer: true,
          HarnessChatInput: HarnessChatInputStub,
          HarnessPermissionDialog: true,
        },
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
        stubs: {
          HarnessChatContainer: true,
          HarnessChatInput: HarnessChatInputStub,
          HarnessPermissionDialog: true,
        },
      },
    })
    await flushPromises()

    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-root')
    await wrapper.vm.$nextTick()

    expect(wrapper.findComponent(HarnessChatInputStub).exists()).toBe(true)
  })
})
