import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import ConversationWorkspaceList from './ConversationWorkspaceList.vue'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import type { HarnessConversation } from '@/types/harness'

enableAutoUnmount(afterEach)

const stubs = {
  Tooltip: { template: '<div><slot /></div>' },
  TooltipContent: true,
  TooltipTrigger: { template: '<div><slot /></div>' },
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: { template: '<button @click="$emit(\'click\')"><slot /></button>' },
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
    expect(wrapper.get('[aria-label="Open workspace Alpha"]').classes()).toContain('bg-primary/10')
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

  it('shows empty live/starting workspaces and navigation, but not stopped empty ones', async () => {
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
    expect(wrapper.text()).toContain('No chats yet — start with New chat')
    expect(wrapper.findAll('[data-testid="workspace-conversation-group"]')).toHaveLength(2)
    expect(wrapper.find(groupSelector('Stopped')).exists()).toBe(false)
    expect(wrapper.get('[data-testid="all-workspaces"]').text()).toBe('All workspaces (3)')
    expect(
      wrapper.get(groupSelector('Booting')).get('[data-testid="workspace-status"]').classes(),
    ).toContain('bg-amber-500')
    await wrapper.get('[data-testid="workspaces-create"]').trigger('click')
    await wrapper.get('[data-testid="all-workspaces"]').trigger('click')
    expect(wrapper.emitted('create')).toHaveLength(1)
    expect(wrapper.emitted('open-all')).toHaveLength(1)
    expect(wrapper.find('[data-testid="mark-all-read"]').exists()).toBe(false)
  })

  it('keeps unknown workspace history while workspace fetching is unavailable', () => {
    const wrapper = mountList({ workspaces: [], conversations: chats('Alpha', 1) })
    expect(wrapper.get(groupSelector('Alpha')).attributes('data-online')).toBe('false')
    expect(wrapper.find('[aria-label="Open chat Alpha chat 0"]').exists()).toBe(true)
  })

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
