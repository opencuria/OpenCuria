/** Workspace-scoped voice input; credentials and model calls stay server-side. */
import { createAgentSettingsApi } from '@opencuria/client/settings'
import { createVoiceApi } from '@opencuria/client/voice'
import type { VoiceRecordingInput, VoiceSettings, VoiceTranscript } from '@opencuria/client'
import { getApiClient } from './api'

export type { VoiceSegment, VoiceTranscript } from '@opencuria/client'
export type VoiceConfig = VoiceSettings

/** Organization voice capabilities for provider settings, without a workspace. */
export const { getVoiceSettings } = createAgentSettingsApi(getApiClient)
const voice = createVoiceApi(getApiClient)

export function getVoiceConfig(workspaceId: string, signal?: AbortSignal): Promise<VoiceConfig> {
  return voice.config(workspaceId, signal)
}
export function transcribeVoice(
  workspaceId: string,
  payload: { audio_base64: string; mime_type: string; model: string; duration_ms: number },
  signal?: AbortSignal,
): Promise<VoiceTranscript> {
  return voice.transcribe(workspaceId, payload, signal)
}
export function saveVoiceRecording(
  workspaceId: string,
  payload: VoiceRecordingInput,
  signal?: AbortSignal,
): Promise<{ path: string }> {
  return voice.saveRecording(workspaceId, payload, signal)
}
