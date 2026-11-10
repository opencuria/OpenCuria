import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useImageStore } from './images'

const api = vi.hoisted(() => ({
  listCaptured: vi.fn(),
  listDefinitions: vi.fn(),
  listBuilds: vi.fn(),
}))
vi.mock('@/services/workspaces.api', () => ({
  listCapturedImages: api.listCaptured,
  listImageDefinitions: api.listDefinitions,
  listRunnerImageBuilds: api.listBuilds,
}))

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

describe('image store', () => {
  it('marks the captured list loaded and keeps its error until a refresh succeeds', async () => {
    const store = useImageStore()
    expect(store.capturedImagesLoaded).toBe(false)
    api.listCaptured.mockRejectedValueOnce(new Error('captured down'))
    await store.fetchCapturedImages()
    expect(store.capturedImagesLoaded).toBe(true)
    expect(store.error).toBe('captured down')

    let resolve!: (images: never[]) => void
    api.listCaptured.mockReturnValueOnce(new Promise((done) => (resolve = done)))
    const retry = store.fetchCapturedImages()
    expect(store.error).toBe('captured down')
    resolve([])
    await retry
    expect(store.error).toBeNull()
  })

  it('reports definition errors apart from the captured images list', async () => {
    const store = useImageStore()
    api.listCaptured.mockResolvedValue([])
    api.listDefinitions.mockRejectedValueOnce(new Error('definitions down'))
    await Promise.all([store.fetchCapturedImages(), store.fetchImageDefinitionsWithBuilds()])
    expect(store.error).toBeNull()
    expect(store.definitionsError).toBe('definitions down')

    api.listDefinitions.mockResolvedValueOnce([{ id: 'def-1' }])
    api.listBuilds.mockResolvedValueOnce([{ id: 'build-1' }])
    await store.fetchImageDefinitionsWithBuilds()
    expect(store.definitionsError).toBeNull()
    expect(store.runnerBuildsByDefinition).toEqual({ 'def-1': [{ id: 'build-1' }] })
  })
})
