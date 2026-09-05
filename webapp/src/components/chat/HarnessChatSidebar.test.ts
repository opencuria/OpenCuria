import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessChatSidebar from './HarnessChatSidebar.vue'
import type { HarnessSession } from '@/types/harness'

function makeSession(overrides: Partial<HarnessSession> = {}): HarnessSession {
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
    ...overrides,
  }
}

const dialogStubs = {
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
  ScrollArea: { template: '<div><slot /></div>' },
}

describe('HarnessChatSidebar', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('lists root sessions and nested child sessions', () => {
    const wrapper = mount(HarnessChatSidebar, {
      props: {
        sessions: [makeSession()],
        childSessionsByParent: {
          'session-1': [
            makeSession({
              id: 'child-1',
              parent_id: 'session-1',
              title: 'Subtask chat',
            }),
          ],
        },
        activeSessionId: null,
      },
      global: { stubs: dialogStubs },
    })

    expect(wrapper.text()).toContain('First chat')
    expect(wrapper.text()).toContain('Subtask chat')
  })

  it('emits rename with session id and title', async () => {
    const wrapper = mount(HarnessChatSidebar, {
      props: {
        sessions: [makeSession()],
        childSessionsByParent: {},
        activeSessionId: 'session-1',
      },
      global: { stubs: dialogStubs },
    })

    const renameButton = wrapper.findAll('button').find((button) =>
      button.attributes('title') === undefined && button.html().includes('lucide-pencil'),
    )
    expect(renameButton).toBeTruthy()
    await renameButton!.trigger('click')

    const input = wrapper.find('input')
    await input.setValue('Renamed chat')
    await wrapper.findAll('button').find((button) => button.html().includes('lucide-check'))!.trigger('click')

    expect(wrapper.emitted('rename')?.[0]).toEqual(['session-1', 'Renamed chat'])
  })

  it('emits delete after confirmation', async () => {
    const wrapper = mount(HarnessChatSidebar, {
      props: {
        sessions: [makeSession()],
        childSessionsByParent: {},
        activeSessionId: 'session-1',
      },
      global: { stubs: dialogStubs },
    })

    const deleteButton = wrapper.findAll('button').find((button) =>
      button.html().includes('lucide-trash'),
    )
    expect(deleteButton).toBeTruthy()
    await deleteButton!.trigger('click')

    const confirm = wrapper.findAll('button').find((button) => button.text() === 'Delete')
    expect(confirm).toBeTruthy()
    await confirm!.trigger('click')

    expect(wrapper.emitted('delete')?.[0]).toEqual(['session-1'])
  })
})
