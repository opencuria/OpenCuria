import { File, UploadType } from "expo-file-system";
import {
  recordingFramesDirectory,
  visualRecordingInput,
  type PointingResult,
} from "@opencuria/chat-core/visualRecording";
import type { VoiceTranscriptText } from "@opencuria/chat-core/voiceTranscriptions";
import type { OpenCuriaClient } from "@opencuria/client";
import type { ChatVoiceClip, VoiceClipFiles } from "./voiceClipStore";

/** The frames and timeline a recording kept next to its audio. */
export async function readVisualClip(
  files: VoiceClipFiles,
  clip: ChatVoiceClip,
): Promise<{
  result: PointingResult;
  frameUri: (path: string) => string;
} | null> {
  const raw = await files.visualTimeline(clip).catch(() => null);
  if (!raw) return null;
  let result: PointingResult;
  try {
    result = JSON.parse(raw) as PointingResult;
  } catch {
    return null;
  }
  if (!Array.isArray(result?.pointers) || !Array.isArray(result.frames))
    return null;
  const directory = await files.visualUri(clip);
  return {
    result,
    frameUri: (path) => files.visualFrameUri(directory, path),
  };
}

function uploadError(status: number, body: string): Error {
  let detail = "";
  try {
    const payload = JSON.parse(body) as { detail?: unknown };
    if (typeof payload.detail === "string") detail = payload.detail;
  } catch {
    // Not JSON.
  }
  return Object.assign(
    new Error(
      detail ||
        `A frame of the app recording could not be uploaded (${status}).`,
    ),
    { status },
  );
}

/**
 * Upload a recording's frames (an app's screen; pictures and files need
 * none) into the workspace and save its timeline beside the transcript;
 * resolves to the transcript with the timeline's path. Frames are replaced on
 * a retry, so a repeated attempt is harmless. A clip whose timeline is gone
 * keeps its text without one.
 */
export async function attachVisualClip(
  client: OpenCuriaClient,
  files: VoiceClipFiles,
  clip: ChatVoiceClip,
  transcript: VoiceTranscriptText,
  signal: AbortSignal,
): Promise<VoiceTranscriptText> {
  const visual = clip.visual;
  if (!visual) return transcript;
  const stored = await readVisualClip(files, clip);
  if (!stored) return transcript;
  const directory = recordingFramesDirectory(visual.recordingId);
  const frames: PointingResult["frames"] = [];
  for (const frame of stored.result.frames) {
    if (signal.aborted) throw signal.reason ?? new Error("Cancelled.");
    const file = new File(stored.frameUri(frame.path));
    if (!file.exists) continue;
    const target = await client.files.createUpload(
      clip.workspaceId,
      {
        directory,
        filename: frame.path.slice(frame.path.lastIndexOf("/") + 1),
        size: file.size,
        conflict: "replace",
      },
      signal,
    );
    const response = await file.upload(target.uploadUrl, {
      httpMethod: "PUT",
      uploadType: UploadType.BINARY_CONTENT,
      headers: { "Content-Type": "image/jpeg" },
    });
    if (response.status !== 201)
      throw uploadError(response.status, response.body);
    frames.push(frame);
  }
  const saved = await client.voice.saveRecording(
    clip.workspaceId,
    visualRecordingInput({
      recordingId: visual.recordingId,
      durationMs: clip.durationMs,
      transcript: {
        text: transcript.text,
        raw_text: transcript.raw_text,
        segments: transcript.segments ?? [],
        timing: transcript.timing ?? "approximate",
        cleanup_warning: transcript.cleanup_warning,
        speech_model: transcript.speech_model,
        cleanup_model: transcript.cleanup_model,
      },
      result: { ...stored.result, frames },
    }),
    signal,
  );
  return { ...transcript, recording_path: saved.path };
}
