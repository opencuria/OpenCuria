import { VOICE_TRANSCRIBE_TIMEOUT_MS } from "@opencuria/chat-core/voiceInput";
import type { ApiClient } from "./api";
import type {
  VoiceRecordingInput,
  VoiceSettings,
  VoiceTranscribeInput,
  VoiceTranscript,
} from "./types";

function segment(value: string): string {
  return encodeURIComponent(value);
}

/** Workspace-scoped dictation; credentials and model calls stay server-side. */
export function createVoiceApi(getApi: () => ApiClient) {
  return {
    /** Whether dictation is set up, and the recording limits. */
    config: (workspaceId: string, signal?: AbortSignal) =>
      getApi().get<VoiceSettings>(
        `/workspaces/${segment(workspaceId)}/voice/config/`,
        signal,
      ),
    /**
     * Transcribe and clean one recording. Speech models and cleanup may take
     * minutes; the caller's signal usually ends the attempt first.
     */
    transcribe: (
      workspaceId: string,
      input: VoiceTranscribeInput,
      signal?: AbortSignal,
    ) =>
      getApi().post<VoiceTranscript>(
        `/workspaces/${segment(workspaceId)}/voice/transcribe/`,
        input,
        signal,
        VOICE_TRANSCRIBE_TIMEOUT_MS + 10_000,
      ),
    /** Save a pointing manifest in the workspace; returns its path. */
    saveRecording: (
      workspaceId: string,
      input: VoiceRecordingInput,
      signal?: AbortSignal,
    ) =>
      getApi().post<{ path: string }>(
        `/workspaces/${segment(workspaceId)}/voice/recordings/`,
        input,
        signal,
      ),
  };
}
