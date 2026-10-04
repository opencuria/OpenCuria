import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, afterEach, describe, expect, it } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { useWorkspaceStore } from '@/stores/workspaces'
import ConversationWorkspaceList from './ConversationWorkspaceList.vue'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import type { HarnessConversation } from '@/types/harness'

enableAutoUnmount(afterEach)
beforeEach(() => setActivePinia(createPinia()))

const stubs = {
  Tooltip: { template: '<div><slot /></div>' },
  TooltipContent: true,
  TooltipTrigger: { template: '<div><slot /></div>' },
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: {
    props: ['disabled'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
  },
}

function workspace(id: string, overrides: Partial<Workspace> = {}): Workspace {
  return {
    id,
    name: id,
    runner_id: 'runner',
    status: WorkspaceStatus.RUNNING,
    runner_online: true,
    active_operation: null,
    has_active_session: false,
    runtime_type: 'docker',
    qemu_vcpus: null,
    qemu_memory_mb: null,
    qemu_disk_size_gb: null,
    desktop_width: 1920,
    desktop_height: 1080,
    created_by_id: 1,
    created_at: '2026-10-02T10:00:00Z',
    updated_at: '2026-10-02T10:00:00Z',
    last_activity_at: '2026-10-02T10:00:00Z',
    auto_stop_timeout_minutes: null,
    auto_stop_at: null,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
    credential_ids: [],
    plugin_ids: [],
    credentials_present: false,
    ...overrides,
  }
}

function chats(workspaceId: string, count: number): HarnessConversation[] {
  return Array.from({ length: count }, (_, index) => ({
    session_id: `${workspaceId}-${index}`,
    workspace_id: workspaceId,
    workspace_name: workspaceId,
    title: `${workspaceId} chat ${index}`,
    status: 'idle',
    mode: 'build',
    agent_name: 'build',
    model: '',
    unread: false,
    updated_at: '2026-10-02T10:00:00Z',
    last_message_at: new Date(Date.parse('2026-10-02T10:00:00Z') - index * 60_000).toISOString(),
  }))
}

function mountList(
  overrides: Partial<InstanceType<typeof ConversationWorkspaceList>['$props']> = {},
) {
  return mount(ConversationWorkspaceList, {
    props: {
      workspaces: [workspace('Alpha'), workspace('Beta')],
      conversations: [...chats('Alpha', 10), ...chats('Beta', 5)],
      totalCount: 2,
      activeSessionId: null,
      activeWorkspaceId: null,
      ...overrides,
    },
    global: { stubs },
  })
}

const groupSelector = (id: string) =>
  `[data-testid="workspace-conversation-group"][data-workspace-id="${id}"]`
const rows = '[data-testid="conversation-row"]'
const more = '[data-testid="show-more-chats"]'

describe('ConversationWorkspaceList', () => {
  it('shows four newest chats per workspace without a global cap', async () => {
    const wrapper = mountList()
    const alpha = wrapper.get(groupSelector('Alpha'))
    const beta = wrapper.get(groupSelector('Beta'))
    expect(alpha.findAll(rows)).toHaveLength(4)
    expect(beta.findAll(rows)).toHaveLength(4)
    expect(alpha.findAll(rows).map((row) => row.attributes('aria-label'))).toEqual([
      'Open chat Alpha chat 0',
      'Open chat Alpha chat 1',
      'Open chat Alpha chat 2',
      'Open chat Alpha chat 3',
    ])
    await alpha.get(more).trigger('click')
    expect(alpha.findAll(rows)).toHaveLength(8)
    expect(beta.findAll(rows)).toHaveLength(4)
    expect(alpha.get(more).text()).toBe('Show 2 more chats')
    await alpha.get(more).trigger('click')
    expect(alpha.findAll(rows)).toHaveLength(10)
    expect(alpha.find(more).exists()).toBe(false)
    expect(beta.get(more).text()).toBe('Show 1 more chat')
    await beta.get(more).trigger('click')
    expect(beta.findAll(rows)).toHaveLength(5)
  })

  it('has no show-more button with at most four chats', () => {
    const wrapper = mountList({ conversations: [...chats('Alpha', 4), ...chats('Beta', 1)] })
    expect(wrapper.find(more).exists()).toBe(false)
  })

  it('keeps expansion with the workspace across polling and runner-status reordering', async () => {
    const wrapper = mountList()
    await wrapper.get(groupSelector('Alpha')).get(more).trigger('click')
    await wrapper.setProps({
      workspaces: [workspace('Beta'), workspace('Alpha', { runner_online: false })],
      conversations: [...chats('Beta', 5), ...chats('Alpha', 10)],
    })
    expect(
      wrapper
        .findAll('[data-testid="workspace-conversation-group"]')
        .map((group) => group.attributes('data-workspace-id')),
    ).toEqual(['Beta', 'Alpha'])
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(8)
    expect(wrapper.get(groupSelector('Beta')).findAll(rows)).toHaveLength(4)
    expect(wrapper.get(groupSelector('Alpha')).attributes('data-online')).toBe('false')
    await wrapper.setProps({ workspaces: [workspace('Alpha'), workspace('Beta')] })
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(8)
  })

  it('automatically reveals an older directly opened chat and marks it selected', async () => {
    const wrapper = mountList({ activeSessionId: 'Alpha-8', activeWorkspaceId: 'Alpha' })
    await nextTick()
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(10)
    expect(wrapper.get('[aria-label="Open chat Alpha chat 8"]').attributes('aria-selected')).toBe(
      'true',
    )
    expect(
      wrapper
        .get('[aria-label="Open workspace Alpha"]')
        .element.parentElement!.classList.contains('bg-primary/10'),
    ).toBe(true)
    expect(wrapper.get(groupSelector('Beta')).findAll(rows)).toHaveLength(4)
    await wrapper.setProps({ activeSessionId: 'Beta-4', activeWorkspaceId: 'Beta' })
    expect(wrapper.get(groupSelector('Beta')).findAll(rows)).toHaveLength(5)
  })

  it('reveals the selected chat when history arrives after the route', async () => {
    const wrapper = mountList({ conversations: [], activeSessionId: 'Alpha-6' })
    await wrapper.setProps({ conversations: chats('Alpha', 10) })
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(8)
    expect(wrapper.get('[aria-label="Open chat Alpha chat 6"]').attributes('aria-selected')).toBe(
      'true',
    )
  })

  it('reveals the selected chat if newer messages move it beyond the visible page', async () => {
    const history = chats('Alpha', 10)
    const wrapper = mountList({ conversations: history, activeSessionId: 'Alpha-3' })
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(4)
    await wrapper.setProps({
      conversations: [
        ...history,
        ...chats('Alpha', 5).map((row, index) => ({
          ...row,
          session_id: `new-${index}`,
          title: `New chat ${index}`,
          last_message_at: '2026-10-02T11:00:00Z',
        })),
      ],
    })
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(12)
    expect(wrapper.get('[aria-label="Open chat Alpha chat 3"]').attributes('aria-selected')).toBe(
      'true',
    )
  })

  it('resets expansion on remount instead of persisting it', async () => {
    const wrapper = mountList()
    await wrapper.get(groupSelector('Alpha')).get(more).trigger('click')
    wrapper.unmount()
    const reloaded = mountList()
    expect(reloaded.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(4)
  })

  it('restores pagination from the sidebar-owned model when the mobile drawer remounts', async () => {
    const wrapper = mountList({ visibleCounts: { Alpha: 8, Beta: 4 } })
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(8)
    await wrapper.get(groupSelector('Alpha')).get(more).trigger('click')
    expect(wrapper.emitted('update:visibleCounts')?.[0]).toEqual([{ Alpha: 12, Beta: 4 }])
    wrapper.unmount()
    const reopened = mountList({ visibleCounts: { Alpha: 12, Beta: 4 } })
    expect(reopened.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(10)
    expect(reopened.get(groupSelector('Beta')).findAll(rows)).toHaveLength(4)
  })

  it('keeps busy/unread indicators without prioritizing those chats', () => {
    const history = chats('Alpha', 5)
    history[1]!.status = 'busy'
    history[2]!.unread = true
    history[4]!.status = 'busy'
    const wrapper = mountList({ conversations: history })
    const alpha = wrapper.get(groupSelector('Alpha'))
    expect(alpha.findAll(rows)[0]!.attributes('aria-label')).toBe('Open chat Alpha chat 0')
    expect(alpha.findAll('[data-testid="busy-spinner"]')).toHaveLength(1)
    expect(alpha.findAll('[data-testid="unread-dot"]')).toHaveLength(1)
    expect(alpha.find('[data-testid="workspace-busy"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="mark-all-read"]').exists()).toBe(true)
  })

  it('shows only empty running workspaces and navigation', async () => {
    const wrapper = mountList({
      conversations: [],
      totalCount: 3,
      workspaces: [
        workspace('Alpha'),
        workspace('Booting', {
          status: WorkspaceStatus.CREATING,
          active_operation: WorkspaceOperation.CREATING,
        }),
        workspace('Stopped', { status: WorkspaceStatus.STOPPED }),
      ],
    })
    expect(wrapper.text()).toContain('No chats yet')
    expect(wrapper.findAll('[data-testid="workspace-conversation-group"]')).toHaveLength(1)
    expect(wrapper.find(groupSelector('Stopped')).exists()).toBe(false)
    expect(wrapper.get('[data-testid="all-workspaces"]').text()).toBe('All workspaces (3)')
    expect(wrapper.find(groupSelector('Booting')).exists()).toBe(false)
    await wrapper.get('[data-testid="workspaces-create"]').trigger('click')
    await wrapper.get('[data-testid="all-workspaces"]').trigger('click')
    expect(wrapper.emitted('create')).toHaveLength(1)
    expect(wrapper.emitted('open-all')).toHaveLength(1)
    expect(wrapper.find('[data-testid="mark-all-read"]').exists()).toBe(false)
  })

  it('does not show history without a known running workspace', () => {
    const wrapper = mountList({ workspaces: [], conversations: chats('Alpha', 1) })
    expect(wrapper.find(groupSelector('Alpha')).exists()).toBe(false)
    expect(wrapper.text()).toContain('No running workspaces')
  })

  it('replaces the status dot with an accessible arrow and forwards collapse separately from navigation', async () => {
    const wrapper = mountList()
    const toggle = wrapper
      .get(groupSelector('Alpha'))
      .get('[data-testid="workspace-collapse-toggle"]')
    await flushPromises()
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('[data-testid="workspace-status"]').exists()).toBe(false)
    await toggle.trigger('click')
    expect(wrapper.emitted('set-collapsed')?.[0]).toEqual(['Alpha', true])
    expect(wrapper.emitted('open')).toBeUndefined()
    await wrapper.setProps({ collapsedWorkspaceIds: ['Alpha'] })
    await flushPromises()
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBeTruthy()
    expect(wrapper.find(`[id="${toggle.attributes('aria-controls')}"]`).exists()).toBe(true)
    expect(toggle.attributes('aria-label')).toBe('Expand workspace Alpha')
    expect(wrapper.get(groupSelector('Alpha')).find(rows).exists()).toBe(false)
    expect(wrapper.get(groupSelector('Beta')).findAll(rows)).toHaveLength(4)
    await wrapper.get('[aria-label="Open workspace Alpha"]').trigger('click')
    expect(wrapper.emitted('open')?.[0]).toEqual(['Alpha'])
  })

  it('honors explicit collapse for the selected older session and retains pagination on reopen', async () => {
    const wrapper = mountList({
      collapsedWorkspaceIds: ['Alpha'],
      activeSessionId: 'Alpha-8',
      activeWorkspaceId: 'Alpha',
    })
    await nextTick()
    expect(wrapper.get(groupSelector('Alpha')).find(rows).exists()).toBe(false)
    await wrapper.setProps({ collapsedWorkspaceIds: [] })
    await flushPromises()
    expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(10)
    expect(wrapper.get('[aria-label="Open chat Alpha chat 8"]').attributes('aria-selected')).toBe(
      'true',
    )
    await wrapper.setProps({
      collapsedWorkspaceIds: ['Alpha'],
      workspaces: [workspace('Beta'), workspace('Alpha', { runner_online: false })],
    })
    await flushPromises()
    expect(wrapper.get(groupSelector('Alpha')).find(rows).exists()).toBe(false)
  })

  it('shows stopped history only in All workspaces, without unread controls in the sidebar', () => {
    const history = chats('Stopped', 2)
    history[0]!.unread = true
    const wrapper = mountList({
      workspaces: [workspace('Stopped', { status: WorkspaceStatus.STOPPED })],
      conversations: history,
    })
    expect(wrapper.find('[data-testid="workspace-conversation-group"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="mark-all-read"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('No running workspaces')
  })

  it.each([
    { active_operation: WorkspaceOperation.CAPTURING_IMAGE },
    { intervention_required: true },
  ])('disables only transitioning workspace chat mutations: %j', async (transition) => {
    const workspaces = [workspace('Alpha', transition), workspace('Beta')]
    useWorkspaceStore().workspaces = workspaces
    const wrapper = mountList({
      workspaces,
      conversations: [...chats('Alpha', 1), ...chats('Beta', 1)],
    })
    for (const id of ['Alpha', 'Beta']) {
      const group = wrapper.get(groupSelector(id))
      for (const label of ['Rename', 'Delete']) {
        const action = group.findAll('button').find((button) => button.text() === label)!
        expect(action.attributes('disabled') !== undefined).toBe(id === 'Alpha')
      }
    }
    await wrapper.get(groupSelector('Alpha')).get(rows).trigger('click')
    expect(wrapper.emitted('select')?.[0]?.[0]).toMatchObject({ session_id: 'Alpha-0' })
  })

  it('preserves collapse and pagination while capture starts and finishes', async () => {
    const store = useWorkspaceStore()
    store.workspaces = [workspace('Alpha'), workspace('Beta')]
    const wrapper = mountList()
    await wrapper.get(groupSelector('Alpha')).get(more).trigger('click')
    await wrapper.setProps({ collapsedWorkspaceIds: ['Alpha'] })
    store.updateWorkspaceOperation('Alpha', WorkspaceOperation.CAPTURING_IMAGE)
    await flushPromises()
    const alpha = wrapper.get(groupSelector('Alpha'))
    expect(alpha.find(rows).exists()).toBe(false)
    expect(alpha.find('[data-testid="workspace-busy"]').exists()).toBe(true)
    expect(alpha.get('[data-testid="workspace-row"]').attributes('title')).toBe('Alpha — Capturing')
    await alpha.get('[data-testid="workspace-row"]').trigger('click')
    expect(wrapper.emitted('open')?.[0]).toEqual(['Alpha'])
    await wrapper.setProps({ collapsedWorkspaceIds: [] })
    await flushPromises()
    expect(alpha.findAll(rows)).toHaveLength(8)
    expect(
      alpha
        .findAll('button')
        .find((button) => button.text() === 'Rename')!
        .attributes('disabled'),
    ).toBeDefined()
    store.updateWorkspaceOperation('Alpha', null)
    await flushPromises()
    expect(alpha.find('[data-testid="workspace-busy"]').exists()).toBe(false)
    expect(alpha.findAll(rows)).toHaveLength(8)
    expect(
      alpha
        .findAll('button')
        .find((button) => button.text() === 'Rename')!
        .attributes('disabled'),
    ).toBeUndefined()
  })

  it.each([
    { active_operation: WorkspaceOperation.CAPTURING_IMAGE },
    { intervention_required: true },
  ])(
    'retains collapsed paginated history through stopped reservation and clears it afterwards: %j',
    async (reservation) => {
      const store = useWorkspaceStore()
      store.workspaces = [workspace('Alpha'), workspace('Beta')]
      const wrapper = mountList()
      await wrapper.get(groupSelector('Alpha')).get(more).trigger('click')
      await wrapper.setProps({ collapsedWorkspaceIds: ['Alpha'] })
      const reserved = workspace('Alpha', { status: WorkspaceStatus.STOPPED, ...reservation })
      store.workspaces = [reserved, workspace('Beta')]
      await wrapper.setProps({ workspaces: store.workspaces })
      await flushPromises()
      const alpha = wrapper.get(groupSelector('Alpha'))
      expect(alpha.find(rows).exists()).toBe(false)
      expect(
        alpha.get('[data-testid="workspace-collapse-toggle"]').attributes('aria-expanded'),
      ).toBe('false')
      expect(alpha.find('[data-testid="workspace-busy"]').exists()).toBe(true)
      await wrapper.setProps({ collapsedWorkspaceIds: [] })
      await flushPromises()
      expect(alpha.findAll(rows)).toHaveLength(8)
      expect(
        alpha
          .findAll('button')
          .find((button) => button.text() === 'Rename')!
          .attributes('disabled'),
      ).toBeDefined()
      await alpha.findAll(rows)[0]!.trigger('click')
      expect(wrapper.emitted('select')?.[0]?.[0]).toMatchObject({ session_id: 'Alpha-0' })
      store.workspaces = [
        workspace('Alpha', { status: WorkspaceStatus.STOPPED }),
        workspace('Beta'),
      ]
      await wrapper.setProps({ workspaces: store.workspaces })
      expect(wrapper.find(groupSelector('Alpha')).exists()).toBe(false)
      store.workspaces = [workspace('Alpha'), workspace('Beta')]
      await wrapper.setProps({ workspaces: store.workspaces })
      await flushPromises()
      expect(wrapper.get(groupSelector('Alpha')).findAll(rows)).toHaveLength(8)
    },
  )

  it('forwards workspace and keyboard chat selection plus chat management events', async () => {
    const wrapper = mountList({ conversations: chats('Alpha', 1) })
    const row = wrapper.get('[aria-label="Open chat Alpha chat 0"]')
    await row.trigger('keydown.enter')
    await wrapper.get('[aria-label="Open workspace Alpha"]').trigger('click')
    expect(wrapper.emitted('select')?.[0]?.[0]).toMatchObject({ session_id: 'Alpha-0' })
    expect(wrapper.emitted('open')?.[0]).toEqual(['Alpha'])
    await wrapper.get('[data-testid="mark-unread-item"]').trigger('click')
    expect(wrapper.emitted('mark-unread')?.[0]?.[0]).toMatchObject({ session_id: 'Alpha-0' })
    const remove = wrapper.findAll('button').find((button) => button.text() === 'Delete')!
    await remove.trigger('click')
    expect(wrapper.emitted('delete')?.[0]?.[0]).toMatchObject({ session_id: 'Alpha-0' })
    const rename = wrapper.findAll('button').find((button) => button.text() === 'Rename')!
    await rename.trigger('click')
    await wrapper.get('[data-testid="conversation-rename-input"]').setValue('Renamed')
    await wrapper.get('[data-testid="conversation-rename-input"]').trigger('keydown.enter')
    expect(wrapper.emitted('rename')?.[0]?.[1]).toBe('Renamed')
  })
})
