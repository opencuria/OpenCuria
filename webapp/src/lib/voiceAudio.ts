/** Observe microphone energy without changing or trimming the recorded waveform. */
export interface VoiceAudioMonitor {
  sample(): number
  hasSignal(): boolean | null
  close(): void
}

/** Silence gating is deliberately conservative; unsupported/suspended meters opt out. */
export function monitorVoiceAudio(stream: MediaStream): VoiceAudioMonitor | null {
  if (typeof AudioContext === 'undefined') return null
  let context: AudioContext | null = null
  try {
    context = new AudioContext()
    const source = context.createMediaStreamSource(stream)
    const analyser = context.createAnalyser()
    analyser.fftSize = 2048
    source.connect(analyser)
    const samples = new Float32Array(analyser.fftSize)
    let measured = false
    let signal = false
    let closed = false
    // No output connection: never play the user's microphone back to them.
    void context.resume().catch(() => undefined)
    return {
      sample() {
        if (closed || context!.state !== 'running') return 0
        analyser.getFloatTimeDomainData(samples)
        measured = true
        const rms = Math.sqrt(
          samples.reduce((sum, value) => sum + value * value, 0) / samples.length,
        )
        // Only reject near-digital silence. This is not a speech classifier and
        // must not drop quiet speech or change alignment with desktop events.
        if (rms >= 0.00001) signal = true
        return Math.min(1, rms * 12)
      },
      hasSignal: () => (measured ? signal : null),
      close() {
        if (closed) return
        closed = true
        source.disconnect()
        analyser.disconnect()
        void context!.close().catch(() => undefined)
      },
    }
  } catch {
    if (context) void context.close().catch(() => undefined)
    return null
  }
}
