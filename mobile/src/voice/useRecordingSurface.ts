import { useEffect, useRef, useState } from "react";
import type {
  VisualSurface,
  VisualSurfaceSpec,
} from "@opencuria/chat-core/visualRecorder";
import { activeRecording, useActiveRecording } from "./voiceRecording";

const lineIds = new WeakMap<readonly string[], number>();
let nextLineId = 0;

/** Identifies a surface: the same picture or file text keeps its surface. */
function surfaceKey(spec: VisualSurfaceSpec): string {
  if (spec.kind === "image")
    return `image|${spec.path}|${spec.width}|${spec.height}`;
  if (spec.kind === "file") {
    let id = lineIds.get(spec.lines);
    if (id === undefined) {
      id = nextLineId += 1;
      lineIds.set(spec.lines, id);
    }
    return `file|${spec.path}|${spec.approximate ? 1 : 0}|${id}`;
  }
  return "app";
}

/**
 * Show `spec` to the running recording while this component shows it; null
 * while no recording runs or nothing (pointable) is on screen. The newest
 * shown surface records, so a viewer opened above another takes over and
 * hands back when it closes. Report views and pointers on the returned
 * surface; it ignores them while covered.
 */
export function useRecordingSurface(
  spec: VisualSurfaceSpec | null,
): VisualSurface | null {
  const recording = useActiveRecording();
  const live = recording?.phase === "recording" ? recording.id : null;
  const key = spec ? surfaceKey(spec) : null;
  const specRef = useRef(spec);
  specRef.current = spec;
  const [surface, setSurface] = useState<VisualSurface | null>(null);
  useEffect(() => {
    const current = activeRecording();
    const wanted = specRef.current;
    if (!live || !key || !wanted || current?.id !== live) {
      setSurface(null);
      return;
    }
    const shown = current.visual.show(wanted);
    setSurface(shown);
    return () => {
      shown.hide();
      setSurface((previous) => (previous === shown ? null : previous));
    };
  }, [key, live]);
  return surface;
}
