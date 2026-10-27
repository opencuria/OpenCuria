import "react-native-gesture-handler";
import React from "react";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { KeyboardProvider } from "react-native-keyboard-controller";
import { ThemeProvider, useTheme } from "../src/ui/theme";
import { SessionProvider } from "../src/screens/SessionContext";
import { NotificationProvider } from "../src/notifications/NotificationContext";

export default function RootLayout(): React.JSX.Element {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider>
        <KeyboardProvider>
          <ThemeProvider initialMode="system">
            <NativeStatusBar />
            <SessionProvider>
              <NotificationProvider>
                <Stack
                  screenOptions={{ headerShown: false, animation: "fade" }}
                >
                  <Stack.Screen name="index" />
                </Stack>
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
