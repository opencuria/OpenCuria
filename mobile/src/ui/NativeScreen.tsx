import React from "react";
import { View } from "react-native";
import {
  DarkTheme,
  DefaultTheme,
  NavigationContainer,
  NavigationIndependentTree,
  useHeaderHeight,
} from "expo-router/react-navigation";
import { createNativeStackNavigator } from "expo-router/native-stack";
import { ChromeInsetContext } from "./chromeInset";
import { useTheme, type OpenCuriaTheme } from "./theme";

const Stack = createNativeStackNavigator<{ content: undefined }>();

/** Navigation colors derived from the app palette for native-stack containers. */
export function navigationTheme(theme: OpenCuriaTheme) {
  const base = theme.scheme === "dark" ? DarkTheme : DefaultTheme;
  return {
    ...base,
    colors: {
      ...base.colors,
      background: theme.background,
      card: theme.surface,
      text: theme.text,
      primary: theme.primary,
      border: theme.border,
    },
  };
}

/** Publishes the measured native header height to content scrolling beneath it. */
export function HeaderChromeInset({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const height = useHeaderHeight();
  return (
    <ChromeInsetContext.Provider value={height}>
      <View style={{ flex: 1 }}>{children}</View>
    </ChromeInsetContext.Provider>
  );
}

/** Native navigation bars also own the content and keyboard insets beneath them. */
export function NativeScreen({
  title,
  children,
  left,
  right,
  transparentHeader = false,
}: React.PropsWithChildren<{
  title: string;
  left?: React.ReactNode;
  right?: React.ReactNode;
  /** Draw content beneath a transparent header, as chat transcripts do. */
  transparentHeader?: boolean;
}>): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <NavigationIndependentTree>
      <NavigationContainer theme={navigationTheme(theme)}>
        <Stack.Navigator
          screenOptions={{
            animation: "none",
            headerBackVisible: false,
            headerShadowVisible: false,
            contentStyle: { backgroundColor: theme.background },
            headerTintColor: theme.primary,
            headerStyle: { backgroundColor: theme.surface },
            statusBarStyle: theme.scheme === "dark" ? "light" : "dark",
          }}
        >
          <Stack.Screen
            name="content"
            options={{
              title,
              headerLeft: left ? () => left : undefined,
              headerRight: right ? () => right : undefined,
              ...(transparentHeader
                ? {
                    headerTransparent: true,
                    headerStyle: { backgroundColor: "transparent" },
                    headerBackground: () => (
                      <View
                        style={{
                          flex: 1,
                          experimental_backgroundImage: `linear-gradient(to bottom, ${theme.background} 45%, ${theme.background}00 100%)`,
                        }}
                      />
                    ),
                  }
                : {}),
            }}
          >
            {() =>
              transparentHeader ? (
                <HeaderChromeInset>{children}</HeaderChromeInset>
              ) : (
                <View style={{ flex: 1 }}>{children}</View>
              )
            }
          </Stack.Screen>
        </Stack.Navigator>
      </NavigationContainer>
    </NavigationIndependentTree>
  );
}
