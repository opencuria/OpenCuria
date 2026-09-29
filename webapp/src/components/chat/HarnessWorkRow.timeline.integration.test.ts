import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessWorkRow from './HarnessWorkRow.vue'
import { pinHarnessPartDetailKey, requestHarnessPartDetailKey } from '@/lib/harnessPartDetail'
import { useHarnessStore } from '@/stores/harness'
import { getHarnessPart, listHarnessParts, type HarnessPartsResponse } from '@/services/harness.api'
import type { HarnessPart, HarnessSession } from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    getHarnessPart: vi.fn(),
    listHarnessParts: vi.fn(),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
  }
})

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }),
}))

const partsMock = vi.mocked(listHarnessParts)
const detailMock = vi.mocked(getHarnessPart)

function makeSession(overrides: Partial<HarnessSession> = {}): HarnessSession {
  return {
    id: 'session-timeline',
    workspace_id: 'workspace-1',
    title: 'Timeline',
    mode: 'build',
    agent_name: 'build',
    model: 'model-1',
    status: 'busy',
    cost: 0,
    tokens: {},
    ...overrides,
  }
}

describe('timeline detail hydration integration', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('opens a completed tool from a backend-faithful timeline while its session is busy', async () => {
    // The timeline endpoint (unlike the legacy parts endpoint) nests parts
    // under their message and does not repeat either parent id on each part.
    const timeline = {
      session: makeSession(),
      messages: [
        {
          id: 'assistant-message',
          session_id: 'session-timeline',
          role: 'assistant',
          content: '',
          parts: [
            {
              id: 'finished-tool',
              type: 'tool',
              state: 'completed',
              title: 'Read file',
              tool: 'read',
              output: '',
              display: { tool: 'read', summary: 'Read file' },
              detail_loaded: false,
            },
          ],
        },
      ],
    } as unknown as HarnessPartsResponse
    partsMock.mockResolvedValueOnce(timeline)
    detailMock.mockResolvedValueOnce({
      id: 'finished-tool',
      session_id: 'session-timeline',
      message_id: 'assistant-message',
      type: 'tool',
      state: 'completed',
      title: 'Read file',
      tool: 'read',
      input: { tool: 'read', arguments: '{"path":"README.md"}' },
      output: 'Full file contents',
      meta: {},
      detail_loaded: true,
    } as HarnessPart)

    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-timeline')
    // A live busy snapshot may already have created this row without its
    // REST-only message parent id; reconciliation must still leave a usable
    // envelope-derived identity on the merged part.
    store.messagesBySession['session-timeline'] = [
      {
        id: 'assistant-message',
        session_id: 'session-timeline',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'finished-tool',
            session_id: 'session-timeline',
            type: 'tool',
            state: 'completed',
            title: 'Read file',
            tool: 'read',
            output: '',
            detail_loaded: false,
          },
        ],
      },
    ]
    await store.fetchParts('session-timeline', false)

    const part = store.messagesBySession['session-timeline']?.[0]?.parts[0]
    expect(part).toMatchObject({
      id: 'finished-tool',
      session_id: 'session-timeline',
      message_id: 'assistant-message',
      detail_loaded: false,
    })

    const pinDetail = vi.fn((sessionId: string, partId: string, pinned: boolean) =>
      store.pinPartDetail(sessionId, partId, pinned),
    )
    const wrapper = mount(HarnessWorkRow, {
      props: { part: part! },
      global: {
        provide: {
          [requestHarnessPartDetailKey as symbol]: (sessionId: string, partId: string) =>
            store.fetchPartDetail(sessionId, partId),
          [pinHarnessPartDetailKey as symbol]: pinDetail,
        },
      },
    })

    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    await flushPromises()

    expect(detailMock).toHaveBeenCalledWith('session-timeline', 'finished-tool')
    expect(pinDetail).toHaveBeenCalledWith('session-timeline', 'finished-tool', true)
    expect(wrapper.get('[data-testid="tool-detail-read"]').text()).toContain('Full file contents')
    expect(store.sessions[0]?.status).toBe('busy')
  })
})
