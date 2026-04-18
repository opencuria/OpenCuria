import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useWorkspaceStore } from './workspaces'
import {
  WorkspaceOperation,
  WorkspaceStatus,
  RuntimeType,
  type Workspace,
  type Chat,
} from '@/types'
import * as workspacesApi from '@/services/workspaces.api'

vi.mock('@/services/workspaces.api', async () => {
  const actual = await vi.importActual<typeof import('@/services/workspaces.api')>('@/services/workspaces.api')
  return {
    ...actual,
    listChats: vi.fn(),
  }
})

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: overrides.id ?? 'workspace-1',
    runner_id: overrides.runner_id ?? 'runner-1',
    status: overrides.status ?? WorkspaceStatus.RUNNING,
    active_operation: overrides.active_operation ?? null,
    name: overrides.name ?? 'Workspace',
    runtime_type: overrides.runtime_type ?? RuntimeType.DOCKER,
    qemu_vcpus: overrides.qemu_vcpus ?? null,
    qemu_memory_mb: overrides.qemu_memory_mb ?? null,
    qemu_disk_size_gb: overrides.qemu_disk_size_gb ?? null,
    created_by_id: overrides.created_by_id ?? 1,
    last_activity_at: overrides.last_activity_at ?? '2026-03-29T10:00:00.000Z',
    auto_stop_timeout_minutes: overrides.auto_stop_timeout_minutes ?? null,
    auto_stop_at: overrides.auto_stop_at ?? null,
    delete_requested_at: overrides.delete_requested_at ?? null,
    delete_started_at: overrides.delete_started_at ?? null,
    delete_confirmed_at: overrides.delete_confirmed_at ?? null,
    delete_last_error: overrides.delete_last_error ?? '',
    delete_attempt_count: overrides.delete_attempt_count ?? 0,
    created_at: overrides.created_at ?? '2026-03-29T10:00:00.000Z',
    updated_at: overrides.updated_at ?? '2026-03-29T10:00:00.000Z',
    has_active_session: overrides.has_active_session ?? false,
    runner_online: overrides.runner_online ?? true,
    credential_ids: overrides.credential_ids ?? [],
  }
}

function makeChat(overrides: Partial<Chat> = {}): Chat {
  return {
    id: overrides.id ?? 'chat-1',
    workspace_id: overrides.workspace_id ?? 'workspace-1',
    name: overrides.name ?? 'Chat',
    agent_definition_id: overrides.agent_definition_id ?? 'agent-1',
    agent_type: overrides.agent_type ?? 'codex',
    created_at: overrides.created_at ?? '2026-03-29T10:00:00.000Z',
    updated_at: overrides.updated_at ?? '2026-03-29T10:00:00.000Z',
    session_count: overrides.session_count ?? 0,
    is_pending: overrides.is_pending,
  }
}

describe('workspace transition state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('derives transition labels from backend active_operation', () => {
    const store = useWorkspaceStore()
    store.workspaces = [
      makeWorkspace({
        id: 'workspace-restart',
        active_operation: WorkspaceOperation.RESTARTING,
      }),
    ]

    expect(store.isWorkspaceTransitioning('workspace-restart')).toBe(true)
    expect(store.getWorkspaceTransitionLabel('workspace-restart')).toBe('Restarting…')
  })

  it('clears optimistic pending state when the backend operation resets', () => {
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace({ id: 'workspace-stop' })]
    store.pendingWorkspaceOperations['workspace-stop'] = {
      operation: 'stop',
      expectedStatus: WorkspaceStatus.STOPPED,
    }

    store.updateWorkspaceOperation('workspace-stop', null)

    expect(store.pendingWorkspaceOperations['workspace-stop']).toBeUndefined()
    expect(store.isWorkspaceTransitioning('workspace-stop')).toBe(false)
  })

  it('selects the first chat after loading when no active chat is set', async () => {
    const store = useWorkspaceStore()
    vi.mocked(workspacesApi.listChats).mockResolvedValue([
      makeChat({ id: 'chat-a' }),
      makeChat({ id: 'chat-b' }),
    ])

    await store.fetchChats('workspace-1')

    expect(store.chats.map((chat) => chat.id)).toEqual(['chat-a', 'chat-b'])
    expect(store.activeChatId).toBe('chat-a')
  })

  it('keeps the current active chat selected when it still exists after refresh', async () => {
    const store = useWorkspaceStore()
    store.activeChatId = 'chat-b'
    vi.mocked(workspacesApi.listChats).mockResolvedValue([
      makeChat({ id: 'chat-a' }),
      makeChat({ id: 'chat-b' }),
    ])

    await store.fetchChats('workspace-1')

    expect(store.activeChatId).toBe('chat-b')
  })

  it('clears the active chat when loading chats fails', async () => {
    const store = useWorkspaceStore()
    store.activeChatId = 'chat-stale'
    vi.mocked(workspacesApi.listChats).mockRejectedValue(new Error('network down'))

    await store.fetchChats('workspace-1')

    expect(store.chats).toEqual([])
    expect(store.activeChatId).toBeNull()
  })
})
