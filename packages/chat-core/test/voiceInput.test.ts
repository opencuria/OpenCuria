import { describe, expect, it } from "vitest";
import {
  VOICE_LEVEL_HISTORY,
  VOICE_MAX_AUDIO_BYTES,
  VOICE_MAX_DURATION_MS,
  VOICE_OFFLINE_MESSAGE,
  VOICE_TIMEOUT_MESSAGE,
  appendDictation,
  appendVoiceLevel,
  formatVoiceDuration,
  voiceDecibelsHaveSignal,
  voiceFailure,
  voiceLevelFromDecibels,
  voiceLevelFromRms,
  voiceRecordingLimits,
  voiceRecordingProblem,
  voiceWaveformBars,
} from "../src/voiceInput";

describe("voice input rules", () => {
  it("lets the server lower but never raise the recording limits", () => {
    expect(voiceRecordingLimits()).toEqual({
      maxDurationMs: VOICE_MAX_DURATION_MS,
      maxAudioBytes: VOICE_MAX_AUDIO_BYTES,
    });
    expect(
      voiceRecordingLimits({ max_duration_ms: 60_000, max_audio_bytes: 99e9 }),
    ).toEqual({ maxDurationMs: 60_000, maxAudioBytes: VOICE_MAX_AUDIO_BYTES });
  });

  it("maps browser RMS and native dBFS readings onto one waveform scale", () => {
    expect(voiceLevelFromRms(0.05)).toBeCloseTo(0.6);
    expect(voiceLevelFromRms(1)).toBe(1);
    expect(voiceLevelFromDecibels(20 * Math.log10(0.05))).toBeCloseTo(0.6);
    expect(voiceLevelFromDecibels(-160)).toBeLessThan(0.001);
    expect(voiceLevelFromDecibels(Number.NaN)).toBe(0);
    // Quiet speech counts as signal; a muted or blocked microphone does not.
    expect(voiceDecibelsHaveSignal(-70)).toBe(true);
    expect(voiceDecibelsHaveSignal(-160)).toBe(false);
  });

  it("keeps a bounded level history and lays bars out newest first", () => {
    let levels: readonly number[] = [];
    for (let index = 0; index < VOICE_LEVEL_HISTORY + 5; index++)
      levels = appendVoiceLevel(levels, index / 1000);
    expect(levels).toHaveLength(VOICE_LEVEL_HISTORY);
    expect(levels.at(-1)).toBeCloseTo((VOICE_LEVEL_HISTORY + 4) / 1000);
    expect(voiceWaveformBars([0.2, 2, 0.5], 5)).toEqual([0.5, 1, 0.2, 0, 0]);
    expect(formatVoiceDuration(0)).toBe("0:00");
    expect(formatVoiceDuration(65_400)).toBe("1:05");
  });

  it("rejects recordings that cannot hold speech before sending audio", () => {
    const ok = { durationMs: 1000, bytes: 1000, hasSignal: true };
    expect(voiceRecordingProblem(ok)).toBeNull();
    expect(voiceRecordingProblem({ ...ok, hasSignal: null })).toBeNull();
    expect(voiceRecordingProblem({ ...ok, durationMs: 150 })).toMatch(
      /too short/,
    );
    expect(voiceRecordingProblem({ ...ok, bytes: 0 })).toMatch(/too short/);
    expect(voiceRecordingProblem({ ...ok, hasSignal: false })).toMatch(
      /No microphone audio/,
    );
    expect(
      voiceRecordingProblem({ ...ok, bytes: 11, maxAudioBytes: 10 }),
    ).toMatch(/size limit/);
  });

  it("tells a lost connection from a busy service and a refused recording", () => {
    expect(voiceFailure(new TypeError("Network request failed"))).toEqual({
      kind: "offline",
      message: VOICE_OFFLINE_MESSAGE,
    });
    expect(voiceFailure({ status: 0, code: "timeout" })).toMatchObject({
      kind: "timeout",
      message: VOICE_TIMEOUT_MESSAGE,
    });
    expect(
      voiceFailure({ status: 504, message: "Speech transcription timed out" }),
    ).toEqual({
      kind: "unavailable",
      message: "Speech transcription timed out",
    });
    expect(voiceFailure({ status: 429 })).toMatchObject({
      kind: "unavailable",
    });
    expect(
      voiceFailure({
        status: 409,
        code: "voice_provider_missing",
        message: "Select a connected speech provider",
      }),
    ).toEqual({
      kind: "rejected",
      code: "voice_provider_missing",
      message: "Select a connected speech provider",
    });
    expect(voiceFailure(new Error("Provider unavailable"))).toEqual({
      kind: "rejected",
      message: "Provider unavailable",
    });
  });

  it("appends dictation on its own line after the draft", () => {
    expect(appendDictation("", "  Hello.  ")).toBe("Hello.");
    expect(appendDictation("Fix this:  \n", "Hello.")).toBe(
      "Fix this:\nHello.",
    );
    expect(appendDictation("Draft", "")).toBe("Draft");
    expect(appendDictation("", "Look.", "/workspace/rec/manifest.json")).toBe(
      "Look.\n\nPointing recording (transcript, pointer timeline and desktop frames): @file:/workspace/rec/manifest.json",
    );
  });
});
