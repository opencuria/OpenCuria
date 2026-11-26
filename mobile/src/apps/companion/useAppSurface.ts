import { useEffect, useRef } from "react";
import type { AppRecordingContext } from "@opencuria/chat-core/visualRecording";
import type {
  FrameCapture,
  VisualSurface,
} from "@opencuria/chat-core/visualRecorder";
import {
  activeRecording,
  useActiveRecording,
  type VoiceRecording,
} from "../../voice/voiceRecording";
import {
  captureAppFrame,
  onAppEvent,
  onAppTouch,
  recentAppEvents,
  startAppObservation,
  stopAppObservation,
  type AppEventRecord,
  type AppSurface,
  type AppTouchEvent,
} from "./companionNative";

/** Logs from this long before a recording come along: often the cause. */
export const EARLIER_EVENTS_MS = 60_000;

/**
 * The open app's side of one recording: native observation starts the first
 * time the app shows during it and runs until it ends, so logs and requests
 * stay complete while the user shows pictures or files in between. Touches
 * only count while the app's surface is on top.
 */
class AppObservation {
  readonly ready: Promise<AppSurface | null>;
  surface: VisualSurface | null = null;

  constructor(
    private readonly recording: VoiceRecording,
    app: AppRecordingContext,
  ) {
    recording.visual.setApp(app);
    this.ready = startAppObservation(recording.id)
      .then(async (size) => {
        const subscriptions = [
          onAppTouch((event) => this.touch(event)),
          onAppEvent((event) => this.event(event)),
        ];
        recording.onEnd(async () => {
          for (const subscription of subscriptions) subscription.remove();
          await stopAppObservation().catch(() => undefined);
        });
        for (const event of await recentAppEvents(
          recording.startedAt - EARLIER_EVENTS_MS,
        ).catch(() => []))
          this.event(event);
        return size;
      })
      // A build without app recording keeps the speech and everything else.
      .catch(() => null);
  }

  readonly capture: FrameCapture = async (index, previousId) => {
    const frame = await captureAppFrame(this.recording.id, index, previousId);
    if (frame.unchanged) return { unchanged: true };
    return {
      id: frame.frameId,
      width: frame.width,
      height: frame.height,
      t_ms: this.recording.time(frame.capturedAt),
      texts: frame.texts,
      source: frame.uri,
    };
  };

  private touch(event: AppTouchEvent): void {
    this.surface?.pointer({
      event: event.phase === "cancel" ? "up" : event.phase,
      point: { x: event.x, y: event.y },
      mode: event.mode,
      t_ms: this.recording.time(event.t),
      element: event.element,
    });
  }

  private event(record: AppEventRecord): void {
    this.recording.visual.event({
      t_ms: this.recording.time(record.t),
      kind: record.kind,
      level: record.level,
      message: record.message,
      method: record.method,
      url: record.url,
      status: record.status,
      duration_ms: record.durationMs,
    });
  }
}

const observations = new WeakMap<VoiceRecording, AppObservation>();

/**
 * While a recording runs and `shown` (the companion's sheet is away), the
 * open app is the surface the recording shows: its screen is captured, taps
 * and pointing land on its frames, and its logs and requests join the
 * timeline.
 */
export function useAppSurface(shown: boolean, app: AppRecordingContext): void {
  const recording = useActiveRecording();
  const live = recording?.phase === "recording" ? recording.id : null;
  const appRef = useRef(app);
  appRef.current = app;
  useEffect(() => {
    const current = activeRecording();
    if (!live || !shown || current?.id !== live) return;
    let observation = observations.get(current);
    if (!observation) {
      observation = new AppObservation(current, appRef.current);
      observations.set(current, observation);
    }
    const watched = observation;
    let surface: VisualSurface | null = null;
    let alive = true;
    void watched.ready.then((size) => {
      if (!alive || !size) return;
      surface = current.visual.show({
        kind: "app",
        size,
        capture: watched.capture,
      });
      watched.surface = surface;
    });
    return () => {
      alive = false;
      surface?.hide();
      if (watched.surface === surface) watched.surface = null;
    };
  }, [live, shown]);
}
