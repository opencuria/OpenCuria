import { describe, expect, it, vi } from 'vitest'
import { getAgentSConfig, saveAgentSConfig } from './harness.api'
import * as api from './api'

describe('harness agent-s api', () => {
  it('gets and saves the Agent-S config via /agent-s-config/', async () => {
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue({ max_steps: 15 })
    await expect(getAgentSConfig()).resolves.toEqual({ max_steps: 15 })
    expect(getSpy).toHaveBeenCalledWith('/agent-s-config/')
    getSpy.mockRestore()

    const putSpy = vi.spyOn(api, 'put').mockResolvedValue({ max_steps: 20 })
    await expect(saveAgentSConfig({ max_steps: 20 })).resolves.toEqual({ max_steps: 20 })
    expect(putSpy).toHaveBeenCalledWith('/agent-s-config/', { max_steps: 20 })
    putSpy.mockRestore()
  })

  it('saves the recording opt-in flag via /agent-s-config/', async () => {
    const putSpy = vi.spyOn(api, 'put').mockResolvedValue({ enable_recording: true })
    await expect(saveAgentSConfig({ enable_recording: true })).resolves.toEqual({
      enable_recording: true,
    })
    expect(putSpy).toHaveBeenCalledWith('/agent-s-config/', { enable_recording: true })
    putSpy.mockRestore()
  })
})
