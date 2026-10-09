import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import {
  createHarnessSession,
  deleteClaudeConnection,
  getClaudeConnection,
  listClaudeModels,
  listHarnessEngines,
  saveClaudeConnection,
} from './harness.api'

const engines = [
  { id: 'native', name: 'OpenCuria', modes: ['build', 'plan'], connected: true },
  { id: 'claude', name: 'Claude Agent', modes: ['build', 'plan'], connected: false },
]
const models = [
  {
    id: 'sonnet',
    name: 'Claude Sonnet',
    provider: 'claude',
    reasoning_efforts: ['high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 200_000,
    max_output_tokens: 16_000,
  },
]

afterEach(() => vi.restoreAllMocks())

describe('harness engine API', () => {
  it('lists engines and keeps Claude models on their dedicated catalog endpoint', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValueOnce(engines).mockResolvedValueOnce(models)
    await expect(listHarnessEngines()).resolves.toEqual(engines)
    await expect(listClaudeModels()).resolves.toEqual(models)
    expect(get).toHaveBeenNthCalledWith(1, '/harness/engines/')
    expect(get).toHaveBeenNthCalledWith(2, '/harness/engines/claude/models/')
  })

  it('persists the selected engine only when creating a Claude session', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ id: 'session-1' })
    await createHarnessSession('ws-1', { prompt: 'hello', harness_id: 'claude', mode: 'plan' })
    expect(post).toHaveBeenCalledWith('/workspaces/ws-1/harness/sessions/', {
      prompt: 'hello',
      agent_name: 'build',
      mode: 'plan',
      model: '',
      reasoning_effort: '',
      skill_ids: [],
      harness_id: 'claude',
    })
    post.mockClear()
    await createHarnessSession('ws-1', { prompt: 'hello', harness_id: 'native' })
    expect(post).toHaveBeenCalledWith('/workspaces/ws-1/harness/sessions/', {
      prompt: 'hello',
      agent_name: 'build',
      mode: 'build',
      model: '',
      reasoning_effort: '',
      skill_ids: [],
      harness_id: 'native',
    })
  })

  it('reads and saves safe connection metadata without reshaping token payloads', async () => {
    const connection = {
      id: 'personal-1',
      auth_type: 'api_token' as const,
      label: 'Personal',
      connected: true,
      created_at: '2026-10-08T00:00:00Z',
      updated_at: '2026-10-08T00:00:00Z',
    }
    const get = vi.spyOn(api, 'get').mockResolvedValue(connection)
    const put = vi.spyOn(api, 'put').mockResolvedValue(connection)
    await expect(getClaudeConnection()).resolves.toEqual(connection)
    await expect(
      saveClaudeConnection({ auth_type: 'api_token', token: 'secret', label: 'Personal' }),
    ).resolves.toEqual(connection)
    expect(get).toHaveBeenCalledWith('/harness/engines/claude/connection/')
    expect(put).toHaveBeenCalledWith('/harness/engines/claude/connection/', {
      auth_type: 'api_token',
      token: 'secret',
      label: 'Personal',
    })
  })

  it('deletes the personal Claude connection', async () => {
    const del = vi.spyOn(api, 'del').mockResolvedValue(undefined)
    await expect(deleteClaudeConnection()).resolves.toBeUndefined()
    expect(del).toHaveBeenCalledWith('/harness/engines/claude/connection/')
  })
})
