import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, nextTick, ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import VoiceInputControl from './VoiceInputControl.vue'
import VoiceCaptureBar from './VoiceCaptureBar.vue'
import { getVoiceConfig, type VoiceConfig } from '@/services/voice.api'
import { voiceSupported, voiceUnavailableReason } from '@/composables/useVoiceInput'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'
import { pointingEngaged, pointingError } from '@/lib/voicePointing'
import { OPEN_SETTINGS_EVENT } from '@/components/settings/settingsTabs'

let deliverResult: (text: string, path?: string) => void
let voice: ReturnType<typeof makeVoice>
vi.mock('@/services/voice.api', () => ({ getVoiceConfig: vi.fn() }))
vi.mock('@/composables/useVoiceInput', () => ({
  voiceSupported: vi.fn(() => true), voiceUnavailableReason: vi.fn(() => ''),
  useVoiceInput: (callback: typeof deliverResult) => { deliverResult = callback; return voice },
}))
function makeVoice() {
  const state = ref<'idle' | 'requesting' | 'recording' | 'processing' | 'error'>('idle')
  return {
    state, error: ref(''), warning: ref(''), audioLevels: ref<number[]>([]), pointingFailed: ref(false),
    busy: computed(() => state.value !== 'idle'),
    start: vi.fn(async () => { state.value = 'recording' }),
    stop: vi.fn(async () => { state.value = 'processing' }),
    retry: vi.fn(async () => {}), useDictationOnly: vi.fn(async () => {}),
    cancel: vi.fn(() => { state.value = 'idle' }),
  }
}
const config: VoiceConfig = {
  available: true, default_model: 'openrouter/saved-speech', cleanup_model: 'openrouter/cleanup-model',
  models: [{ id: 'openrouter/saved-speech', name: 'Saved speech' }],
  max_duration_ms: 300_000, max_audio_bytes: 1_500_000,
}
let wrappers: VueWrapper[] = []
function create(props: { workspaceId: string; contextKey?: string; disabled?: boolean } = { workspaceId: 'workspace-1' }) {
  const wrapper = mount(VoiceInputControl, { props, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
function namedButton(text: string) {
  const result = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button'))
    .find((button) => button.textContent?.includes(text))
  expect(result, `button ${text}`).toBeDefined()
  return result!
}
async function record(wrapper: VueWrapper) {
  await wrapper.get('[data-testid="composer-voice"]').trigger('click')
  await flushPromises()
}
beforeEach(() => {
  vi.clearAllMocks()
  voice = makeVoice()
  voiceCaptureControls.value = null
  vi.mocked(voiceSupported).mockReturnValue(true)
  vi.mocked(voiceUnavailableReason).mockReturnValue('')
  vi.mocked(getVoiceConfig).mockResolvedValue(config)
})
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
  voiceCaptureControls.value = null
  pointingEngaged.value = false
  pointingError.value = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('VoiceInputControl', () => {
  it('starts with one click using server settings and has no model or pointing controls', async () => {
    const wrapper = create()
    expect(getVoiceConfig).not.toHaveBeenCalled()
    expect(wrapper.findAll('button')).toHaveLength(1)
    expect(wrapper.find('[data-testid="composer-voice-pointing"]').exists()).toBe(false)
    await record(wrapper)
    expect(getVoiceConfig).toHaveBeenCalledWith('workspace-1', expect.any(AbortSignal))
    expect(voice.start).toHaveBeenCalledExactlyOnceWith({
      workspaceId: 'workspace-1', model: config.default_model,
      maxDurationMs: config.max_duration_ms, maxAudioBytes: config.max_audio_bytes,
    })
    expect(wrapper.emitted('busy')?.slice(-1)[0]).toEqual([true])
    expect(wrapper.text()).not.toContain('openrouter')
    expect(wrapper.find('input, [role="combobox"]').exists()).toBe(false)
  })

  it('replaces the microphone with discard, waveform, stop and send while recording', async () => {
    const wrapper = create()
    await record(wrapper)
    expect(wrapper.find('[data-testid="composer-voice"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="voice-waveform"]').exists()).toBe(true)
    for (const id of ['voice-cancel', 'voice-stop', 'voice-send'])
      expect(wrapper.find(`[data-testid="${id}"]`).exists(), id).toBe(true)
    await wrapper.get('[data-testid="voice-cancel"]').trigger('click')
    expect(voice.cancel).toHaveBeenCalledOnce()
    expect(voice.stop).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="composer-voice"]').exists()).toBe(true)
  })

  it('shows the recording bar while voice settings load and lets it be discarded', async () => {
    let resolve!: (result: VoiceConfig) => void
    vi.mocked(getVoiceConfig).mockReturnValueOnce(new Promise((res) => { resolve = res }))
    const wrapper = create()
    await wrapper.get('[data-testid="composer-voice"]').trigger('click')
    expect(voiceCaptureControls.value?.state).toBe('requesting')
    expect(wrapper.text()).toContain('Waiting for microphone')
    expect(wrapper.get<HTMLButtonElement>('[data-testid="voice-stop"]').element.disabled).toBe(true)
    await wrapper.get('[data-testid="voice-cancel"]').trigger('click')
    resolve(config)
    await flushPromises()
    expect(voice.start).not.toHaveBeenCalled()
    expect(wrapper.emitted('busy')?.slice(-1)[0]).toEqual([false])
  })

  it('stops into the composer for editing and refreshes saved models for the next recording', async () => {
    const wrapper = create()
    await record(wrapper)
    await wrapper.get('[data-testid="voice-stop"]').trigger('click')
    expect(voice.stop).toHaveBeenCalledOnce()
    deliverResult('Edit me.')
    expect(wrapper.emitted('result')).toEqual([['Edit me.', undefined, false]])
    voice.state.value = 'idle'
    vi.mocked(getVoiceConfig).mockResolvedValueOnce({ ...config, default_model: 'openrouter/new-saved-model' })
    await nextTick()
    await record(wrapper)
    expect(voice.start).toHaveBeenLastCalledWith(expect.objectContaining({ model: 'openrouter/new-saved-model' }))
  })

  it('asks the composer to send once transcription finishes, including after a retry', async () => {
    const wrapper = create()
    await record(wrapper)
    await wrapper.get('[data-testid="voice-send"]').trigger('click')
    expect(voice.stop).toHaveBeenCalledOnce()
    voice.error.value = 'Provider unavailable'
    voice.state.value = 'error'
    await nextTick()
    namedButton('Retry').click()
    expect(voice.retry).toHaveBeenCalledOnce()
    deliverResult('Ship it.', '/workspace/.opencuria/recordings/id/metadata.json')
    expect(wrapper.emitted('result')).toEqual([
      ['Ship it.', '/workspace/.opencuria/recordings/id/metadata.json', true],
    ])
    // The send choice is consumed; a later plain stop must not send.
    deliverResult('Later.')
    expect(wrapper.emitted('result')?.[1]).toEqual(['Later.', undefined, false])
  })

  it('forgets a pending send when the recording is discarded', async () => {
    const wrapper = create()
    await record(wrapper)
    await wrapper.get('[data-testid="voice-send"]').trigger('click')
    await nextTick()
    await wrapper.get('[data-testid="voice-cancel"]').trigger('click')
    deliverResult('Stale.')
    expect(wrapper.emitted('result')).toEqual([['Stale.', undefined, false]])
  })

  it('opens existing provider settings for missing setup rather than showing a model picker', async () => {
    vi.mocked(getVoiceConfig).mockResolvedValue({ ...config, available: false })
    const listener = vi.fn()
    window.addEventListener(OPEN_SETTINGS_EVENT, listener)
    const wrapper = create()
    await record(wrapper)
    expect(wrapper.text()).toContain('Set up voice input in Settings')
    expect(voice.start).not.toHaveBeenCalled()
    namedButton('Open Settings').click()
    expect(listener).toHaveBeenCalledOnce()
    expect((listener.mock.calls[0]![0] as CustomEvent).detail).toEqual({ tab: 'provider' })
    window.removeEventListener(OPEN_SETTINGS_EVENT, listener)
  })

  it('reports a settings load failure and allows the next microphone click to retry', async () => {
    vi.mocked(getVoiceConfig).mockRejectedValueOnce(new Error('Connection unavailable'))
    const wrapper = create()
    await record(wrapper)
    expect(wrapper.get('[role="alert"]').text()).toContain('Connection unavailable')
    expect(voice.start).not.toHaveBeenCalled()
    await record(wrapper)
    expect(voice.start).toHaveBeenCalledOnce()
  })

  it('explains unavailable browser microphone access without fetching models', async () => {
    vi.mocked(voiceSupported).mockReturnValue(false)
    vi.mocked(voiceUnavailableReason).mockReturnValue('Your browser blocks microphone access. Open via http://localhost.')
    const wrapper = create()
    await record(wrapper)
    expect(wrapper.get('[role="alert"]').text()).toContain('http://localhost')
    expect(getVoiceConfig).not.toHaveBeenCalled()
    expect(voice.start).not.toHaveBeenCalled()
  })

  it('cancels pending settings and ignores a response after navigating to another chat', async () => {
    let resolve!: (result: VoiceConfig) => void
    vi.mocked(getVoiceConfig).mockReturnValueOnce(new Promise((res) => { resolve = res }))
    const wrapper = create({ workspaceId: 'workspace-1', contextKey: 'chat-1' })
    await wrapper.get('[data-testid="composer-voice"]').trigger('click')
    const signal = vi.mocked(getVoiceConfig).mock.calls[0]![1]!
    await wrapper.setProps({ workspaceId: 'workspace-2', contextKey: 'chat-2' })
    expect(signal.aborted).toBe(true)
    resolve(config)
    await flushPromises()
    expect(voice.start).not.toHaveBeenCalled()
    await record(wrapper)
    expect(voice.start).toHaveBeenCalledWith(expect.objectContaining({ workspaceId: 'workspace-2' }))
  })

  it('cancels recording when the composer becomes disabled', async () => {
    const wrapper = create()
    await record(wrapper)
    await wrapper.setProps({ disabled: true })
    expect(voice.cancel).toHaveBeenCalledOnce()
    expect(voiceCaptureControls.value).toBeNull()
    expect(wrapper.get<HTMLButtonElement>('[data-testid="composer-voice"]').element.disabled).toBe(true)
  })

  it('preserves retry and discard controls after processing errors', async () => {
    const wrapper = create()
    voice.error.value = 'Provider unavailable'
    voice.state.value = 'error'
    await nextTick()
    namedButton('Retry').click()
    expect(voice.retry).toHaveBeenCalledOnce()
    namedButton('Discard').click()
    await nextTick()
    expect(voice.cancel).toHaveBeenCalledOnce()
    expect(wrapper.emitted('busy')?.slice(-1)[0]).toEqual([false])
  })

  it('can explicitly keep dictation after visual capture fails', async () => {
    create()
    voice.error.value = 'Desktop disconnected'
    voice.state.value = 'error'
    voice.pointingFailed.value = true
    await nextTick()
    namedButton('Use dictation only').click()
    expect(voice.useDictationOnly).toHaveBeenCalledOnce()
  })

  it('removes global viewer controls and pending requests on unmount', async () => {
    const wrapper = create()
    await record(wrapper)
    expect(voiceCaptureControls.value?.workspaceId).toBe('workspace-1')
    wrapper.unmount()
    expect(voiceCaptureControls.value).toBeNull()
    expect(wrapper.emitted('busy')?.slice(-1)[0]).toEqual([false])
  })
})

describe('VoiceCaptureBar', () => {
  const resizeCallbacks = new Map<Element, (width: number) => void>()
  beforeEach(() => {
    resizeCallbacks.clear()
    class FakeResizeObserver {
      private targets = new Set<Element>()
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element): void {
        this.targets.add(target)
        resizeCallbacks.set(target, (width) => this.callback(
          [{ target, contentRect: { width } } as ResizeObserverEntry],
          this as unknown as ResizeObserver,
        ))
      }
      disconnect(): void {
        this.targets.forEach((target) => resizeCallbacks.delete(target))
        this.targets.clear()
      }
    }
    vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  })
  async function resize(wrapper: VueWrapper, width: number): Promise<void> {
    await nextTick()
    const callback = resizeCallbacks.get(wrapper.get('[data-testid="voice-waveform"]').element)
    expect(callback).toBeDefined()
    callback!(width)
    await nextTick()
  }
  function controls(overrides: Partial<NonNullable<typeof voiceCaptureControls.value>> = {}) {
    return {
      workspaceId: 'workspace-1', state: 'recording' as const, levels: [0, 0.5, 1],
      stop: vi.fn(), send: vi.fn(), cancel: vi.fn(), ...overrides,
    }
  }

  it('renders a live waveform with discard, stop and send controls', async () => {
    voiceCaptureControls.value = controls()
    const wrapper = mount(VoiceCaptureBar)
    wrappers.push(wrapper)
    await resize(wrapper, 480)
    const bars = wrapper.findAll('[data-testid="voice-waveform"] > span')
    expect(bars).toHaveLength(81)
    // Newest sample first in a reversed row, so it sits at the right edge.
    expect(bars.slice(0, 4).map((bar) => bar.attributes('style'))).toEqual([
      'height: 28px;', 'height: 16px;', 'height: 3px;', 'height: 3px;',
    ])
    await wrapper.get('[aria-label="Stop and edit"]').trigger('click')
    await wrapper.get('[aria-label="Stop and send"]').trigger('click')
    await wrapper.get('[aria-label="Discard recording"]').trigger('click')
    expect(voiceCaptureControls.value.stop).toHaveBeenCalledOnce()
    expect(voiceCaptureControls.value.send).toHaveBeenCalledOnce()
    expect(voiceCaptureControls.value.cancel).toHaveBeenCalledOnce()
  })

  it.each([false, true])('fills the available width and follows resizes (embedded: %s)', async (embedded) => {
    voiceCaptureControls.value = controls({ levels: Array(160).fill(1) })
    const wrapper = mount(VoiceCaptureBar, { props: { embedded } })
    wrappers.push(wrapper)
    const waveform = wrapper.get('[data-testid="voice-waveform"]')
    expect(waveform.attributes('class')).not.toContain('mask-image')
    for (const width of [240, 960, 2200, 4096, 357.5, 0, 1800]) {
      await resize(wrapper, width)
      const bars = waveform.findAll('span')
      expect(bars).toHaveLength(Math.ceil((width + 3) / 6))
      // The row covers the entire width, with less than one bar + gap clipped.
      const coverage = bars.length * 6 - 3
      expect(coverage).toBeGreaterThanOrEqual(width)
      expect(coverage - width).toBeLessThan(6)
      expect(bars[0]!.attributes('style')).toBe('height: 28px;')
      if (bars.length > 160) {
        expect(bars.slice(160).every((bar) => bar.attributes('style') === 'height: 3px;')).toBe(true)
      }
    }
    wrapper.unmount()
    expect(resizeCallbacks.has(waveform.element)).toBe(false)
  })

  it('fills a newly mounted waveform after waiting for microphone access', async () => {
    voiceCaptureControls.value = controls({ state: 'requesting' })
    const wrapper = mount(VoiceCaptureBar)
    wrappers.push(wrapper)
    expect(wrapper.find('[data-testid="voice-waveform"]').exists()).toBe(false)
    voiceCaptureControls.value = controls({ levels: [] })
    await nextTick()
    await resize(wrapper, 2100)
    const bars = wrapper.findAll('[data-testid="voice-waveform"] > span')
    expect(bars).toHaveLength(351)
    expect(bars.every((bar) => bar.attributes('style') === 'height: 3px;')).toBe(true)
    voiceCaptureControls.value = controls({ state: 'processing' })
    await nextTick()
    expect(wrapper.find('[data-testid="voice-waveform"]').exists()).toBe(false)
    expect(resizeCallbacks.size).toBe(0)
  })

  it('indicates pointing only once something was pointed at, and its failure', async () => {
    voiceCaptureControls.value = controls()
    const wrapper = mount(VoiceCaptureBar)
    wrappers.push(wrapper)
    expect(wrapper.find('[aria-label="Pointing is recorded"]').exists()).toBe(false)
    pointingEngaged.value = true
    await nextTick()
    expect(wrapper.find('[aria-label="Pointing is recorded"]').exists()).toBe(true)
    pointingError.value = 'Desktop disconnected'
    await nextTick()
    expect(wrapper.text()).toContain('Pointing failed')
  })

  it('keeps cancellation available during permissions, transcription and errors', async () => {
    voiceCaptureControls.value = controls({ state: 'requesting' })
    const wrapper = mount(VoiceCaptureBar)
    wrappers.push(wrapper)
    expect(wrapper.text()).toContain('Waiting for microphone')
    expect(wrapper.get<HTMLButtonElement>('[aria-label="Stop and send"]').element.disabled).toBe(true)
    voiceCaptureControls.value = controls({ state: 'processing' })
    await nextTick()
    expect(wrapper.text()).toContain('Transcribing')
    expect(wrapper.get<HTMLButtonElement>('[aria-label="Stop and edit"]').element.disabled).toBe(true)
    expect(wrapper.find('[aria-label="Discard recording"]').exists()).toBe(true)
    voiceCaptureControls.value = controls({ state: 'error' })
    await nextTick()
    expect(wrapper.text()).toContain('Review it in the composer')
    expect(wrapper.find('[aria-label="Stop and send"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Discard recording"]').exists()).toBe(true)
  })
})
