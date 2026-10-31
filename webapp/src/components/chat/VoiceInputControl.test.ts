import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, nextTick, ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import VoiceInputControl from './VoiceInputControl.vue'
import VoiceCaptureBar from './VoiceCaptureBar.vue'
import { getVoiceConfig, type VoiceConfig } from '@/services/voice.api'
import { voiceSupported, voiceUnavailableReason } from '@/composables/useVoiceInput'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'
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
    state, error: ref(''), warning: ref(''), elapsedMs: ref(0), audioLevel: ref(0), pointingFailed: ref(false),
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
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('VoiceInputControl', () => {
  it('starts with one click using server settings and has no model controls in chat', async () => {
    const wrapper = create()
    expect(getVoiceConfig).not.toHaveBeenCalled()
    expect(wrapper.findAll('button')).toHaveLength(2)
    await record(wrapper)
    expect(getVoiceConfig).toHaveBeenCalledWith('workspace-1', expect.any(AbortSignal))
    expect(voice.start).toHaveBeenCalledExactlyOnceWith({
      workspaceId: 'workspace-1', model: config.default_model, pointing: false,
      maxDurationMs: config.max_duration_ms, maxAudioBytes: config.max_audio_bytes,
    })
    expect(wrapper.emitted('busy')?.slice(-1)[0]).toEqual([true])
    expect(wrapper.text()).not.toContain('openrouter')
    expect(wrapper.find('input, [role="combobox"]').exists()).toBe(false)
  })

  it('offers a direct pointing toggle before recording and keeps it locked during capture', async () => {
    const wrapper = create()
    const toggle = wrapper.get<HTMLButtonElement>('[data-testid="composer-voice-pointing"]')
    expect(toggle.attributes('aria-pressed')).toBe('false')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-pressed')).toBe('true')
    await record(wrapper)
    expect(voice.start).toHaveBeenCalledWith(expect.objectContaining({ pointing: true }))
    expect(toggle.element.disabled).toBe(true)
    expect(voiceCaptureControls.value?.pointing).toBe(true)
  })

  it('finishes via the same microphone control and refreshes saved models for the next recording', async () => {
    const wrapper = create()
    await record(wrapper)
    expect(wrapper.get('[data-testid="composer-voice"]').attributes('aria-label')).toBe('Finish dictation')
    await wrapper.get('[data-testid="composer-voice"]').trigger('click')
    expect(voice.stop).toHaveBeenCalledOnce()
    voice.state.value = 'idle'
    vi.mocked(getVoiceConfig).mockResolvedValueOnce({ ...config, default_model: 'openrouter/new-saved-model' })
    await nextTick()
    await record(wrapper)
    expect(voice.start).toHaveBeenLastCalledWith(expect.objectContaining({ model: 'openrouter/new-saved-model' }))
  })

  it('forwards cleaned dictation and the recording reference', () => {
    const wrapper = create()
    deliverResult('Change this button.', '/workspace/.opencuria/recordings/id/metadata.json')
    expect(wrapper.emitted('result')).toEqual([['Change this button.', '/workspace/.opencuria/recordings/id/metadata.json']])
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
  it('exposes finish and discard controls while displaying elapsed recording time', async () => {
    const stop = vi.fn()
    const cancel = vi.fn()
    voiceCaptureControls.value = { workspaceId: 'workspace-1', state: 'recording', elapsedMs: 65_000, pointing: true, stop, cancel }
    const wrapper = mount(VoiceCaptureBar)
    wrappers.push(wrapper)
    expect(wrapper.text()).toContain('Dictating + pointing · 1:05')
    await wrapper.get('[aria-label="Finish dictation"]').trigger('click')
    await wrapper.get('[aria-label="Discard dictation"]').trigger('click')
    expect(stop).toHaveBeenCalledOnce()
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('keeps cancellation available during permissions and processing', async () => {
    voiceCaptureControls.value = { workspaceId: 'workspace-1', state: 'requesting', elapsedMs: 0, pointing: false, stop: vi.fn(), cancel: vi.fn() }
    const wrapper = mount(VoiceCaptureBar)
    wrappers.push(wrapper)
    expect(wrapper.text()).toContain('Waiting for microphone')
    expect(wrapper.find('[aria-label="Finish dictation"]').exists()).toBe(false)
    voiceCaptureControls.value = { ...voiceCaptureControls.value, state: 'processing' }
    await nextTick()
    expect(wrapper.text()).toContain('Preparing your text')
    expect(wrapper.find('[aria-label="Discard dictation"]').exists()).toBe(true)
    voiceCaptureControls.value = { ...voiceCaptureControls.value, state: 'error' }
    await nextTick()
    expect(wrapper.text()).toContain('Voice input needs attention')
    expect(wrapper.find('[aria-label="Discard dictation"]').exists()).toBe(true)
  })
})
