// Adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-terminal/LICENSE.
import type { ComponentType, Ref } from "react";
import type { NativeSyntheticEvent, ViewProps } from "react-native";
import { requireNativeView, requireOptionalNativeModule } from "expo";

const NATIVE_TERMINAL_MODULE_NAME = "OpenCuriaTerminalSurface";

interface ExpoGlobalWithViewConfig {
  readonly expo?: {
    getViewConfig?: (moduleName: string, viewName?: string) => unknown;
  };
}

export interface NativeTerminalSurfaceProps extends ViewProps {
  readonly ref?: Ref<NativeTerminalSurfaceHandle>;
  readonly appearanceScheme?: "light" | "dark";
  readonly autoFocus?: boolean;
  readonly readOnly?: boolean;
  readonly focusRequest?: number;
  readonly captureRequest?: number;
  readonly onCapture?: (
    event: NativeSyntheticEvent<{ readonly text: string }>,
  ) => void;
  readonly themeConfig?: string;
  readonly backgroundColor?: string;
  readonly foregroundColor?: string;
  readonly mutedForegroundColor?: string;
  readonly terminalKey: string;
  readonly initialBuffer: string;
  readonly fontSize: number;
  /** `[cols, rows]` of the shared PTY, or empty before one is known. */
  readonly sharedGrid?: ReadonlyArray<number>;
  readonly onInput?: (
    event: NativeSyntheticEvent<{ readonly data: string }>,
  ) => void;
  readonly onResize?: (
    event: NativeSyntheticEvent<{
      readonly cols: number;
      readonly rows: number;
    }>,
  ) => void;
}

export interface NativeTerminalSurfaceHandle {
  readonly writeOutput: (data: string, reset: boolean) => Promise<void>;
}

let cachedView: ComponentType<NativeTerminalSurfaceProps> | undefined;
let resolutionFailed = false;

/** The libghostty view, or null in builds without the native module. */
export function resolveNativeTerminalSurfaceView(): ComponentType<NativeTerminalSurfaceProps> | null {
  if (cachedView) return cachedView;
  if (resolutionFailed) return null;
  const config = (
    globalThis as typeof globalThis & ExpoGlobalWithViewConfig
  ).expo?.getViewConfig?.(NATIVE_TERMINAL_MODULE_NAME);
  if (config == null) return null;
  try {
    cachedView = requireNativeView<NativeTerminalSurfaceProps>(
      NATIVE_TERMINAL_MODULE_NAME,
    );
  } catch {
    resolutionFailed = true;
    return null;
  }
  return cachedView ?? null;
}

export function hasNativeTerminalSurface(): boolean {
  return resolveNativeTerminalSurfaceView() !== null;
}

/** The native view accepts streamed output (`writeOutput`). */
export function supportsNativeTerminalOutput(): boolean {
  try {
    const module = requireOptionalNativeModule<{
      readonly outputRevision?: number;
    }>(NATIVE_TERMINAL_MODULE_NAME);
    return (module?.outputRevision ?? 0) >= 1 && hasNativeTerminalSurface();
  } catch {
    return false;
  }
}
