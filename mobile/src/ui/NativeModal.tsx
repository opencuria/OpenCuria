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
import { visiblePrimaryAction } from "./formStates";
import { NativeForm } from "./NativeForm";
import type { NativeFormProps } from "./NativeForm.types";
import type { NativeMenuAction } from "./NativeMenu";
import { ToastHost } from "./ToastHost";
import { useTheme } from "./theme";

export interface NativeModalProps {
  visible: boolean;
  onClose: () => void;
  title?: string;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  testID?: string;
  dismissible?: boolean;
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
  /** Large collapsing title, for top-level collection sheets. */
  largeTitle?: boolean;
  /**
   * The sheet's content as one native form. On iOS it runs inside the
   * sheet's SwiftUI navigation stack (large titles collapse, the glass bar
   * follows the scrolling, SwiftUI avoids the keyboard); on Android it fills
   * the dialog. `children` then only hold overlays such as nested sheets.
   * While the form's empty state offers the same action (same title),
   * `primaryAction` hides.
   */
  form?: NativeFormProps;
}

/** UIKit page-sheet / Android system dialog; the OS owns the presentation. */
export function NativeModal({
  visible,
  onClose,
  title = "OpenCuria",
  children = null,
  footer,
  testID,
  dismissible = true,
  primaryAction,
  search,
  actions,
  onAction,
  largeTitle,
  form,
  swipeToDismiss = dismissible,
}: NativeModalProps): React.JSX.Element | null {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  if (!visible) return null;
  const primary = visiblePrimaryAction(primaryAction, form);
  const content = (
    <>
      {form ? (
        <View style={{ flex: 1 }}>
          <NativeForm {...form} />
          {children}
        </View>
      ) : (
        <ScrollView
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          contentContainerStyle={{ padding: 20, gap: 12 }}
        >
          {children}
        </ScrollView>
      )}
      {footer ? (
        <View style={{ padding: 20, gap: 8 }}>{footer}</View>
      ) : primary ? (
        <ModalPrimaryAction action={primary} />
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
        {form && Platform.OS === "ios" ? (
          // SwiftUI draws the form, its background and keyboard avoidance.
          <View
            {...frame}
            style={{ flex: 1, backgroundColor: theme.background }}
          >
            <ModalFrame
              title={title}
              largeTitle={largeTitle}
              onClose={dismissible ? onClose : undefined}
              primaryAction={primary}
              search={search}
              actions={actions}
              onAction={onAction}
              form={form}
            >
              {null}
            </ModalFrame>
            {children}
            {/* Inside the modal root, so VoiceOver can reach its toasts. */}
            <ToastHost />
          </View>
        ) : (
          <ModalFrame
            title={title}
            largeTitle={largeTitle}
            onClose={dismissible ? onClose : undefined}
            primaryAction={primary}
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
        )}
        {form && Platform.OS === "ios" ? null : <ToastHost />}
      </View>
    </Modal>
  );
}
