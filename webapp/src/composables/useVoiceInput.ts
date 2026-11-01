/** Bounded, cancellable microphone lifecycle shared by the voice composer UI. */
import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'
import { fileToBase64 } from '@/lib/chatUpload'
import { createSecureUuid } from '@/lib/secureUuid'
import { monitorVoiceAudio, type VoiceAudioMonitor } from '@/lib/voiceAudio'
import { VOICE_LEVEL_HISTORY } from '@/lib/voiceCaptureControls'
import { startPointing, type PointingResult } from '@/lib/voicePointing'
import { saveVoiceRecording, transcribeVoice, type VoiceTranscript } from '@/services/voice.api'

const MAX_DURATION = 300_000
const MAX_BYTES = 1_500_000
// The meter scales RMS by 12; ignore low background energy when pacing visuals.
const VISUAL_AUDIO_ACTIVITY_LEVEL = 0.03
let microphoneOwner: symbol | null = null

export function voiceSupported(): boolean {
  return (
    typeof globalThis.navigator?.mediaDevices?.getUserMedia === 'function' &&
    typeof MediaRecorder !== 'undefined'
  )
}

/** Explain browser capabilities without imposing a protocol restriction in the app. */
export function voiceUnavailableReason(): string {
  if (voiceSupported()) return ''
  if (
    globalThis.isSecureContext === false &&
    typeof globalThis.navigator?.mediaDevices?.getUserMedia !== 'function'
  ) {
    return 'Your browser blocks microphone access at this HTTP address. Open OpenCuria via http://localhost or allow this origin in your browser settings.'
  }
  return 'This browser does not provide microphone recording. Use a browser with getUserMedia and MediaRecorder support.'
}

export function useVoiceInput(onResult: (text: string, path?: string) => void) {
  const state = ref<'idle' | 'requesting' | 'recording' | 'processing' | 'error'>('idle')
  const error = ref('')
  const warning = ref('')
  const elapsedMs = ref(0)
  const audioLevel = ref(0)
  const audioLevels = shallowRef<readonly number[]>([])
  const audio = shallowRef<Blob | null>(null)
  const pointingFailed = ref(false)
  let visualError: Error | null = null
  const busy = computed(() => state.value !== 'idle')
  const owner = Symbol('voice')
  let generation = 0
  let stream: MediaStream | null = null
  let audioMonitor: VoiceAudioMonitor | null = null
  let recorder: MediaRecorder | null = null
  let timer: ReturnType<typeof setInterval> | null = null
  let controller: AbortController | null = null
  let pointing: ReturnType<typeof startPointing> | null = null
  let pointingResult: PointingResult | null = null
  let transcript: VoiceTranscript | null = null
  let workspaceId = ''
  let model = ''
  let recordingId = ''
  let origin = 0
  let durationMs = 0
  let limitDuration = MAX_DURATION
  let limitBytes = MAX_BYTES
  let chunks: Blob[] = []
  let bytes = 0
  let stopped: Promise<void> | null = null
  let resolveStopped: (() => void) | null = null

  function releaseMicrophone(): void {
    if (timer) clearInterval(timer)
    timer = null
    document.removeEventListener('visibilitychange', onVisibility)
    stream?.getTracks().forEach((track) => {
      track.onended = null
      track.stop()
    })
    stream = null
    audioMonitor?.close()
    audioMonitor = null
    audioLevel.value = 0
  }

  function sampleAudio(): void {
    audioLevel.value = audioMonitor?.sample() ?? 0
    audioLevels.value = [...audioLevels.value.slice(1 - VOICE_LEVEL_HISTORY), audioLevel.value]
    // Unknown audio must not be interpreted as silence. This only controls
    // screenshot cadence; the complete microphone waveform is always retained.
    if (
      !audioMonitor ||
      audioMonitor.hasSignal() === null ||
      audioLevel.value >= VISUAL_AUDIO_ACTIVITY_LEVEL
    )
      pointing?.audioActivity()
  }

  function cancel(): void {
    generation++
    controller?.abort()
    controller = null
    pointing?.cancel()
    pointing = null
    if (recorder) {
      recorder.ondataavailable = null
      recorder.onstop = null
      recorder.onerror = null
      if (recorder.state !== 'inactive') recorder.stop()
    }
    resolveStopped?.()
    resolveStopped = null
    recorder = null
    releaseMicrophone()
    if (microphoneOwner === owner) microphoneOwner = null
    chunks = []
    bytes = 0
    audio.value = null
    audioLevels.value = []
    transcript = null
    pointingResult = null
    visualError = null
    pointingFailed.value = false
    state.value = 'idle'
    error.value = ''
    warning.value = ''
  }

  function onVisibility(): void {
    // Hidden tabs throttle pointer/frame timers. Finish at the actual boundary.
    if (document.visibilityState === 'hidden' && state.value === 'recording') void stop()
  }

  async function start(options: {
    workspaceId: string
    model: string
    maxDurationMs?: number
    maxAudioBytes?: number
  }): Promise<void> {
    if (state.value !== 'idle') return
    if (microphoneOwner) {
      error.value = 'Another voice recording is already active.'
      return
    }
    if (!voiceSupported()) {
      error.value = voiceUnavailableReason()
      return
    }
    cancel()
    const id = createSecureUuid()
    if (!id) {
      error.value = 'Secure voice recording IDs are unavailable in this browser.'
      return
    }
    const current = generation
    microphoneOwner = owner
    workspaceId = options.workspaceId
    model = options.model
    limitDuration = Math.min(options.maxDurationMs || MAX_DURATION, MAX_DURATION)
    limitBytes = Math.min(options.maxAudioBytes || MAX_BYTES, MAX_BYTES)
    state.value = 'requesting'
    try {
      const acquired = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      if (current !== generation) {
        acquired.getTracks().forEach((track) => track.stop())
        return
      }
      stream = acquired
      audioMonitor = monitorVoiceAudio(acquired)
      const mimeType = [
        'audio/webm;codecs=opus',
        'audio/mp4',
        'audio/ogg;codecs=opus',
        'audio/webm',
      ].find((mime) => MediaRecorder.isTypeSupported(mime))
      recorder = new MediaRecorder(acquired, {
        ...(mimeType ? { mimeType } : {}),
        audioBitsPerSecond: 64_000,
      })
      recordingId = id
      origin = performance.now()
      durationMs = 0
      elapsedMs.value = 0
      stopped = new Promise<void>((resolve) => {
        resolveStopped = resolve
      })
      recorder.ondataavailable = (event) => {
        if (current !== generation || !event.data.size) return
        bytes += event.data.size
        if (bytes > limitBytes) {
          cancel()
          error.value =
            'The recording exceeded the audio size limit. Please record a shorter message.'
          return
        }
        chunks.push(event.data)
        if (bytes >= limitBytes * 0.9 && state.value === 'recording') void stop()
      }
      recorder.onstop = () => {
        resolveStopped?.()
        resolveStopped = null
      }
      recorder.onerror = () => {
        cancel()
        error.value = 'The microphone recording failed. Please try again.'
      }
      try {
        // Passive until the speaker points at an image or desktop; never blocks dictation.
        pointing = startPointing(workspaceId, recordingId, origin)
      } catch {
        pointing = null
      }
      recorder.start(1000)
      state.value = 'recording'
      acquired.getAudioTracks().forEach((track) => {
        track.onended = () => {
          if (state.value === 'recording') void stop()
        }
      })
      document.addEventListener('visibilitychange', onVisibility)
      timer = setInterval(() => {
        sampleAudio()
        elapsedMs.value = Math.min(Math.round(performance.now() - origin), limitDuration)
        if (elapsedMs.value >= limitDuration) void stop()
      }, 100)
    } catch (cause) {
      if (current !== generation) return
      cancel()
      error.value =
        cause instanceof DOMException && cause.name === 'NotAllowedError'
          ? 'Microphone access was denied. Allow it in your browser settings and try again.'
          : 'The microphone could not be opened. Check that it is connected and available.'
    }
  }

  async function process(): Promise<void> {
    if (!audio.value || !workspaceId) return
    const current = generation
    state.value = 'processing'
    error.value = ''
    const request = new AbortController()
    controller = request
    const requestTimer = setTimeout(() => request.abort(), 180_000)
    try {
      if (!transcript) {
        const encoded = await fileToBase64(
          new File([audio.value], 'dictation', { type: audio.value.type }),
        )
        if (current !== generation) return
        const result = await transcribeVoice(
          workspaceId,
          {
            audio_base64: encoded,
            mime_type: audio.value.type,
            model,
            duration_ms: durationMs,
          },
          request.signal,
        )
        if (current !== generation) return
        transcript = result
      }
      if (current !== generation) return
      if (visualError) throw visualError
      let path: string | undefined
      if (pointingResult) {
        const saved = await saveVoiceRecording(
          workspaceId,
          {
            ...transcript,
            ...pointingResult,
            recording_id: recordingId,
            duration_ms: durationMs,
          },
          request.signal,
        )
        path = saved.path
      }
      if (current !== generation) return
      const text = transcript.text
      const cleanupWarning = [
        transcript.cleanup_warning,
        pointingResult && transcript.timing === 'approximate'
          ? 'The speech model returned no phrase timestamps. The pointing recording has only approximate speech timing.'
          : '',
      ]
        .filter(Boolean)
        .join(' ')
      cancel()
      warning.value = cleanupWarning
      onResult(text, path)
    } catch (cause) {
      if (current !== generation) return
      state.value = 'error'
      error.value = request.signal.aborted
        ? 'Voice input processing timed out. Retry or discard the recording.'
        : cause instanceof Error
          ? cause.message
          : 'Voice input could not be processed. Retry or discard the recording.'
    } finally {
      clearTimeout(requestTimer)
    }
  }

  async function stop(): Promise<void> {
    if (state.value !== 'recording' || !recorder) return
    const current = generation
    state.value = 'processing'
    audioMonitor?.sample()
    const hasAudioSignal = audioMonitor?.hasSignal()
    const mime = recorder.mimeType
    // Freeze visual input before waiting for MediaRecorder's final asynchronous data.
    const visual = pointing
    const points = visual?.stop().catch((cause: unknown) => {
      if (current !== generation) return undefined
      visualError =
        cause instanceof Error ? cause : new Error('The visual recording could not be saved.')
      pointingFailed.value = true
      return undefined
    })
    durationMs = Math.min(Math.round(performance.now() - origin), limitDuration)
    elapsedMs.value = durationMs
    let finalizeTimer: ReturnType<typeof setTimeout> | undefined
    const finalized = Promise.race([
      stopped,
      new Promise<never>((_, reject) => {
        finalizeTimer = setTimeout(
          () => reject(new Error('The browser could not finish the recording. Please try again.')),
          10_000,
        )
      }),
    ])
    try {
      recorder.stop()
      releaseMicrophone()
      const [, result] = await Promise.all([finalized, points])
      clearTimeout(finalizeTimer)
      if (current !== generation) return
      if (pointing === visual) pointing = null
      pointingResult = result ?? null
      audio.value = new Blob(chunks, { type: mime })
      chunks = []
      recorder = null
      if (!audio.value.size || durationMs < 200) {
        cancel()
        error.value = 'The recording was too short. Please try again.'
        return
      }
      if (hasAudioSignal === false) {
        cancel()
        error.value = 'No microphone audio was detected. Check your input device and try again.'
        return
      }
      await process()
    } catch (cause) {
      if (current !== generation) return
      // A partial pointing capture must never be presented as a complete one.
      cancel()
      error.value =
        cause instanceof Error
          ? cause.message
          : 'The pointing recording could not be saved. Please try again.'
    } finally {
      clearTimeout(finalizeTimer)
    }
  }

  async function useDictationOnly(): Promise<void> {
    if (!pointingFailed.value || state.value !== 'error') return
    visualError = null
    pointingResult = null
    pointingFailed.value = false
    await process()
  }

  async function retry(): Promise<void> {
    if (state.value !== 'error' || pointingFailed.value) return
    await process()
  }

  onBeforeUnmount(cancel)
  return {
    state,
    error,
    warning,
    elapsedMs,
    audioLevel,
    audioLevels,
    audio,
    busy,
    pointingFailed,
    start,
    stop,
    retry,
    useDictationOnly,
    cancel,
  }
}
