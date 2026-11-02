import React from "react";
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  ScrollView,
  View,
} from "react-native";
import { KeyboardAvoidingView as ImeAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ModalFrame } from "./ModalFrame";
import type {
  ModalPrimaryAction as PrimaryAction,
  ModalSearch,
} from "./ModalFrame.types";
import { ModalPrimaryAction } from "./ModalPrimaryAction";
import type { NativeMenuAction } from "./NativeMenu";
import { ToastHost } from "./ToastHost";
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
  /** Main action: navigation bar on iOS, bottom button or FAB on Android. */
  primaryAction?: PrimaryAction;
  search?: ModalSearch;
  /**
   * Swipe-down dismissal (iOS); off while a form has unsaved changes so the
   * close button can confirm discarding them. Defaults to `dismissible`.
   */
  swipeToDismiss?: boolean;
  /** Overflow ("…") menu in the header. */
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
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
  primaryAction,
  search,
  actions,
  onAction,
  swipeToDismiss = dismissible,
}: NativeModalProps): React.JSX.Element | null {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  if (!visible) return null;
  const content = (
    <>
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
      ) : primaryAction ? (
        <ModalPrimaryAction action={primaryAction} />
      ) : null}
    </>
  );
  const frame = {
    testID,
    accessibilityViewIsModal: true,
    onAccessibilityEscape: dismissible ? onClose : undefined,
  };
  return (
    <Modal
      visible
      statusBarTranslucent
      navigationBarTranslucent
      animationType="slide"
      presentationStyle="pageSheet"
      allowSwipeDismissal={swipeToDismiss}
      onRequestClose={dismissible ? onClose : () => undefined}
    >
      <View style={{ flex: 1 }}>
        <ModalFrame
          title={title}
          onClose={dismissible ? onClose : undefined}
          primaryAction={primaryAction}
          search={search}
          actions={actions}
          onAction={onAction}
        >
          {Platform.OS === "android" ? (
            // Edge-to-edge dialogs do not resize for the keyboard; pad by the
            // keyboard's overlap so the bottom action stays reachable.
            <View
              {...frame}
              style={{
                flex: 1,
                backgroundColor: theme.background,
                paddingBottom: insets.bottom,
              }}
            >
              <ImeAvoidingView
                behavior="padding"
                automaticOffset
                style={{ flex: 1 }}
              >
                {/* Floating buttons are placed against this box, which ends at the keyboard. */}
                <View style={{ flex: 1 }}>{content}</View>
              </ImeAvoidingView>
            </View>
          ) : (
            <KeyboardAvoidingView
              {...frame}
              behavior="padding"
              style={{
                flex: 1,
                backgroundColor: theme.background,
                paddingBottom: insets.bottom,
              }}
            >
              {content}
            </KeyboardAvoidingView>
          )}
        </ModalFrame>
        <ToastHost />
      </View>
    </Modal>
  );
}
