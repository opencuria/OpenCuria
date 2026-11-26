import React, { useState } from "react";
import {
  activeRecording,
  cancelRecording,
  finishRecording,
  setRecordingMode,
  useActiveRecording,
  type RecordingSnapshot,
} from "../voice/voiceRecording";
import { RecordingBar } from "./RecordingBar";

const HINTS: Record<NonNullable<RecordingSnapshot["surface"]>["kind"], string> =
  {
    app: "Use the app and talk. Switch to Point to show something without tapping it.",
    image:
      "Talk about the picture. Tap a spot, or switch to Point to show it without zooming.",
    file: "Scroll and talk. Tap a line, or switch to Point to show lines without scrolling.",
  };

/** The hint for what is shown on top of a recording. */
export function recordingHint(
  surface: RecordingSnapshot["surface"],
): string | undefined {
  return surface ? HINTS[surface.kind] : undefined;
}

/**
 * The recording bar for a screen that covers the composer (a picture or file
 * viewer, a tools sheet): while a recording runs it floats here, so it can
 * always be seen, switched between Use and Point, discarded or finished. Its
 * parent must fill the screen or sheet.
 */
export function RecordingOverlay({
  atTop: initiallyAtTop = false,
  bottomOffset,
}: {
  /** Rest at the top edge until dragged, else at the bottom. */
  atTop?: boolean;
  /** Rest this far above the bottom edge (bottom chrome). */
  bottomOffset?: number;
}): React.JSX.Element | null {
  const recording = useActiveRecording();
  const [atTop, setAtTop] = useState(initiallyAtTop);
  const current = activeRecording();
  if (!recording || !current || current.id !== recording.id) return null;
  return (
    <RecordingBar
      phase={recording.phase}
      meter={current.meter}
      mode={recording.mode}
      modes={Boolean(recording.surface?.pointable)}
      onModeChange={setRecordingMode}
      onCancel={cancelRecording}
      onFinish={() => finishRecording(false)}
      atTop={atTop}
      onAtTopChange={setAtTop}
      hint={recordingHint(recording.surface)}
      bottomOffset={bottomOffset}
    />
  );
}
