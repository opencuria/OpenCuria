import React from "react";
import { View } from "react-native";
import {
  DarkTheme,
  DefaultTheme,
  NavigationContainer,
  NavigationIndependentTree,
} from "expo-router/react-navigation";
import { createNativeStackNavigator } from "expo-router/native-stack";
import { useTheme } from "./theme";

const Stack = createNativeStackNavigator<{ content: undefined }>();

/** Native navigation bars also own the content and keyboard insets beneath them. */
export function NativeScreen({
  title,
  children,
  left,
  right,
}: React.PropsWithChildren<{
  title: string;
  left?: React.ReactNode;
  right?: React.ReactNode;
}>): React.JSX.Element {
  const { theme } = useTheme();
  const navigationTheme = theme.scheme === "dark" ? DarkTheme : DefaultTheme;
  return (
    <NavigationIndependentTree>
      <NavigationContainer
        theme={{
          ...navigationTheme,
          colors: {
            ...navigationTheme.colors,
            background: theme.background,
            card: theme.surface,
            text: theme.text,
            primary: theme.primary,
            border: theme.border,
          },
        }}
      >
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
            }}
          >
            {() => <View style={{ flex: 1 }}>{children}</View>}
          </Stack.Screen>
        </Stack.Navigator>
      </NavigationContainer>
    </NavigationIndependentTree>
  );
}
