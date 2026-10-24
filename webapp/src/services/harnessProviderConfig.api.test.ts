import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import { getProviderConfig, saveProviderConfig } from './harness.api'

const config = {
  small_model: 'provider/model-small',
  small_effort: 'low',
}

afterEach(() => vi.restoreAllMocks())

describe('harness provider config API', () => {
  it('loads the model and effort defaults-only config', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue(config)

    await expect(getProviderConfig()).resolves.toEqual(config)
    expect(get).toHaveBeenCalledWith('/provider-config/')
  })

  it('saves only model and effort defaults', async () => {
    const put = vi.spyOn(api, 'put').mockResolvedValue(config)

    await expect(saveProviderConfig({ small_model: 'provider/model-new' })).resolves.toEqual(config)
    expect(put).toHaveBeenCalledWith('/provider-config/', {
      small_model: 'provider/model-new',
      small_effort: '',
    })
  })
})
