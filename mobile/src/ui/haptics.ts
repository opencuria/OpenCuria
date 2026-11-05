import { Platform } from "react-native";

type HapticsModule = typeof import("expo-haptics");

let loaded: HapticsModule | null | undefined;

/**
 * Required on first use: a development client built before `expo-haptics`
 * was added has no native module, and feedback must never break a gesture.
 */
function haptics(): HapticsModule | null {
  if (loaded === undefined) {
    try {
      loaded = require("expo-haptics") as HapticsModule;
    } catch {
      loaded = null;
    }
  }
  return loaded;
}

/**
 * A short, crisp tick when a gesture crosses the point where releasing acts,
 * such as pulling up to open the workspace tools.
 */
export function gestureThresholdHaptic(): void {
  const module = haptics();
  if (!module) return;
  const feedback =
    Platform.OS === "android"
      ? module.performAndroidHapticsAsync(module.AndroidHaptics.Confirm)
      : module.impactAsync(module.ImpactFeedbackStyle.Medium);
  void feedback.catch(() => undefined);
}

/** A light tick for a discrete action under the finger, such as a click. */
export function lightTapHaptic(): void {
  const module = haptics();
  if (!module) return;
  const feedback =
    Platform.OS === "android"
      ? module.performAndroidHapticsAsync(module.AndroidHaptics.Clock_Tick)
      : module.impactAsync(module.ImpactFeedbackStyle.Light);
  void feedback.catch(() => undefined);
}
