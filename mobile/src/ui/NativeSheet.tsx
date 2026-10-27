import React from "react";
import { Platform, View, useWindowDimensions } from "react-native";
import { BottomSheet } from "@expo/ui/community/bottom-sheet";

export interface NativeSheetProps {
  visible: boolean;
  onClose: () => void;
  children: React.ReactNode;
  /**
   * Content expects the keyboard (e.g. a search field). Android then keeps the
   * sheet short enough to stay below the status bar while the keyboard shows.
   */
  keyboard?: boolean;
  testID?: string;
}

/** SwiftUI resizes sheet content per detent and avoids the keyboard itself. */
const IOS_DETENTS = ["60%", "94%"];
/**
 * Material lays partially expanded content out at full height, hiding its
 * end; Android therefore opens a fully expanded sheet of fixed height.
 */
const ANDROID_HEIGHT = 0.72;
const ANDROID_KEYBOARD_HEIGHT = 0.5;

/**
 * Bottom sheet for quick choices: a SwiftUI sheet with medium/large detents
 * on iOS and a Material 3 ModalBottomSheet on Android. The OS owns the drag
 * handle, swipe/back dismissal and background.
 */
export function NativeSheet({
  visible,
  onClose,
  children,
  keyboard = false,
  testID,
}: NativeSheetProps): React.JSX.Element {
  const { height } = useWindowDimensions();
  const android = Platform.OS === "android";
  const fraction = keyboard ? ANDROID_KEYBOARD_HEIGHT : ANDROID_HEIGHT;
  return (
    <BottomSheet
      index={visible ? 0 : -1}
      snapPoints={android ? undefined : IOS_DETENTS}
      enablePanDownToClose
      onClose={onClose}
    >
      <View
        testID={testID}
        accessibilityViewIsModal
        style={
          android ? { height: Math.round(height * fraction) } : { flex: 1 }
        }
      >
        {children}
      </View>
    </BottomSheet>
  );
}
