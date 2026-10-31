import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, nextTick, ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import VoiceInputControl from './VoiceInputControl.vue'
import VoiceCaptureBar from './VoiceCaptureBar.vue'
import { getVoiceConfig, type VoiceConfig } from '@/services/voice.api'
import { voiceSupported, voiceUnavailableReason } from '@/composables/useVoiceInput'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'

let deliverResult: (text: string, path?: string) => void
let voice: ReturnType<typeof makeVoice>

vi.mock('@/services/voice.api', () => ({ getVoiceConfig: vi.fn() }))
vi.mock('@/composables/useVoiceInput', () => ({
  voiceSupported: vi.fn(() => true),
  voiceUnavailableReason: vi.fn(() => ''),
  useVoiceInput: (callback: typeof deliverResult) => { deliverResult = callback; return voice },
}))

function makeVoice() {
  const state = ref<'idle' | 'requesting' | 'recording' | 'processing' | 'error'>('idle')
  return {
    state, error: ref(''), warning: ref(''), elapsedMs: ref(0), pointingFailed: ref(false),
    busy: computed(() => state.value !== 'idle'),
    start: vi.fn(async () => { state.value = 'recording' }),
    stop: vi.fn(async () => { state.value = 'processing' }),
    retry: vi.fn(async () => {}),
    useDictationOnly: vi.fn(async () => {}),
    cancel: vi.fn(() => { state.value = 'idle' }),
  }
}
const config: VoiceConfig = {
  available: true, default_model: 'openrouter/openai/whisper-1', cleanup_model: 'openrouter/cleanup-model',
  models: [{ id: 'openrouter/openai/whisper-1', name: 'Whisper' }],
  max_duration_ms: 300_000, max_audio_bytes: 20 * 1024 * 1024,
}
let wrappers: VueWrapper[] = []

function create(props: { workspaceId: string; contextKey?: string; disabled?: boolean } = { workspaceId: 'workspace-1' }) {
  const wrapper = mount(VoiceInputControl, {
    props, attachTo: document.body,
    global: { stubs: {
      ModelPicker: {
        props: ['model'], emits: ['update:model'],
        template: '<input aria-label="Speech model" :value="model" @input="$emit(\'update:model\', $event.target.value)" />',
      },
      Checkbox: {
        props: ['modelValue'], emits: ['update:modelValue'],
        template: '<button role="checkbox" :aria-checked="modelValue" @click="$emit(\'update:modelValue\', !modelValue)" />',
      },
    } },
  })
  wrappers.push(wrapper)
  return wrapper
}
function namedButton(text: string) {
  const result = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button'))
    .find((button) => button.textContent?.includes(text))
  expect(result, `button ${text}`).toBeDefined()
  return result!
}
async function open(wrapper: VueWrapper) {
  await wrapper.get('[data-testid="composer-voice"]').trigger('click')
  await flushPromises()
  await nextTick()
}

beforeEach(() => {
  vi.clearAllMocks()
  voice = makeVoice()
  voiceCaptureControls.value = null
  vi.mocked(voiceSupported).mockReturnValue(true)
  vi.mocked(voiceUnavailableReason).mockReturnValue('')
  vi.mocked(getVoiceConfig).mockResolvedValue(config)
  vi.stubGlobal('ResizeObserver', class { observe = vi.fn(); unobserve = vi.fn(); disconnect = vi.fn() })
})
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
  voiceCaptureControls.value = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('VoiceInputControl', () => {
  it('loads voice settings on demand and starts ordinary dictation by default', async () => {
    const wrapper = create()
    expect(getVoiceConfig).not.toHaveBeenCalled()
    await open(wrapper)
    expect(getVoiceConfig).toHaveBeenCalledWith('workspace-1', expect.any(AbortSignal))
    expect(document.body.textContent).toContain('Filler words are removed')
    expect(document.body.querySelector('[data-testid="voice-cleanup-model"]')?.textContent).toContain(config.cleanup_model)
    expect(document.querySelector('[role="checkbox"]')?.getAttribute('aria-checked')).toBe('false')
    namedButton('Start dictating').click()
    await flushPromises()
    expect(voice.start).toHaveBeenCalledExactlyOnceWith({
      workspaceId: 'workspace-1', model: config.default_model, pointing: false,
      maxDurationMs: config.max_duration_ms, maxAudioBytes: config.max_audio_bytes,
    })
    expect(wrapper.emitted('busy')?.slice(-1)[0]).toEqual([true])
  })

  it('requires an explicit choice to record pointing and explains workspace capture', async () => {
    const wrapper = create()
    await open(wrapper)
    document.querySelector<HTMLButtonElement>('[role="checkbox"]')!.click()
    await nextTick()
    expect(document.body.textContent).toContain('Pointer positions and desktop snapshots are saved in this workspace')
    namedButton('Start dictating').click()
    await flushPromises()
    expect(voice.start).toHaveBeenCalledWith(expect.objectContaining({ pointing: true }))
    expect(voiceCaptureControls.value?.pointing).toBe(true)
  })

  it('starts configured dictation with one microphone click and keeps settings separately available', async () => {
    const wrapper = create()
    await open(wrapper)
    namedButton('Start dictating').click()
    await flushPromises()
    voice.state.value = 'idle'
    await nextTick()
    await wrapper.get('[data-testid="composer-voice"]').trigger('click')
    expect(voice.start).toHaveBeenCalledTimes(2)
    expect(getVoiceConfig).toHaveBeenCalledOnce()
    voice.state.value = 'idle'
    await nextTick()
    await wrapper.get('[aria-label="Voice input settings"]').trigger('click')
    await flushPromises()
    expect(getVoiceConfig).toHaveBeenCalledTimes(2)
    expect(document.body.textContent).toContain('Start dictating')
  })

  it('forwards a reviewed transcript and artifact reference to the composer', () => {
    const wrapper = create()
    deliverResult('Please change this button.', '/workspace/.opencuria/recordings/id/manifest.json')
    expect(wrapper.emitted('result')).toEqual([
      ['Please change this button.', '/workspace/.opencuria/recordings/id/manifest.json'],
    ])
  })

  it('shows provider setup failures and supports reloading settings', async () => {
    vi.mocked(getVoiceConfig).mockRejectedValueOnce(new Error('Connection unavailable'))
    const wrapper = create()
    await open(wrapper)
    expect(document.body.textContent).toContain('Connection unavailable')
    expect(document.body.textContent).not.toContain('Start dictating')
    namedButton('Reload settings').click()
    await flushPromises()
    expect(getVoiceConfig).toHaveBeenCalledTimes(2)
    expect(document.body.textContent).toContain('Start dictating')
  })

  it('shows a provider configuration reason without allowing microphone capture', async () => {
    vi.mocked(getVoiceConfig).mockResolvedValue({ ...config, available: false, reason: 'Connect an OpenRouter account.' })
    const wrapper = create()
    await open(wrapper)
    expect(document.body.textContent).toContain('Connect an OpenRouter account.')
    expect(document.body.textContent).not.toContain('Start dictating')
    expect(voice.start).not.toHaveBeenCalled()
  })

  it('keeps model settings visible when this browser blocks microphone access', async () => {
    vi.mocked(voiceSupported).mockReturnValue(false)
    vi.mocked(voiceUnavailableReason).mockReturnValue('Your browser blocks microphone access at this HTTP address. Open via http://localhost.')
    const wrapper = create()
    await open(wrapper)
    expect(document.body.textContent).toContain('http://localhost')
    expect(document.body.querySelector('[aria-label="Speech model"]')).not.toBeNull()
    expect(document.body.querySelector('[data-testid="voice-cleanup-model"]')?.textContent).toContain(config.cleanup_model)
    const button = namedButton('Start dictating')
    expect(button.disabled).toBe(true)
    button.click()
    expect(voice.start).not.toHaveBeenCalled()
  })

  it('cancels on workspace or chat navigation and ignores stale settings', async () => {
    let resolve!: (result: VoiceConfig) => void
    vi.mocked(getVoiceConfig).mockReturnValueOnce(new Promise((res) => { resolve = res }))
    const wrapper = create({ workspaceId: 'workspace-1', contextKey: 'chat-1' })
    await open(wrapper)
    const signal = vi.mocked(getVoiceConfig).mock.calls[0]![1]!
    await wrapper.setProps({ workspaceId: 'workspace-2', contextKey: 'chat-2' })
    expect(voice.cancel).toHaveBeenCalledOnce()
    expect(signal.aborted).toBe(true)
    resolve({ ...config, default_model: 'old-model', models: [{ id: 'old-model', name: 'Old' }] })
    await flushPromises()
    await open(wrapper)
    namedButton('Start dictating').click()
    await flushPromises()
    expect(voice.start).toHaveBeenCalledWith(expect.objectContaining({ workspaceId: 'workspace-2', model: config.default_model }))
  })

  it('cancels recording if the composer becomes disabled', async () => {
    const wrapper = create()
    voice.state.value = 'recording'
    await nextTick()
    await wrapper.setProps({ disabled: true })
    expect(voice.cancel).toHaveBeenCalledOnce()
    expect(voiceCaptureControls.value).toBeNull()
    expect(wrapper.get<HTMLButtonElement>('[data-testid="composer-voice"]').element.disabled).toBe(true)
  })

  it('offers retry and discard for processing failure while preserving the recording', async () => {
    const wrapper = create()
    voice.error.value = 'Provider unavailable'
    voice.state.value = 'error'
    await nextTick()
    expect(wrapper.get('[role="alert"]').text()).toContain('Provider unavailable')
    namedButton('Retry').click()
    await nextTick()
    expect(voice.retry).toHaveBeenCalledOnce()
    namedButton('Discard').click()
    expect(voice.cancel).toHaveBeenCalledOnce()
  })

  it('offers an explicit dictation-only choice after a failed visual recording', async () => {
    create()
    voice.error.value = 'Desktop disconnected'
    voice.state.value = 'error'
    voice.pointingFailed.value = true
    await nextTick()
    expect(document.body.textContent).not.toContain('Retry')
    namedButton('Use dictation only').click()
    expect(voice.useDictationOnly).toHaveBeenCalledOnce()
  })

  it('removes global viewer controls on unmount', async () => {
    const wrapper = create()
    voice.state.value = 'recording'
    await nextTick()
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
