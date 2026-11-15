import React, { useEffect } from "react";
import { StyleSheet, View } from "react-native";
import Animated, {
  useAnimatedStyle,
  useSharedValue,
  withTiming,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { MediaViewerFrameProps } from "./MediaViewerFrame.types";
import { NativeToolbar } from "./NativeToolbar.android";
import { useTheme } from "./theme";

/**
 * The system AppCompat toolbar floating above the picture: the back arrow as
 * navigation icon and share as an action. Hiding the chrome fades it away.
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
  const insets = useSafeAreaInsets();
  const shown = useSharedValue(chromeVisible ? 1 : 0);
  useEffect(() => {
    shown.value = withTiming(chromeVisible ? 1 : 0, { duration: 180 });
  }, [chromeVisible, shown]);
  const barStyle = useAnimatedStyle(() => ({
    opacity: shown.value,
    transform: [{ translateY: (shown.value - 1) * 12 }],
  }));
  return (
    <View style={styles.root}>
      {children}
      <Animated.View
        pointerEvents={chromeVisible ? "auto" : "none"}
        accessibilityElementsHidden={!chromeVisible}
        importantForAccessibility={
          chromeVisible ? "auto" : "no-hide-descendants"
        }
        style={[
          styles.bar,
          { paddingTop: insets.top, backgroundColor: theme.background },
          barStyle,
        ]}
      >
        <NativeToolbar
          title={title}
          subtitle=""
          titleColor={theme.text}
          subtitleColor={theme.mutedText}
          surfaceColor={theme.background}
          tintColor={theme.text}
          showClose
          navigationIcon="back"
          menuItems={[
            {
              id: "share",
              title: "Share",
              enabled: !shareDisabled,
              checked: false,
              icon: "share",
            },
          ]}
          onClose={onBack}
          onMenuAction={({ nativeEvent }) => {
            if (nativeEvent.id === "share") onShare();
          }}
          style={styles.toolbar}
        />
      </Animated.View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  bar: { position: "absolute", top: 0, left: 0, right: 0 },
  toolbar: { height: 56, width: "100%" },
});
