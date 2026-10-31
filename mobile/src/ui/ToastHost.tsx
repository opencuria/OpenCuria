import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  AccessibilityInfo,
  Animated,
  PanResponder,
  Platform,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  CircleAlert,
  CircleCheck,
  Info,
  TriangleAlert,
} from "lucide-react-native";
import { useTheme, type OpenCuriaTheme } from "./theme";
import { armToast, dismissToast, useToastHost, type Toast } from "./toast";
import { ToastSurface } from "./ToastSurface";

/**
 * Shows `showToast` feedback at the top of its screen, like a quiet system
 * notification. Mount one per screen or modal sheet; only the one in front
 * shows the toast. Tap or swipe it up to dismiss.
 */
export function ToastHost(): React.JSX.Element | null {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const toast = useToastHost();
  // The last toast stays rendered while it animates away.
  const [shown, setShown] = useState<Toast | null>(toast);
  const appearance = useRef(new Animated.Value(0)).current;
  const drag = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (toast) {
      setShown(toast);
      return;
    }
    Animated.timing(appearance, {
      toValue: 0,
      duration: 200,
      useNativeDriver: true,
    }).start(({ finished }) => {
      if (finished) setShown(null);
    });
  }, [appearance, toast]);
  // Animate in only once the card is mounted: a native-driven animation that
  // starts before its view exists never reaches it.
  const shownId = shown?.id;
  const visible = Boolean(toast) && toast?.id === shownId;
  useEffect(() => {
    if (!visible || !shown) return;
    drag.setValue(0);
    Animated.spring(appearance, {
      toValue: 1,
      speed: 18,
      bounciness: 5,
      useNativeDriver: true,
    }).start();
    armToast(shown.id);
    AccessibilityInfo.announceForAccessibility(
      [shown.title, shown.message].filter(Boolean).join(". "),
    );
    // Runs once per toast; `shown` belongs to `shownId`.
  }, [appearance, drag, shownId, visible]);

  const id = toast?.id;
  const gestures = useMemo(
    () =>
      PanResponder.create({
        onStartShouldSetPanResponder: () => true,
        onMoveShouldSetPanResponder: (_event, { dx, dy }) =>
          Math.abs(dy) > 4 && Math.abs(dy) > Math.abs(dx),
        // Up follows the finger; down resists.
        onPanResponderMove: (_event, { dy }) =>
          drag.setValue(dy < 0 ? dy : dy * 0.15),
        onPanResponderRelease: (_event, { dx, dy, vy }) => {
          const tap = Math.abs(dx) < 6 && Math.abs(dy) < 6;
          if (tap || dy < -18 || vy < -0.35) dismissToast(id);
          else
            Animated.spring(drag, {
              toValue: 0,
              speed: 20,
              bounciness: 0,
              useNativeDriver: true,
            }).start();
        },
        onPanResponderTerminate: () => drag.setValue(0),
      }),
    [drag, id],
  );

  if (!shown) return null;
  const { icon: Icon, color } = toneStyle(theme, shown.tone);
  return (
    <View pointerEvents="box-none" style={styles.layer}>
      <Animated.View
        {...gestures.panHandlers}
        accessible
        accessibilityRole="alert"
        accessibilityLiveRegion="polite"
        accessibilityLabel={[shown.title, shown.message]
          .filter(Boolean)
          .join(", ")}
        accessibilityHint="Dismisses the message"
        accessibilityActions={[{ name: "activate" }]}
        onAccessibilityAction={() => dismissToast(shown.id)}
        testID="toast"
        style={[
          styles.toast,
          {
            top: insets.top + (Platform.OS === "ios" ? 4 : 8),
            opacity: appearance,
            transform: [
              {
                translateY: Animated.add(
                  appearance.interpolate({
                    inputRange: [0, 1],
                    outputRange: [-28, 0],
                  }),
                  drag,
                ),
              },
              {
                scale: appearance.interpolate({
                  inputRange: [0, 1],
                  outputRange: [0.94, 1],
                }),
              },
            ],
          },
        ]}
      >
        <ToastSurface>
          <View style={styles.content}>
            <Icon size={18} color={color} strokeWidth={2.25} />
            <View style={styles.text}>
              <Text
                numberOfLines={2}
                style={[styles.title, { color: theme.text }]}
              >
                {shown.title}
              </Text>
              {shown.message ? (
                <Text
                  numberOfLines={3}
                  style={[styles.message, { color: theme.mutedText }]}
                >
                  {shown.message}
                </Text>
              ) : null}
            </View>
          </View>
        </ToastSurface>
      </Animated.View>
    </View>
  );
}

function toneStyle(theme: OpenCuriaTheme, tone: Toast["tone"]) {
  switch (tone) {
    case "success":
      return { icon: CircleCheck, color: theme.success };
    case "warning":
      return { icon: TriangleAlert, color: theme.warning };
    case "error":
      return { icon: CircleAlert, color: theme.danger };
    default:
      return { icon: Info, color: theme.mutedText };
  }
}

const styles = StyleSheet.create({
  layer: {
    position: "absolute",
    top: 0,
    right: 0,
    bottom: 0,
    left: 0,
    zIndex: 1000,
    elevation: 1000,
  },
  toast: {
    position: "absolute",
    alignSelf: "center",
    maxWidth: 440,
    marginHorizontal: 16,
  },
  content: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 44,
    paddingVertical: 10,
    paddingLeft: 14,
    paddingRight: 18,
  },
  text: { flexShrink: 1, gap: 1 },
  title: { fontSize: 14, lineHeight: 19, fontWeight: "600" },
  message: { fontSize: 13, lineHeight: 17 },
});
