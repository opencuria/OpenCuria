import { describe, expect, it, vi } from 'vitest'
import { getComputerUseConfig, saveComputerUseConfig } from './harness.api'
import * as api from './api'

describe('harness computer-use api', () => {
  it('gets and saves the Computer Use config via /computer-use-config/', async () => {
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue({ max_steps: 15 })
    await expect(getComputerUseConfig()).resolves.toEqual({ max_steps: 15 })
    expect(getSpy).toHaveBeenCalledWith('/computer-use-config/')
    getSpy.mockRestore()

    const putSpy = vi.spyOn(api, 'put').mockResolvedValue({ max_steps: 20 })
    await expect(saveComputerUseConfig({ max_steps: 20 })).resolves.toEqual({ max_steps: 20 })
    expect(putSpy).toHaveBeenCalledWith('/computer-use-config/', { max_steps: 20 })
    putSpy.mockRestore()
  })

  it('saves the recording opt-in flag via /computer-use-config/', async () => {
    const putSpy = vi.spyOn(api, 'put').mockResolvedValue({ enable_recording: true })
    await expect(saveComputerUseConfig({ enable_recording: true })).resolves.toEqual({
      enable_recording: true,
    })
    expect(putSpy).toHaveBeenCalledWith('/computer-use-config/', { enable_recording: true })
    putSpy.mockRestore()
  })
})
