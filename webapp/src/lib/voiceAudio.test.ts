import { afterEach, describe, expect, it, vi } from 'vitest'
import { monitorVoiceAudio } from './voiceAudio'

afterEach(() => vi.unstubAllGlobals())

function audioFixture(amplitude: number, state = 'running') {
  const source = { connect: vi.fn(), disconnect: vi.fn() }
  const analyser = {
    fftSize: 0,
    getFloatTimeDomainData: (samples: Float32Array) => samples.fill(amplitude),
    disconnect: vi.fn(),
  }
  const close = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('AudioContext', class {
    state = state
    resume = vi.fn().mockResolvedValue(undefined)
    close = close
    createMediaStreamSource = () => source
    createAnalyser = () => analyser
  })
  const monitor = monitorVoiceAudio({} as MediaStream)!
  return { monitor, source, analyser, close }
}

describe('microphone audio monitor', () => {
  it('recognizes digital silence without connecting microphone playback', () => {
    const { monitor, source, analyser, close } = audioFixture(0)
    expect(monitor.hasSignal()).toBeNull()
    expect(monitor.sample()).toBe(0)
    expect(monitor.hasSignal()).toBe(false)
    expect(source.connect).toHaveBeenCalledExactlyOnceWith(analyser)
    monitor.close()
    monitor.close()
    expect(close).toHaveBeenCalledOnce()
    expect(source.disconnect).toHaveBeenCalledOnce()
    expect(analyser.disconnect).toHaveBeenCalledOnce()
  })

  it('retains quiet audio rather than applying aggressive speech classification', () => {
    const { monitor } = audioFixture(0.00002)
    expect(monitor.sample()).toBeGreaterThan(0)
    expect(monitor.hasSignal()).toBe(true)
  })

  it('does not claim silence when a browser suspends its audio context', () => {
    const { monitor } = audioFixture(0, 'suspended')
    monitor.sample()
    expect(monitor.hasSignal()).toBeNull()
  })

  it('opts out on browsers without Web Audio instead of blocking dictation', () => {
    vi.stubGlobal('AudioContext', undefined)
    expect(monitorVoiceAudio({} as MediaStream)).toBeNull()
  })
})
