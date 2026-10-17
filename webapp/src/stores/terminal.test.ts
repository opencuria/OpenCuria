import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useTerminalStore } from './terminal'

describe('terminal reconnect state', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('keeps the remote identity and output cursor while locally disconnected', () => {
    const terminal = useTerminalStore()
    terminal.setConnected('terminal-1', 'workspace-1', 4)
    terminal.setDisconnected(true)

    expect(terminal.isConnected).toBe(false)
    expect(terminal.terminalId).toBe('terminal-1')
    expect(terminal.workspaceId).toBe('workspace-1')
    expect(terminal.outputSequence).toBe(4)
  })

  it('accepts output once in order and rejects duplicates and gaps', () => {
    const terminal = useTerminalStore()
    terminal.setConnected('terminal-1', 'workspace-1')

    expect(terminal.recordOutput(1)).toBe(true)
    expect(terminal.recordOutput(1)).toBe(false)
    expect(terminal.recordOutput(3)).toBe(false)
    expect(terminal.recordOutput(2)).toBe(true)
    expect(terminal.outputSequence).toBe(2)
  })

  it('tracks uncertain input IDs without replaying them on reconnect', () => {
    const terminal = useTerminalStore()
    terminal.setConnected('terminal-1', 'workspace-1')
    terminal.addPendingInput('input-1')
    terminal.setDisconnected(true)

    expect(terminal.pendingInputs).toEqual(['input-1'])
    expect(terminal.unconfirmedInputs).toEqual(['input-1'])
    terminal.settlePendingInput('input-1')
    expect(terminal.unconfirmedInputs).toEqual([])
  })
})
