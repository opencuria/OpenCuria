import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useDesktopStore } from './desktop'

describe('desktop store leases', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('tracks computer-use overlay from status and subtask ids', () => {
    const store = useDesktopStore()
    expect(store.computerUseActive).toBe(false)

    store.setComputerUseActive(true)
    expect(store.computerUseActive).toBe(true)

    store.markComputerUseStarted('cu-1')
    store.markComputerUseFinished('cu-1')
    expect(store.computerUseActive).toBe(false)
  })

  it('keeps overlay until the last computer-use run finishes', () => {
    const store = useDesktopStore()
    store.markComputerUseStarted('cu-1')
    store.markComputerUseStarted('cu-2')
    store.markComputerUseFinished('cu-1')
    expect(store.computerUseActive).toBe(true)
    store.markComputerUseFinished('cu-2')
    expect(store.computerUseActive).toBe(false)
  })

  it('clears computer-use state on reset but not on viewer disconnect', () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)
    store.setDisconnected()
    expect(store.isConnected).toBe(false)
    expect(store.computerUseActive).toBe(true)
    store.reset()
    expect(store.computerUseActive).toBe(false)
  })
})
