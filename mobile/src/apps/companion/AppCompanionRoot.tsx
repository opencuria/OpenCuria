import "react-native-gesture-handler";
import React from "react";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { KeyboardProvider } from "react-native-keyboard-controller";
import {
  initialWindowMetrics,
  SafeAreaProvider,
} from "react-native-safe-area-context";
import {
  AppCompanionScreen,
  type AppCompanionProps,
} from "../../screens/AppCompanionScreen";
import { SessionScope } from "../../screens/SessionContext";
import { ThemeProvider } from "../../ui/theme";
import { ToastHost } from "../../ui/ToastHost";

/** Name the native app runtime renders the companion under. */
export const APP_COMPANION_COMPONENT = "OpenCuriaAppCompanion";

/**
 * Second React root of OpenCuria's own instance, drawn over an open app. It
 * shares the session with OpenCuria's main root (`SessionScope`). The
 * keyboard provider must not inset the app's window: both bars count as
 * translucent, and the companion pads itself.
 */
export function AppCompanionRoot(props: AppCompanionProps): React.JSX.Element {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider initialMetrics={initialWindowMetrics}>
        <KeyboardProvider
          statusBarTranslucent
          navigationBarTranslucent
          preserveEdgeToEdge
        >
          <ThemeProvider initialMode="system" persist>
            <SessionScope>
              <AppCompanionScreen {...props} />
              <ToastHost />
            </SessionScope>
          </ThemeProvider>
        </KeyboardProvider>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}
