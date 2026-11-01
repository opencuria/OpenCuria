import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'
import { useVoiceInput, voiceSupported, voiceUnavailableReason } from './useVoiceInput'
import { fileToBase64 } from '@/lib/chatUpload'
import { monitorVoiceAudio } from '@/lib/voiceAudio'
import { startPointing } from '@/lib/voicePointing'
import { VOICE_LEVEL_HISTORY } from '@/lib/voiceCaptureControls'
import { saveVoiceRecording, transcribeVoice, type VoiceTranscript } from '@/services/voice.api'

vi.mock('@/lib/voiceAudio', () => ({ monitorVoiceAudio: vi.fn(() => null) }))
vi.mock('@/lib/chatUpload', () => ({ fileToBase64: vi.fn() }))
vi.mock('@/lib/voicePointing', () => ({ startPointing: vi.fn() }))
vi.mock('@/services/voice.api', () => ({
  transcribeVoice: vi.fn(), saveVoiceRecording: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

// Final audio arrives asynchronously after stop(), as it does in browsers.
class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = []
  static isTypeSupported = vi.fn(() => true)
  state = 'inactive'
  mimeType: string
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  finalChunk = 'final'
  start = vi.fn(() => { this.state = 'recording' })
  stop = vi.fn(() => {
    this.state = 'inactive'
    queueMicrotask(() => {
      this.emit(this.finalChunk)
      this.onstop?.()
    })
  })
  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? 'audio/webm'
    FakeMediaRecorder.instances.push(this)
  }
  emit(text: string) { this.ondataavailable?.({ data: new Blob([text]) }) }
}

type Track = { stop: ReturnType<typeof vi.fn>; onended: (() => void) | null }
function makeStream() {
  const track: Track = { stop: vi.fn(), onended: null }
  const stream = {
    getTracks: () => [track], getAudioTracks: () => [track],
  } as unknown as MediaStream
  return { stream, track }
}
const transcript: VoiceTranscript = {
  text: 'Clean text.',
  segments: [{ start_ms: 0, end_ms: 500, text: 'Clean text.' }], timing: 'provider',
}
const options = { workspaceId: 'workspace-1', model: 'openai/whisper-1' }
const pointed = {
  pointers: [{ t_ms: 100, event: 'move' as const, x: 1, y: 2, target: { kind: 'image' as const, path: '/workspace/a.png', width: 10, height: 10 } }],
  frames: [],
}
let now = 0
let getUserMedia: ReturnType<typeof vi.fn>
let media: ReturnType<typeof makeStream>
let wrappers: VueWrapper[] = []
let sessions: ReturnType<typeof useVoiceInput>[] = []

function create() {
  const onResult = vi.fn()
  let voice!: ReturnType<typeof useVoiceInput>
  wrappers.push(mount(defineComponent({
    setup() { voice = useVoiceInput(onResult); return () => null },
  })))
  sessions.push(voice)
  return { voice, onResult }
}
async function settle() {
  // Flush async stop, conversion and provider continuations without real sleeps.
  for (let index = 0; index < 12; index++) await Promise.resolve()
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  now = 0
  vi.clearAllMocks()
  vi.mocked(monitorVoiceAudio).mockReturnValue(null)
  FakeMediaRecorder.instances = []
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
  media = makeStream()
  getUserMedia = vi.fn().mockResolvedValue(media.stream)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  vi.mocked(fileToBase64).mockResolvedValue('encoded-audio')
  vi.mocked(transcribeVoice).mockResolvedValue(transcript)
  vi.mocked(saveVoiceRecording).mockResolvedValue({ path: '/workspace/.opencuria/recordings/id/manifest.json' })
  // Plain dictation by default: the passive observer saw no pointing.
  vi.mocked(startPointing).mockReturnValue({
    stop: vi.fn().mockResolvedValue(null), cancel: vi.fn(), audioActivity: vi.fn(),
  } as unknown as ReturnType<typeof startPointing>)
})
afterEach(() => {
  sessions.forEach((session) => session.cancel())
  sessions = []
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('voice browser capabilities', () => {
  it('records when microphone APIs are available even in a browser-approved insecure context without randomUUID', async () => {
    vi.stubGlobal('isSecureContext', false)
    const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto)
    vi.stubGlobal('crypto', { getRandomValues })
    expect(voiceSupported()).toBe(true)
    expect(voiceUnavailableReason()).toBe('')
    const { voice } = create()
    await voice.start(options)
    expect(voice.state.value).toBe('recording')
    expect(startPointing).toHaveBeenCalledWith('workspace-1', expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/), expect.any(Number))
  })

  it('explains the HTTP browser restriction before requesting microphone access', async () => {
    vi.stubGlobal('isSecureContext', false)
    vi.stubGlobal('navigator', {})
    expect(voiceSupported()).toBe(false)
    expect(voiceUnavailableReason()).toContain('http://localhost')
    const { voice } = create()
    await voice.start(options)
    expect(voice.error.value).toContain('browser blocks microphone')
    expect(voice.state.value).toBe('idle')
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('reports missing cryptography before reserving or opening the microphone', async () => {
    vi.stubGlobal('crypto', {})
    const { voice } = create()
    await voice.start(options)
    expect(voice.error.value).toContain('Secure voice recording IDs are unavailable')
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(voice.state.value).toBe('idle')
  })

  it('reports missing recording APIs without claiming HTTPS is required', () => {
    vi.stubGlobal('isSecureContext', true)
    vi.stubGlobal('MediaRecorder', undefined)
    expect(voiceSupported()).toBe(false)
    expect(voiceUnavailableReason()).toContain('MediaRecorder')
    expect(voiceUnavailableReason()).not.toContain('HTTPS')
  })
})

describe('useVoiceInput microphone lifecycle', () => {
  it('reports microphone activity for visuals while letting silence and low noise back off', async () => {
    const sample = vi.fn().mockReturnValue(0)
    vi.mocked(monitorVoiceAudio).mockReturnValue({ sample, hasSignal: () => true, close: vi.fn() })
    const { voice } = create()
    await voice.start(options)
    const observer = vi.mocked(startPointing).mock.results[0]!.value
    await vi.advanceTimersByTimeAsync(100)
    sample.mockReturnValue(0.01)
    await vi.advanceTimersByTimeAsync(100)
    expect(observer.audioActivity).not.toHaveBeenCalled()
    sample.mockReturnValue(0.03)
    await vi.advanceTimersByTimeAsync(100)
    expect(observer.audioActivity).toHaveBeenCalledOnce()
    sample.mockReturnValue(0.2)
    await vi.advanceTimersByTimeAsync(100)
    expect(observer.audioActivity).toHaveBeenCalledTimes(2)
    voice.cancel()
    await vi.advanceTimersByTimeAsync(100)
    expect(observer.audioActivity).toHaveBeenCalledTimes(2)
  })

  it.each(['unavailable', 'unmeasured'])('keeps visual cadence active when microphone energy is %s', async (status) => {
    if (status === 'unmeasured')
      vi.mocked(monitorVoiceAudio).mockReturnValue({ sample: () => 0, hasSignal: () => null, close: vi.fn() })
    const { voice } = create()
    await voice.start(options)
    const observer = vi.mocked(startPointing).mock.results[0]!.value
    await vi.advanceTimersByTimeAsync(100)
    expect(observer.audioActivity).toHaveBeenCalledOnce()
  })

  it('rejects microphone silence without sending audio to the model', async () => {
    const close = vi.fn()
    vi.mocked(monitorVoiceAudio).mockReturnValue({ sample: () => 0, hasSignal: () => false, close })
    const { voice, onResult } = create()
    await voice.start(options)
    now = 2500
    await voice.stop()
    expect(voice.error.value).toContain('No microphone audio')
    expect(transcribeVoice).not.toHaveBeenCalled()
    expect(onResult).not.toHaveBeenCalled()
    expect(close).toHaveBeenCalledOnce()
    expect(media.track.stop).toHaveBeenCalledOnce()
  })

  it('retains quiet audio and exposes signal level without changing recording timestamps', async () => {
    vi.mocked(monitorVoiceAudio).mockReturnValue({ sample: () => 0.1, hasSignal: () => true, close: vi.fn() })
    const { voice } = create()
    await voice.start(options)
    now = 600
    await vi.advanceTimersByTimeAsync(100)
    expect(voice.audioLevel.value).toBe(0.1)
    expect(voice.audioLevels.value).toEqual([0.1])
    await voice.stop()
    expect(transcribeVoice).toHaveBeenCalledWith('workspace-1', expect.objectContaining({ duration_ms: 600 }), expect.any(AbortSignal))
  })

  it('keeps a bounded level history for the waveform and clears it with the recording', async () => {
    let level = 0
    vi.mocked(monitorVoiceAudio).mockReturnValue({ sample: () => (level += 0.001), hasSignal: () => true, close: vi.fn() })
    const { voice } = create()
    await voice.start(options)
    await vi.advanceTimersByTimeAsync(100 * (VOICE_LEVEL_HISTORY + 20))
    expect(voice.audioLevels.value).toHaveLength(VOICE_LEVEL_HISTORY)
    expect(voice.audioLevels.value[VOICE_LEVEL_HISTORY - 1]).toBeCloseTo((VOICE_LEVEL_HISTORY + 20) * 0.001)
    voice.cancel()
    expect(voice.audioLevels.value).toEqual([])
  })

  it('reports permission denial and releases the shared microphone reservation', async () => {
    getUserMedia.mockRejectedValueOnce(new DOMException('Denied', 'NotAllowedError'))
    const { voice, onResult } = create()
    await voice.start(options)
    expect(voice.state.value).toBe('idle')
    expect(voice.error.value).toMatch(/denied/i)
    expect(onResult).not.toHaveBeenCalled()
    await voice.start(options)
    expect(voice.state.value).toBe('recording')
  })

  it('stops a late permission stream after cancellation without starting recording', async () => {
    const pending = deferred<MediaStream>()
    getUserMedia.mockReturnValueOnce(pending.promise)
    const { voice } = create()
    const starting = voice.start(options)
    expect(voice.state.value).toBe('requesting')
    voice.cancel()
    pending.resolve(media.stream)
    await starting
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(FakeMediaRecorder.instances).toHaveLength(0)
    expect(voice.state.value).toBe('idle')
  })

  it('collects the final stop chunk and inserts the transcript exactly once on repeated stop', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    const recorder = FakeMediaRecorder.instances[0]!
    recorder.emit('first')
    now = 1200
    await Promise.all([voice.stop(), voice.stop()])
    expect(recorder.stop).toHaveBeenCalledOnce()
    expect(media.track.stop).toHaveBeenCalledOnce()
    const file = vi.mocked(fileToBase64).mock.calls[0]![0]
    expect(file.size).toBe(new Blob(['firstfinal']).size)
    expect(transcribeVoice).toHaveBeenCalledWith('workspace-1', {
      audio_base64: 'encoded-audio', mime_type: 'audio/webm;codecs=opus',
      model: options.model, duration_ms: 1200,
    }, expect.any(AbortSignal))
    expect(onResult).toHaveBeenCalledExactlyOnceWith('Clean text.', undefined)
    expect(voice.audio.value).toBeNull()
    expect(voice.state.value).toBe('idle')
  })

  it('ignores provider results after cancellation and aborts the request', async () => {
    const pending = deferred<VoiceTranscript>()
    vi.mocked(transcribeVoice).mockReturnValueOnce(pending.promise)
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    const stopping = voice.stop()
    await settle()
    const signal = vi.mocked(transcribeVoice).mock.calls[0]![2]!
    voice.cancel()
    expect(signal.aborted).toBe(true)
    pending.resolve(transcript)
    await stopping
    expect(onResult).not.toHaveBeenCalled()
    expect(voice.state.value).toBe('idle')
  })

  it('automatically finishes at the configured duration boundary', async () => {
    const { voice, onResult } = create()
    await voice.start({ ...options, maxDurationMs: 1000 })
    now = 1100
    await vi.advanceTimersByTimeAsync(100)
    await settle()
    expect(transcribeVoice).toHaveBeenCalledOnce()
    expect(vi.mocked(transcribeVoice).mock.calls[0]![1].duration_ms).toBe(1000)
    expect(onResult).toHaveBeenCalledOnce()
  })

  it('finishes recording if the microphone track ends', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1500
    media.track.onended?.()
    await settle()
    expect(FakeMediaRecorder.instances[0]!.stop).toHaveBeenCalledOnce()
    expect(onResult).toHaveBeenCalledOnce()
    expect(media.track.onended).toBeNull()
  })

  it('keeps recorded audio after a network failure and retries without microphone access', async () => {
    vi.mocked(transcribeVoice).mockRejectedValueOnce(new Error('Provider unavailable'))
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    await voice.stop()
    expect(voice.state.value).toBe('error')
    expect(voice.error.value).toContain('Provider unavailable')
    expect(voice.audio.value?.size).toBeGreaterThan(0)
    expect(onResult).not.toHaveBeenCalled()
    await voice.retry()
    expect(getUserMedia).toHaveBeenCalledOnce()
    expect(transcribeVoice).toHaveBeenCalledTimes(2)
    expect(onResult).toHaveBeenCalledOnce()
  })

  it('does not duplicate paid provider requests when retry is triggered twice', async () => {
    const pending = deferred<VoiceTranscript>()
    vi.mocked(transcribeVoice)
      .mockRejectedValueOnce(new Error('Connection lost'))
      .mockReturnValueOnce(pending.promise)
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    await voice.stop()
    const first = voice.retry()
    const second = voice.retry()
    await settle()
    expect(transcribeVoice).toHaveBeenCalledTimes(2)
    pending.resolve(transcript)
    await Promise.all([first, second])
    expect(onResult).toHaveBeenCalledOnce()
  })

  it('aborts timed-out processing while preserving audio for retry', async () => {
    vi.mocked(transcribeVoice).mockImplementationOnce((_workspace, _payload, signal) =>
      new Promise((_resolve, reject) => {
        signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      }),
    )
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    const stopping = voice.stop()
    await settle()
    const signal = vi.mocked(transcribeVoice).mock.calls[0]![2]!
    await vi.advanceTimersByTimeAsync(179_999)
    expect(signal.aborted).toBe(false)
    expect(voice.state.value).toBe('processing')
    await vi.advanceTimersByTimeAsync(1)
    await stopping
    expect(signal.aborted).toBe(true)
    expect(voice.state.value).toBe('error')
    expect(voice.error.value).toMatch(/timed out/i)
    expect(voice.audio.value?.size).toBeGreaterThan(0)
    expect(onResult).not.toHaveBeenCalled()
    await voice.retry()
    expect(getUserMedia).toHaveBeenCalledOnce()
    expect(onResult).toHaveBeenCalledOnce()
  })

  it('cleans up if the browser never emits its final recorder stop event', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    const recorder = FakeMediaRecorder.instances[0]!
    recorder.emit('unfinished-audio')
    recorder.stop.mockImplementation(() => { recorder.state = 'inactive' })
    now = 1000
    const stopping = voice.stop()
    await vi.advanceTimersByTimeAsync(10_000)
    await stopping
    expect(voice.state.value).toBe('idle')
    expect(voice.error.value).toMatch(/could not finish/i)
    expect(voice.audio.value).toBeNull()
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(transcribeVoice).not.toHaveBeenCalled()
    expect(onResult).not.toHaveBeenCalled()
    await voice.start(options)
    expect(voice.state.value).toBe('recording')
  })

  it('reserves the recording owner through processing and recoverable errors', async () => {
    const pending = deferred<VoiceTranscript>()
    vi.mocked(transcribeVoice).mockReturnValueOnce(pending.promise)
    const first = create()
    const second = create()
    await first.voice.start(options)
    now = 1000
    const stopping = first.voice.stop()
    await settle()
    expect(media.track.stop).toHaveBeenCalledOnce()
    await second.voice.start(options)
    expect(second.voice.error.value).toMatch(/already active/i)
    expect(getUserMedia).toHaveBeenCalledOnce()
    pending.reject(new Error('Connection unavailable'))
    await stopping
    await second.voice.start(options)
    expect(second.voice.state.value).toBe('idle')
    expect(getUserMedia).toHaveBeenCalledOnce()
    first.voice.cancel()
    await second.voice.start(options)
    expect(second.voice.state.value).toBe('recording')
    expect(getUserMedia).toHaveBeenCalledTimes(2)
  })

  it.each([true, false])('reports missing phrase timing only for pointing recordings (pointed=%s)', async (didPoint) => {
    vi.mocked(transcribeVoice).mockResolvedValueOnce({ ...transcript, timing: 'approximate' })
    vi.mocked(startPointing).mockReturnValueOnce({
      stop: vi.fn().mockResolvedValue(didPoint ? pointed : null), cancel: vi.fn(), audioActivity: vi.fn(),
    })
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    await voice.stop()
    expect(onResult).toHaveBeenCalledOnce()
    expect(voice.state.value).toBe('idle')
    if (didPoint) expect(voice.warning.value).toMatch(/approximate speech timing/i)
    else expect(voice.warning.value).toBe('')
  })

  it('always observes pointing passively but saves nothing when nothing was pointed at', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    expect(startPointing).toHaveBeenCalledOnce()
    now = 1000
    await voice.stop()
    expect(saveVoiceRecording).not.toHaveBeenCalled()
    expect(onResult).toHaveBeenCalledExactlyOnceWith('Clean text.', undefined)
  })

  it('keeps dictation available when the pointing observer cannot start', async () => {
    vi.mocked(startPointing).mockImplementationOnce(() => {
      throw new Error('A pointing recording is already active.')
    })
    const { voice, onResult } = create()
    await voice.start(options)
    expect(voice.state.value).toBe('recording')
    now = 1000
    await voice.stop()
    expect(onResult).toHaveBeenCalledExactlyOnceWith('Clean text.', undefined)
  })

  it('retries pointing persistence using the cached transcript', async () => {
    vi.mocked(saveVoiceRecording).mockRejectedValueOnce(new Error('Workspace offline'))
    vi.mocked(startPointing).mockReturnValueOnce({
      stop: vi.fn().mockResolvedValue(pointed), cancel: vi.fn(), audioActivity: vi.fn(),
    })
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    await voice.stop()
    expect(voice.state.value).toBe('error')
    expect(voice.audio.value).not.toBeNull()
    expect(onResult).not.toHaveBeenCalled()
    await voice.retry()
    expect(transcribeVoice).toHaveBeenCalledOnce()
    expect(saveVoiceRecording).toHaveBeenCalledTimes(2)
    expect(startPointing).toHaveBeenCalledOnce()
    expect(onResult).toHaveBeenCalledExactlyOnceWith(
      'Clean text.', '/workspace/.opencuria/recordings/id/manifest.json',
    )
  })

  it('cancels a visual upload still draining after microphone stop', async () => {
    const pending = deferred<{ pointers: []; frames: [] }>()
    const cancelPointing = vi.fn(() => pending.reject(new Error('Cancelled')))
    vi.mocked(startPointing).mockReturnValueOnce({
      stop: vi.fn(() => pending.promise), cancel: cancelPointing, audioActivity: vi.fn(),
    })
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    const stopping = voice.stop()
    await settle()
    voice.cancel()
    expect(cancelPointing).toHaveBeenCalledOnce()
    await stopping
    expect(voice.state.value).toBe('idle')
    expect(transcribeVoice).not.toHaveBeenCalled()
    expect(onResult).not.toHaveBeenCalled()
  })

  it('preserves dictation after visual failure and only inserts it after an explicit fallback choice', async () => {
    vi.mocked(startPointing).mockReturnValueOnce({
      stop: vi.fn().mockRejectedValue(new Error('Desktop disconnected')), cancel: vi.fn(), audioActivity: vi.fn(),
    })
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    await voice.stop()
    expect(voice.state.value).toBe('error')
    expect(voice.pointingFailed.value).toBe(true)
    expect(voice.error.value).toContain('Desktop disconnected')
    expect(voice.audio.value).not.toBeNull()
    expect(transcribeVoice).toHaveBeenCalledOnce()
    expect(onResult).not.toHaveBeenCalled()
    await voice.useDictationOnly()
    expect(transcribeVoice).toHaveBeenCalledOnce()
    expect(saveVoiceRecording).not.toHaveBeenCalled()
    expect(onResult).toHaveBeenCalledExactlyOnceWith('Clean text.', undefined)
  })

  it('releases tracks and prevents insertion when the composer unmounts', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    wrappers[0]!.unmount()
    await settle()
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(onResult).not.toHaveBeenCalled()
    expect(transcribeVoice).not.toHaveBeenCalled()
  })

  it('prevents two composers from competing for the microphone', async () => {
    const first = create()
    const second = create()
    await first.voice.start(options)
    await second.voice.start(options)
    expect(getUserMedia).toHaveBeenCalledOnce()
    expect(second.voice.error.value).toMatch(/already active/i)
    first.voice.cancel()
    await second.voice.start(options)
    expect(second.voice.state.value).toBe('recording')
  })

  it('cleans up recorder errors and permits a fresh recording', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    FakeMediaRecorder.instances[0]!.onerror?.()
    await settle()
    expect(voice.error.value).toMatch(/recording failed/i)
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(voice.state.value).toBe('idle')
    expect(onResult).not.toHaveBeenCalled()
    await voice.start(options)
    expect(voice.state.value).toBe('recording')
  })

  it('stops on tab hiding to avoid claiming continuous visual observations', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1500
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(onResult).toHaveBeenCalledOnce()
    expect(media.track.stop).toHaveBeenCalledOnce()
  })

  it('rejects empty final audio without sending an empty provider request', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    FakeMediaRecorder.instances[0]!.finalChunk = ''
    now = 1500
    await voice.stop()
    expect(voice.error.value).toMatch(/too short/i)
    expect(transcribeVoice).not.toHaveBeenCalled()
    expect(onResult).not.toHaveBeenCalled()
  })

  it('rejects recordings shorter than 200 ms without calling the provider', async () => {
    const { voice, onResult } = create()
    await voice.start(options)
    now = 199
    await voice.stop()
    expect(voice.error.value).toMatch(/too short/i)
    expect(voice.state.value).toBe('idle')
    expect(transcribeVoice).not.toHaveBeenCalled()
    expect(onResult).not.toHaveBeenCalled()
  })

  it('drops oversized audio and releases the microphone', async () => {
    const { voice } = create()
    await voice.start({ ...options, maxAudioBytes: 4 })
    FakeMediaRecorder.instances[0]!.emit('oversized')
    await settle()
    expect(voice.error.value).toMatch(/size limit/i)
    expect(voice.audio.value).toBeNull()
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(transcribeVoice).not.toHaveBeenCalled()
  })

  it('applies the audio byte limit independently to each consecutive recording', async () => {
    const { voice, onResult } = create()
    await voice.start({ ...options, maxAudioBytes: 12 })
    FakeMediaRecorder.instances[0]!.emit('123456')
    now = 1000
    await voice.stop()
    expect(onResult).toHaveBeenCalledOnce()
    await voice.start({ ...options, maxAudioBytes: 12 })
    now = 2000
    await voice.stop()
    expect(voice.error.value).toBe('')
    expect(onResult).toHaveBeenCalledTimes(2)
  })

  it('does not let a cancelled request seed a later recording with its stale transcript', async () => {
    const pending = deferred<VoiceTranscript>()
    vi.mocked(transcribeVoice).mockReturnValueOnce(pending.promise)
    const { voice, onResult } = create()
    await voice.start(options)
    now = 1000
    const oldStop = voice.stop()
    await settle()
    voice.cancel()
    await voice.start(options)
    pending.resolve({ ...transcript, text: 'Old cancelled words.' })
    await oldStop
    now = 2000
    await voice.stop()
    expect(transcribeVoice).toHaveBeenCalledTimes(2)
    expect(onResult).toHaveBeenCalledExactlyOnceWith('Clean text.', undefined)
  })
})
