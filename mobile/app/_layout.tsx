import "react-native-gesture-handler";
import React from "react";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import {
  initialWindowMetrics,
  SafeAreaProvider,
} from "react-native-safe-area-context";
import { KeyboardProvider } from "react-native-keyboard-controller";
import { ThemeProvider, useTheme } from "../src/ui/theme";
import { SessionProvider } from "../src/screens/SessionContext";
import { NotificationProvider } from "../src/notifications/NotificationContext";
import { ToastHost } from "../src/ui/ToastHost";
import { AppsWidgetProvider } from "../src/widgets/AppsWidgetProvider";

export default function RootLayout(): React.JSX.Element {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider initialMetrics={initialWindowMetrics}>
        <KeyboardProvider>
          <ThemeProvider initialMode="system" persist>
            <NativeStatusBar />
            <SessionProvider>
              <NotificationProvider>
                <AppsWidgetProvider>
                  <Stack
                    screenOptions={{ headerShown: false, animation: "fade" }}
                  >
                    <Stack.Screen name="index" />
                  </Stack>
                  <ToastHost />
                </AppsWidgetProvider>
              </NotificationProvider>
            </SessionProvider>
          </ThemeProvider>
        </KeyboardProvider>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}

function NativeStatusBar(): React.JSX.Element {
  const { theme } = useTheme();
  return <StatusBar style={theme.scheme === "dark" ? "light" : "dark"} />;
}
