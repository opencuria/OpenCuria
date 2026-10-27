import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Animated,
  Platform,
  StyleSheet,
  View,
  type LayoutChangeEvent,
} from "react-native";
import {
  KeyboardController,
  KeyboardGestureArea,
  useKeyboardAnimation,
  useReanimatedKeyboardAnimation,
} from "react-native-keyboard-controller";
import {
  makeMutable,
  useDerivedValue,
  useSharedValue,
  type DerivedValue,
  type SharedValue,
} from "react-native-reanimated";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { runOnJS } from "react-native-worklets";
import { useTheme } from "../ui/theme";

/** Links the composer input to the keyboard's interactive-dismiss area. */
export const COMPOSER_INPUT_NATIVE_ID = "chat-composer-input";

/** Space between the docked composer and the top of an open keyboard. */
const KEYBOARD_GAP = 8;
/** Composer height before its first layout, so the transcript opens clear of it. */
const ESTIMATED_COMPOSER_HEIGHT = 112;
/** Downward pull on the composer that hands the keyboard its close animation. */
const COMPOSER_PULL = 12;
const IS_IOS = Platform.OS === "ios";
/**
 * Android 11+ hands keyboard insets to the gesture area so the IME follows the
 * finger; iOS uses the transcript's native interactive dismissal.
 */
const ANDROID_INTERACTIVE =
  Platform.OS === "android" && Number(Platform.Version) >= 30;
/**
 * On iPhone the dismiss area extends up to the composer, so a drag reaching the
 * composer takes the keyboard along. iPad skips it: a minimized hardware
 * keyboard can swallow the extender's keyboard events.
 */
const EXTENDS_DISMISS_AREA = Platform.OS === "ios" && !Platform.isPad;

/** Scrolls the transcript to its newest message. */
export type TranscriptScrollToEnd = () => Promise<void>;

export interface ChatLayoutValue {
  /** Docked composer plus its bottom inset; the transcript scrolls above it. */
  dockHeight: SharedValue<number>;
  /** The same height for layout on the JavaScript side. */
  dockOffset: number;
  /**
   * Vertical dock translation, negative while the keyboard is open. Natively
   * driven by keyboard frames, so it never waits for JavaScript or React.
   */
  dockTranslateY: Animated.AnimatedInterpolation<number>;
  /** Visible keyboard height, for gesture worklets. */
  keyboardHeight: DerivedValue<number>;
  /** Bottom inset the open keyboard covers; matches the dock's offset. */
  keyboardOffset: number;
  keyboardDismissMode: "interactive" | "on-drag" | "none";
  /** The composer reports its core height (without transient panels). */
  onComposerLayout: (event: LayoutChangeEvent) => void;
  /** The transcript registers how to reach its end; returns an unregister. */
  registerScrollToEnd: (scrollToEnd: TranscriptScrollToEnd) => () => void;
  /** Closes the keyboard; after a send it also reveals the newest messages. */
  dismissKeyboard: (options?: { revealLatest?: boolean }) => void;
}

const zero = makeMutable(0);

/** Static geometry for transcripts rendered outside a chat layout (tests, previews). */
const DETACHED: ChatLayoutValue = {
  dockHeight: zero,
  dockOffset: 0,
  dockTranslateY: new Animated.Value(0).interpolate({
    inputRange: [0, 1],
    outputRange: [0, 1],
  }),
  keyboardHeight: zero,
  keyboardOffset: 0,
  keyboardDismissMode: IS_IOS ? "interactive" : "on-drag",
  onComposerLayout: () => undefined,
  registerScrollToEnd: () => () => undefined,
  dismissKeyboard: () => {
    void KeyboardController.dismiss().catch(() => undefined);
  },
};

const ChatLayoutContext = createContext<ChatLayoutValue>(DETACHED);

/** Geometry and keyboard actions shared by the transcript and the composer. */
export function useChatLayout(): ChatLayoutValue {
  return useContext(ChatLayoutContext);
}

export interface ChatLayoutProps {
  /** The transcript; it fills the screen and scrolls beneath the composer. */
  children: React.ReactNode;
  /** Composer docked to the bottom edge, riding on top of the keyboard. */
  composer?: React.ReactNode;
  /** Chrome drawn above the transcript, such as status banners. */
  overlay?: React.ReactNode;
}

/**
 * Chat surface in the style of native messengers: the transcript keeps a fixed
 * layout and only shifts its content, while the composer floats above it and
 * follows the keyboard frame by frame, including interactive dismissal.
 */
export function ChatLayout({
  children,
  composer,
  overlay,
}: ChatLayoutProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const restingInset = insets.bottom > 0 ? insets.bottom : 12;
  const keyboardOffset = Math.max(0, restingInset - KEYBOARD_GAP);
  const docked = composer != null;

  const [composerHeight, setComposerHeight] = useState(
    ESTIMATED_COMPOSER_HEIGHT,
  );
  const dockOffset = (docked ? composerHeight : 0) + restingInset;
  const dockHeight = useSharedValue(dockOffset);
  useEffect(() => {
    dockHeight.value = dockOffset;
  }, [dockHeight, dockOffset]);
  const onComposerLayout = useCallback(
    (event: LayoutChangeEvent) => {
      const height = Math.round(event.nativeEvent.layout.height);
      // The transcript inset follows on the UI thread before React re-renders.
      dockHeight.value = height + restingInset;
      setComposerHeight(height);
    },
    [dockHeight, restingInset],
  );
  const dismissArea = composerHeight + KEYBOARD_GAP;

  // Same proportional offset as the transcript's keyboard padding, so the
  // composer and the newest message move as one.
  const keyboard = useKeyboardAnimation();
  const dockTranslateY = useMemo(
    () =>
      Animated.add(
        keyboard.height,
        Animated.multiply(keyboard.progress, keyboardOffset),
      ),
    [keyboard.height, keyboard.progress, keyboardOffset],
  );
  const dockStyle = useMemo(
    () => [
      styles.dock,
      {
        paddingBottom: restingInset,
        transform: [{ translateY: dockTranslateY }],
      },
    ],
    [dockTranslateY, restingInset],
  );
  const { height: keyboardShift } = useReanimatedKeyboardAnimation();
  const keyboardHeight = useDerivedValue(() => -keyboardShift.value);

  const scrollToEndRef = useRef<TranscriptScrollToEnd | null>(null);
  const registerScrollToEnd = useCallback(
    (scrollToEnd: TranscriptScrollToEnd) => {
      scrollToEndRef.current = scrollToEnd;
      return () => {
        if (scrollToEndRef.current === scrollToEnd)
          scrollToEndRef.current = null;
      };
    },
    [],
  );
  const dismissKeyboard = useCallback(
    ({ revealLatest = false }: { revealLatest?: boolean } = {}) => {
      const closing = KeyboardController.dismiss().catch(() => undefined);
      // At the end, the transcript already lowers with the closing keyboard
      // and keeps following; otherwise it scrolls once the keyboard is down.
      if (revealLatest)
        void closing
          .then(() => scrollToEndRef.current?.())
          .catch(() => undefined);
    },
    [],
  );

  const value = useMemo<ChatLayoutValue>(
    () => ({
      dockHeight,
      dockOffset,
      dockTranslateY,
      keyboardHeight,
      keyboardOffset,
      keyboardDismissMode: IS_IOS
        ? "interactive"
        : ANDROID_INTERACTIVE
          ? "none"
          : "on-drag",
      onComposerLayout,
      registerScrollToEnd,
      dismissKeyboard,
    }),
    [
      dismissKeyboard,
      dockHeight,
      dockOffset,
      dockTranslateY,
      keyboardHeight,
      keyboardOffset,
      onComposerLayout,
      registerScrollToEnd,
    ],
  );

  return (
    <ChatLayoutContext.Provider value={value}>
      <View style={[styles.root, { backgroundColor: theme.background }]}>
        <KeyboardGestureArea
          style={styles.root}
          interpolator="ios"
          offset={docked ? dismissArea : 0}
          textInputNativeID={
            EXTENDS_DISMISS_AREA && docked
              ? COMPOSER_INPUT_NATIVE_ID
              : undefined
          }
        >
          {children}
        </KeyboardGestureArea>
        {docked ? (
          <Animated.View pointerEvents="box-none" style={dockStyle}>
            <View
              pointerEvents="none"
              style={[
                styles.scrim,
                {
                  experimental_backgroundImage: `linear-gradient(to bottom, ${theme.background}00 0%, ${theme.background}99 45%, ${theme.background}E6 100%)`,
                },
              ]}
            />
            {composer}
          </Animated.View>
        ) : null}
        {overlay}
      </View>
    </ChatLayoutContext.Provider>
  );
}

/**
 * Lets the composer be pulled down to close the keyboard. On Android 11+ the
 * keyboard follows the finger; elsewhere a short, quick pull starts the native
 * close animation and the dock rides along. Holds (caret, selection), side
 * swipes and drafts that scroll keep their native text gestures.
 */
export function ComposerDismissArea({
  enabled,
  children,
}: {
  enabled: boolean;
  /** A single native view: the composer surface. */
  children: React.ReactElement;
}): React.JSX.Element {
  const { dismissKeyboard, keyboardHeight } = useChatLayout();
  const start = useSharedValue({ x: 0, y: 0, time: 0 });
  const dismiss = useCallback(() => dismissKeyboard(), [dismissKeyboard]);
  const pull = useMemo(
    () =>
      Gesture.Pan()
        .enabled(enabled)
        .manualActivation(true)
        .onTouchesDown((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          if (
            !touch ||
            event.numberOfTouches > 1 ||
            keyboardHeight.value <= 0
          ) {
            manager.fail();
            return;
          }
          start.value = {
            x: touch.absoluteX,
            y: touch.absoluteY,
            time: Date.now(),
          };
        })
        .onTouchesMove((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          if (!touch) return;
          const dx = touch.absoluteX - start.value.x;
          const dy = touch.absoluteY - start.value.y;
          if (
            Date.now() - start.value.time > 450 ||
            dy < -8 ||
            Math.abs(dx) > 16
          )
            manager.fail();
          else if (dy >= COMPOSER_PULL && dy > Math.abs(dx) * 1.5)
            manager.activate();
        })
        .onTouchesUp((_event, manager) => {
          "worklet";
          manager.fail();
        })
        .onStart(() => {
          "worklet";
          runOnJS(dismiss)();
        }),
    [dismiss, enabled, keyboardHeight, start],
  );
  if (ANDROID_INTERACTIVE)
    return (
      <KeyboardGestureArea
        interpolator="linear"
        enableSwipeToDismiss={enabled}
        showOnSwipeUp={false}
      >
        {children}
      </KeyboardGestureArea>
    );
  return <GestureDetector gesture={pull}>{children}</GestureDetector>;
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  dock: { position: "absolute", left: 0, right: 0, bottom: 0 },
  scrim: { position: "absolute", left: 0, right: 0, top: -18, bottom: 0 },
});
