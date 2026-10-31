import React from "react";
import { Modal, Platform, View } from "react-native";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { ModalFrame } from "../ui/ModalFrame";
import { useTheme } from "../ui/theme";

export interface ToolDetailSheetProps {
  visible: boolean;
  onClose: () => void;
  title: string;
  /** Second header line, such as the containing folder or the diff side. */
  subtitle?: string;
  /** Overflow ("…") menu at the trailing edge of the native header. */
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
  /** Content owns its scrolling (source views, diffs, lists). */
  children: React.ReactNode;
  testID?: string;
}

/**
 * Viewer presented above the tools sheet, e.g. a file or a diff: a UIKit page
 * sheet with a native navigation bar on iOS (swipe down closes it) and a
 * full-screen system dialog with an AppCompat toolbar on Android.
 */
export function ToolDetailSheet({
  visible,
  onClose,
  title,
  subtitle,
  actions,
  onAction,
  children,
  testID,
}: ToolDetailSheetProps): React.JSX.Element | null {
  const { theme } = useTheme();
  if (!visible) return null;
  return (
    <Modal
      visible
      statusBarTranslucent
      navigationBarTranslucent
      animationType="slide"
      presentationStyle={Platform.OS === "ios" ? "pageSheet" : undefined}
      onRequestClose={onClose}
    >
      <ModalFrame
        title={title}
        subtitle={subtitle}
        onClose={onClose}
        closePlacement="leading"
        actions={actions}
        onAction={onAction}
      >
        <View
          testID={testID}
          accessibilityViewIsModal
          onAccessibilityEscape={onClose}
          style={{ flex: 1, backgroundColor: theme.background }}
        >
          {children}
        </View>
      </ModalFrame>
    </Modal>
  );
}
