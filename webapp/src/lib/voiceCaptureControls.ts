/** One recording can be controlled from the composer or an open viewer dialog. */
import { shallowRef } from 'vue'

export interface VoiceCaptureControls {
  workspaceId: string
  state: 'requesting' | 'recording' | 'processing' | 'error'
  elapsedMs: number
  pointing: boolean
  stop: () => void
  cancel: () => void
}
export const voiceCaptureControls = shallowRef<VoiceCaptureControls | null>(null)
