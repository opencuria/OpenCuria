import { describe, expect, it, vi } from 'vitest'
import { getPlugin, listPlugins, createPlugin, updatePlugin, deletePlugin, togglePluginActivation, listWorkspacePlugins, updateWorkspacePlugins } from './plugins.api'
import * as api from './api'

describe('plugins api', () => {
  it('lists and fetches catalog plugins', async () => {
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue([])
    await expect(listPlugins()).resolves.toEqual([])
    expect(getSpy).toHaveBeenCalledWith('/plugins/')
    getSpy.mockRestore()

    const oneSpy = vi.spyOn(api, 'get').mockResolvedValue({ id: 'p-1' })
    await expect(getPlugin('p-1')).resolves.toEqual({ id: 'p-1' })
    expect(oneSpy).toHaveBeenCalledWith('/plugins/p-1/')
    oneSpy.mockRestore()
  })

  it('creates/updates/deletes/toggles via the matching endpoints', async () => {
    const postSpy = vi.spyOn(api, 'post').mockResolvedValue({ id: 'p-1' })
    await expect(createPlugin({ name: 'X' })).resolves.toEqual({ id: 'p-1' })
    expect(postSpy).toHaveBeenCalledWith('/plugins/', { name: 'X' })
    postSpy.mockRestore()

    const patchSpy = vi.spyOn(api, 'patch').mockResolvedValue({ id: 'p-1' })
    await expect(updatePlugin('p-1', { name: 'Y' })).resolves.toEqual({ id: 'p-1' })
    expect(patchSpy).toHaveBeenCalledWith('/plugins/p-1/', { name: 'Y' })
    patchSpy.mockRestore()

    const delSpy = vi.spyOn(api, 'del').mockResolvedValue(undefined)
    await expect(deletePlugin('p-1')).resolves.toBeUndefined()
    expect(delSpy).toHaveBeenCalledWith('/plugins/p-1/')
    delSpy.mockRestore()

    const toggleSpy = vi.spyOn(api, 'post').mockResolvedValue({ id: 'p-1' })
    await expect(togglePluginActivation('p-1', true)).resolves.toEqual({ id: 'p-1' })
    expect(toggleSpy).toHaveBeenCalledWith('/plugins/p-1/activation/', { active: true })
    toggleSpy.mockRestore()
  })

  it('lists and replaces workspace plugin activations', async () => {
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue([])
    await expect(listWorkspacePlugins('ws-1')).resolves.toEqual([])
    expect(getSpy).toHaveBeenCalledWith('/workspaces/ws-1/plugins/')
    getSpy.mockRestore()

    const putSpy = vi.spyOn(api, 'put').mockResolvedValue([])
    await expect(updateWorkspacePlugins('ws-1', { plugin_ids: ['p-1'] })).resolves.toEqual([])
    expect(putSpy).toHaveBeenCalledWith('/workspaces/ws-1/plugins/', { plugin_ids: ['p-1'] })
    putSpy.mockRestore()
  })
})
