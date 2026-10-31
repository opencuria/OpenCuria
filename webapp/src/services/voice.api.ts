/** Workspace-scoped voice input; credentials and model calls stay server-side. */
import { get, post } from './api'
import type { PointingResult } from '@/lib/voicePointing'

export interface VoiceConfig {
  available: boolean
  reason?: string
  models: { id: string; name: string }[]
  default_model: string
  cleanup_model: string
  max_duration_ms: number
  max_audio_bytes: number
}
export interface VoiceSegment {
  start_ms: number
  end_ms: number
  text: string
  raw_text?: string
}
export interface VoiceTranscript {
  text: string
  segments: VoiceSegment[]
  timing: 'provider' | 'approximate'
  cleanup_warning?: string
}
export function getVoiceConfig(workspaceId: string, signal?: AbortSignal): Promise<VoiceConfig> {
  return get(`/workspaces/${workspaceId}/voice/config/`, signal)
}
export function transcribeVoice(
  workspaceId: string,
  payload: { audio_base64: string; mime_type: string; model: string; duration_ms: number },
  signal?: AbortSignal,
): Promise<VoiceTranscript> {
  return post(`/workspaces/${workspaceId}/voice/transcribe/`, payload, signal)
}
export function saveVoiceRecording(
  workspaceId: string,
  payload: VoiceTranscript & PointingResult & { recording_id: string; duration_ms: number },
  signal?: AbortSignal,
): Promise<{ path: string }> {
  return post(`/workspaces/${workspaceId}/voice/recordings/`, payload, signal)
}
