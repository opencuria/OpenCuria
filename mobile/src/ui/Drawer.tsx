import React, { useEffect, useRef } from "react";
import {
  Animated,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  View,
  useWindowDimensions,
} from "react-native";
import {
  Gesture,
  GestureDetector,
  GestureHandlerRootView,
} from "react-native-gesture-handler";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useTheme } from "./theme";

/** Native side navigation with a fixed account footer and swipe-to-dismiss. */
export function Drawer({
  visible,
  onClose,
  children,
  footer,
  testID,
}: React.PropsWithChildren<{
  visible: boolean;
  onClose: () => void;
  footer: React.ReactNode;
  testID?: string;
}>): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const { width } = useWindowDimensions();
  const drawerWidth = Math.min(width * 0.8, 360);
  const offset = useRef(new Animated.Value(-drawerWidth)).current;
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const gesture = Gesture.Pan()
    .activeOffsetX(-12)
    .failOffsetY([-20, 20])
    .runOnJS(true)
    .onUpdate(({ translationX }) => offset.setValue(Math.min(0, translationX)))
    .onEnd(({ translationX, velocityX }) => {
      if (translationX < -70 || velocityX < -500) closeRef.current();
      else
        Animated.spring(offset, {
          toValue: 0,
          useNativeDriver: true,
        }).start();
    });

  useEffect(() => {
    if (!visible) return;
    offset.setValue(-drawerWidth);
    const animation = Animated.timing(offset, {
      toValue: 0,
      duration: 220,
      useNativeDriver: true,
    });
    animation.start();
    return () => animation.stop();
  }, [visible, drawerWidth, offset]);

  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      statusBarTranslucent
      onRequestClose={onClose}
    >
      <GestureHandlerRootView style={styles.root}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Close conversation drawer"
          onPress={onClose}
          style={[StyleSheet.absoluteFill, { backgroundColor: theme.overlay }]}
        />
        <GestureDetector gesture={gesture}>
          <Animated.View
            testID={testID}
            accessibilityViewIsModal
            onAccessibilityEscape={onClose}
            style={[
              styles.panel,
              {
                width: drawerWidth,
                paddingTop: insets.top + 12,
                paddingBottom: Math.max(insets.bottom, 12),
                backgroundColor: theme.surface,
                transform: [{ translateX: offset }],
              },
            ]}
          >
            <ScrollView
              keyboardShouldPersistTaps="handled"
              keyboardDismissMode="on-drag"
              contentContainerStyle={styles.content}
              showsVerticalScrollIndicator={false}
            >
              {children}
            </ScrollView>
            <View style={[styles.footer, { borderTopColor: theme.border }]}>
              {footer}
            </View>
          </Animated.View>
        </GestureDetector>
      </GestureHandlerRootView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  panel: { height: "100%" },
  content: { paddingHorizontal: 16, paddingBottom: 20, gap: 8 },
  footer: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 12,
    paddingTop: 10,
  },
});
