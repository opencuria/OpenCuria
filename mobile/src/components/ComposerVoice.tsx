import React, { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  StyleSheet,
  Text,
  View,
  type LayoutChangeEvent,
} from "react-native";
import Reanimated, {
  cancelAnimation,
  useAnimatedStyle,
  useReducedMotion,
  useSharedValue,
  withRepeat,
  withTiming,
} from "react-native-reanimated";
import {
  ArrowUp,
  CircleAlert,
  CloudOff,
  RotateCcw,
  Square,
  X,
} from "lucide-react-native";
import {
  formatVoiceDuration,
  voiceWaveformBars,
} from "@opencuria/chat-core/voiceInput";
import type { DictationRecordingKind } from "@opencuria/chat-core/voiceInput";
import type { VoiceClipStatus } from "@opencuria/chat-core/voiceTranscriptions";
import { ComposerAction } from "../ui/ComposerAction";
import { IconButton } from "../ui/primitives";
import { statusColor } from "../ui/statusColors";
import { useTheme } from "../ui/theme";

/** The recording on screen. */
export interface ComposerVoiceRecording {
  /** `starting` waits for the microphone, `finishing` saves the file. */
  phase: "starting" | "recording" | "finishing";
  elapsedMs: number;
  /** Recent microphone levels (0..1), oldest first. */
  levels: readonly number[];
}

/** A recording of this chat that waits for its text. */
export interface ComposerVoiceClip {
  id: string;
  durationMs: number;
  status: VoiceClipStatus;
}

export interface ComposerVoiceNotice {
  message: string;
  tone: "error" | "neutral";
  action?: { label: string; onPress: () => void };
}

/** Takes a transcript of the chat on screen; `send` asks to send it at once. */
/** A recording saved beside the transcript, referenced in the draft. */
export interface ComposerVoiceRecordingRef {
  path: string;
  kind: DictationRecordingKind;
}

export type ComposerVoiceReceiver = (
  text: string,
  send: boolean,
  recording?: ComposerVoiceRecordingRef,
) => void;

/** Dictation in the composer; the host owns recording and transcription. */
export interface ComposerVoice {
  recording: ComposerVoiceRecording | null;
  clips: readonly ComposerVoiceClip[];
  notice: ComposerVoiceNotice | null;
  start: () => void;
  /** Stop recording; the text goes to the draft or, with `send`, is sent. */
  finish: (send: boolean) => void;
  /** Discard the recording on screen. */
  cancel: () => void;
  retry: (id: string) => void;
  discard: (id: string) => void;
  dismissNotice: () => void;
  /** The composer on screen registers itself; returns the unregistration. */
  receive: (receiver: ComposerVoiceReceiver) => () => void;
}

const BAR_WIDTH = 3;
const BAR_GAP = 3;
const BAR_MIN = 3;
const BAR_RANGE = 21;

/** Live microphone levels, newest at the trailing edge, as in the webapp. */
export function VoiceWaveform({
  levels,
  color,
}: {
  levels: readonly number[];
  color: string;
}): React.JSX.Element {
  const [width, setWidth] = useState(0);
  const count = Math.ceil((width + BAR_GAP) / (BAR_WIDTH + BAR_GAP));
  return (
    <View
      testID="voice-waveform"
      importantForAccessibility="no-hide-descendants"
      accessibilityElementsHidden
      onLayout={(event: LayoutChangeEvent) =>
        setWidth(Math.round(event.nativeEvent.layout.width))
      }
      style={styles.waveform}
    >
      {voiceWaveformBars(levels, count).map((level, index) => (
        <View
          key={index}
          style={[
            styles.bar,
            {
              height: BAR_MIN + Math.round(level * BAR_RANGE),
              backgroundColor: color,
            },
          ]}
        />
      ))}
    </View>
  );
}

/** The red recording light, pulsing like the system's while it records. */
export function RecordingLight({
  active,
}: {
  active: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  const reduceMotion = useReducedMotion();
  const opacity = useSharedValue(1);
  useEffect(() => {
    if (!active || reduceMotion) {
      cancelAnimation(opacity);
      opacity.value = 1;
      return;
    }
    opacity.value = withRepeat(withTiming(0.25, { duration: 700 }), -1, true);
    return () => cancelAnimation(opacity);
  }, [active, opacity, reduceMotion]);
  const animated = useAnimatedStyle(() => ({ opacity: opacity.value }));
  return (
    <Reanimated.View
      style={[
        styles.light,
        {
          backgroundColor: active
            ? statusColor("critical", theme)
            : theme.subtleText,
        },
        animated,
      ]}
    />
  );
}

/**
 * Replaces the composer toolbar while recording: discard, the running time
 * and waveform, stop (the text stays for editing) and send.
 */
export function VoiceRecordingBar({
  recording,
  onCancel,
  onFinish,
}: {
  recording: ComposerVoiceRecording;
  onCancel: () => void;
  onFinish: (send: boolean) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const live = recording.phase === "recording";
  return (
    <View
      testID="voice-recording-bar"
      accessibilityLabel="Voice input"
      style={styles.recording}
    >
      <ComposerAction
        label="Discard recording"
        icon={X}
        disabled={recording.phase === "finishing"}
        onPress={onCancel}
        testID="voice-cancel"
      />
      <View
        style={styles.meter}
        accessible
        accessibilityRole="text"
        accessibilityLabel={
          live
            ? `Recording, ${formatVoiceDuration(recording.elapsedMs)}`
            : recording.phase === "starting"
              ? "Starting microphone"
              : "Saving recording"
        }
        accessibilityLiveRegion="polite"
      >
        <RecordingLight active={live} />
        {recording.phase === "finishing" ? (
          <ActivityIndicator size="small" color={theme.mutedText} />
        ) : null}
        <Text
          testID="voice-elapsed"
          style={[styles.elapsed, { color: theme.text }]}
        >
          {formatVoiceDuration(recording.elapsedMs)}
        </Text>
        <VoiceWaveform levels={recording.levels} color={theme.mutedText} />
      </View>
      <ComposerAction
        label="Stop and edit"
        icon={Square}
        iconSize={13}
        filledIcon
        disabled={!live}
        onPress={() => onFinish(false)}
        testID="voice-stop"
      />
      <ComposerAction
        label="Stop and send"
        icon={ArrowUp}
        primary
        iconSize={22}
        disabled={!live}
        onPress={() => onFinish(true)}
        testID="voice-send"
      />
    </View>
  );
}

function clipStatusText(status: VoiceClipStatus): string {
  switch (status.kind) {
    case "queued":
    case "transcribing":
      return "Transcribing…";
    case "waiting":
      return status.failure.kind === "offline"
        ? "Saved. Transcribes when online."
        : "Saved. Trying again shortly…";
    case "failed":
      return status.failure.message;
  }
}

/**
 * Recordings of this chat that wait for their text, above the editor like
 * attachments. They are saved on the device, so nothing is lost offline.
 */
export function VoiceClipRows({
  clips,
  onRetry,
  onDiscard,
}: {
  clips: readonly ComposerVoiceClip[];
  onRetry: (id: string) => void;
  onDiscard: (id: string) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={styles.clips}>
      {clips.map((clip) => {
        const { status } = clip;
        const failed = status.kind === "failed";
        const working =
          status.kind === "queued" || status.kind === "transcribing";
        return (
          <View
            key={clip.id}
            testID={`voice-clip-${clip.id}`}
            style={[styles.clip, { backgroundColor: theme.controlFill }]}
          >
            <View
              style={[styles.clipIcon, { backgroundColor: theme.controlFill }]}
            >
              {working ? (
                <ActivityIndicator size="small" color={theme.mutedText} />
              ) : failed ? (
                <CircleAlert size={17} color={theme.danger} />
              ) : (
                <CloudOff size={17} color={theme.mutedText} />
              )}
            </View>
            <View
              style={styles.clipText}
              accessible
              accessibilityRole="text"
              accessibilityLabel={`Voice recording, ${formatVoiceDuration(clip.durationMs)}. ${clipStatusText(status)}`}
            >
              <View style={styles.clipTitleRow}>
                <Text
                  numberOfLines={1}
                  style={[
                    styles.clipTitle,
                    styles.clipName,
                    { color: theme.text },
                  ]}
                >
                  Recording
                </Text>
                {/* The length always shows; only the name gives way. */}
                <Text style={[styles.clipTitle, { color: theme.text }]}>
                  {` · ${formatVoiceDuration(clip.durationMs)}`}
                </Text>
              </View>
              <Text
                testID={`voice-clip-status-${clip.id}`}
                numberOfLines={2}
                style={[
                  styles.clipStatus,
                  { color: failed ? theme.danger : theme.mutedText },
                ]}
              >
                {clipStatusText(status)}
              </Text>
            </View>
            {failed || status.kind === "waiting" ? (
              <IconButton
                symbol="arrow.clockwise"
                icon={<RotateCcw size={18} color={theme.mutedText} />}
                label="Try again"
                tone="quiet"
                onPress={() => onRetry(clip.id)}
                testID={`voice-clip-retry-${clip.id}`}
              />
            ) : null}
            <IconButton
              symbol="xmark"
              icon={<X size={18} color={theme.mutedText} />}
              label="Discard recording"
              tone="quiet"
              onPress={() =>
                // The recording exists only here; never lose it to a stray tap.
                Alert.alert(
                  "Discard recording?",
                  "Its text has not reached your message yet.",
                  [
                    { text: "Keep", style: "cancel" },
                    {
                      text: "Discard",
                      style: "destructive",
                      onPress: () => onDiscard(clip.id),
                    },
                  ],
                )
              }
              testID={`voice-clip-discard-${clip.id}`}
            />
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  recording: {
    minHeight: 44,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  meter: {
    flex: 1,
    minWidth: 0,
    height: 36,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 6,
  },
  light: { width: 8, height: 8, borderRadius: 4 },
  elapsed: {
    fontSize: 15,
    fontWeight: "500",
    fontVariant: ["tabular-nums"],
  },
  waveform: {
    flex: 1,
    minWidth: 0,
    height: 28,
    flexDirection: "row-reverse",
    alignItems: "center",
    gap: BAR_GAP,
    overflow: "hidden",
  },
  bar: { width: BAR_WIDTH, borderRadius: BAR_WIDTH / 2 },
  clips: { gap: 6, paddingHorizontal: 6, paddingTop: 6 },
  clip: {
    minHeight: 52,
    borderRadius: 22,
    borderCurve: "continuous",
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingLeft: 7,
    paddingRight: 2,
  },
  clipIcon: {
    width: 36,
    height: 36,
    borderRadius: 18,
    alignItems: "center",
    justifyContent: "center",
  },
  clipText: { flex: 1, minWidth: 0, paddingVertical: 7, gap: 1 },
  clipTitleRow: { flexDirection: "row", minWidth: 0 },
  clipTitle: {
    fontSize: 15,
    fontWeight: "500",
    fontVariant: ["tabular-nums"],
  },
  clipName: { flexShrink: 1 },
  clipStatus: { fontSize: 13, lineHeight: 17 },
});
