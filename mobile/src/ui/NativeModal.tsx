import React from "react";
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  ScrollView,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ModalFrame } from "./ModalFrame";
import { useTheme } from "./theme";

export interface NativeModalProps {
  visible: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  testID?: string;
  dismissible?: boolean;
  scrollable?: boolean;
}

/** UIKit page-sheet / Android system dialog; the OS owns the presentation. */
export function NativeModal({
  visible,
  onClose,
  title = "OpenCuria",
  children,
  footer,
  testID,
  dismissible = true,
  scrollable = true,
}: NativeModalProps): React.JSX.Element | null {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  if (!visible) return null;
  return (
    <Modal
      visible
      statusBarTranslucent
      navigationBarTranslucent
      animationType="slide"
      presentationStyle="pageSheet"
      allowSwipeDismissal={dismissible}
      onRequestClose={dismissible ? onClose : () => undefined}
    >
      <ModalFrame title={title} onClose={dismissible ? onClose : undefined}>
        <KeyboardAvoidingView
          testID={testID}
          accessibilityViewIsModal
          onAccessibilityEscape={dismissible ? onClose : undefined}
          behavior={Platform.OS === "ios" ? "padding" : undefined}
          style={{
            flex: 1,
            backgroundColor: theme.background,
            paddingBottom: insets.bottom,
          }}
        >
          {scrollable ? (
            <ScrollView
              keyboardShouldPersistTaps="handled"
              keyboardDismissMode="on-drag"
              contentContainerStyle={{ padding: 20, gap: 12 }}
            >
              {children}
            </ScrollView>
          ) : (
            <View style={{ flex: 1 }}>{children}</View>
          )}
          {footer ? (
            <View style={{ padding: 20, gap: 8 }}>{footer}</View>
          ) : null}
        </KeyboardAvoidingView>
      </ModalFrame>
    </Modal>
  );
}
