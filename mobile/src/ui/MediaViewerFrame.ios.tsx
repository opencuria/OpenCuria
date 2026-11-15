import React, { useRef } from "react";
import { Platform } from "react-native";
import {
  NavigationContainer,
  NavigationIndependentTree,
} from "expo-router/react-navigation";
import { createNativeStackNavigator } from "expo-router/native-stack";
import type { MediaViewerFrameProps } from "./MediaViewerFrame.types";
import { navigationTheme } from "./NativeScreen";
import { useTheme } from "./theme";

const Stack = createNativeStackNavigator<{ viewer: undefined }>();
/** iOS 26 bar items float as liquid glass; older systems blur the bar. */
const LIQUID_GLASS = Number.parseInt(String(Platform.Version), 10) >= 26;

/**
 * A transparent UIKit navigation bar above the picture, like the chat's own:
 * back at the leading edge, share at the trailing edge. Hiding the chrome
 * hides the bar the way UIKit does.
 */
export function MediaViewerFrame({
  title,
  chromeVisible,
  onBack,
  onShare,
  shareDisabled = false,
  children,
}: MediaViewerFrameProps): React.JSX.Element {
  const { theme } = useTheme();
  const latest = useRef({ onBack, onShare });
  latest.current = { onBack, onShare };
  const base = navigationTheme(theme);
  return (
    <NavigationIndependentTree>
      <NavigationContainer
        theme={{
          ...base,
          colors: { ...base.colors, background: "transparent" },
        }}
      >
        <Stack.Navigator
          screenOptions={{
            animation: "none",
            headerShadowVisible: false,
            headerTintColor: theme.text,
            contentStyle: { backgroundColor: "transparent" },
          }}
        >
          <Stack.Screen
            name="viewer"
            options={{
              title,
              headerShown: chromeVisible,
              headerTransparent: true,
              headerBlurEffect: LIQUID_GLASS
                ? undefined
                : "systemChromeMaterial",
              unstable_headerLeftItems: () => [
                {
                  type: "button",
                  label: "Back",
                  icon: { type: "sfSymbol", name: "chevron.left" },
                  onPress: () => latest.current.onBack(),
                },
              ],
              unstable_headerRightItems: () => [
                {
                  type: "button",
                  label: "Share",
                  icon: { type: "sfSymbol", name: "square.and.arrow.up" },
                  disabled: shareDisabled,
                  onPress: () => latest.current.onShare(),
                },
              ],
            }}
          >
            {() => children}
          </Stack.Screen>
        </Stack.Navigator>
      </NavigationContainer>
    </NavigationIndependentTree>
  );
}
