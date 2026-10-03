import { afterEach, describe, expect, it, vi } from 'vitest'
import { getSubagentConfig, saveSubagentConfig } from './harness.api'
import * as api from './api'

describe('harness subagent api', () => {
  afterEach(() => vi.restoreAllMocks())

  it('gets the org-wide depth config', async () => {
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue({ max_depth: 2 })
    await expect(getSubagentConfig()).resolves.toEqual({ max_depth: 2 })
    expect(getSpy).toHaveBeenCalledWith('/subagent-config/')
  })

  it('saves the depth config', async () => {
    const putSpy = vi.spyOn(api, 'put').mockResolvedValue({ max_depth: 4 })
    await expect(saveSubagentConfig({ max_depth: 4 })).resolves.toEqual({ max_depth: 4 })
    expect(putSpy).toHaveBeenCalledWith('/subagent-config/', { max_depth: 4 })
  })

  it('propagates save errors', async () => {
    vi.spyOn(api, 'put').mockRejectedValue(new Error('denied'))
    await expect(saveSubagentConfig({ max_depth: 3 })).rejects.toThrow('denied')
  })
})
