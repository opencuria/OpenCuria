/** One recording can be controlled from the composer or an open viewer dialog. */
import { shallowRef } from 'vue'

/** Number of recent 100 ms microphone levels kept for the live waveform. */
export const VOICE_LEVEL_HISTORY = 160

export interface VoiceCaptureControls {
  workspaceId: string
  state: 'requesting' | 'recording' | 'processing' | 'error'
  /** Recent microphone levels (0..1), oldest first, for the live waveform. */
  levels: readonly number[]
  /** Finish recording and insert the transcript into the composer for editing. */
  stop: () => void
  /** Finish recording and send the transcript as soon as it is ready. */
  send: () => void
  /** Discard the recording without transcribing it. */
  cancel: () => void
}
export const voiceCaptureControls = shallowRef<VoiceCaptureControls | null>(null)
