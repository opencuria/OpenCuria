import React, { forwardRef, useImperativeHandle, useRef } from "react";
import { Image, StyleSheet, View, type ViewStyle } from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, {
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  type SharedValue,
} from "react-native-reanimated";
import Svg, { Path } from "react-native-svg";
import { WebView, type WebViewMessageEvent } from "react-native-webview";
import {
  clamp,
  clampPan,
  maxZoomFor,
  pointerOnScreen,
  screenToFramebuffer,
  zoomAround,
  type Size,
} from "./pointerMath";
import { DESKTOP_VIEWER_BRIDGE } from "./viewerBridge";

/** Shared values describing what the screen shows; owned by the panel. */
export interface DesktopViewport {
  fb: SharedValue<Size>;
  box: SharedValue<Size>;
  /** Zoom the WebView is laid out at. */
  zoom: SharedValue<number>;
  /** Extra scale while a pinch is in progress. */
  pinch: SharedValue<number>;
  panX: SharedValue<number>;
  panY: SharedValue<number>;
  pointerX: SharedValue<number>;
  pointerY: SharedValue<number>;
}

export function useDesktopViewport(initialFb: Size): DesktopViewport {
  return {
    fb: useSharedValue(initialFb),
    box: useSharedValue({ width: 0, height: 0 }),
    zoom: useSharedValue(1),
    pinch: useSharedValue(1),
    panX: useSharedValue(0),
    panY: useSharedValue(0),
    pointerX: useSharedValue(initialFb.width / 2),
    pointerY: useSharedValue(initialFb.height / 2),
  };
}

/** Remote cursor image reported by the viewer, in framebuffer pixels. */
export interface DesktopCursorImage {
  uri: string;
  width: number;
  height: number;
  hotX: number;
  hotY: number;
}

export interface DesktopScreenHandle {
  inject: (script: string) => void;
}

export interface DesktopScreenProps {
  viewport: DesktopViewport;
  box: Size;
  url: string;
  /** Remount key: a new viewer session loads a fresh page. */
  viewerKey: string;
  /** Hide the page until KasmVNC reports a live connection. */
  visible: boolean;
  cursor: DesktopCursorImage | null;
  showCursor: boolean;
  /** Taps and zoom are ignored while Computer Use drives the desktop. */
  interactive: boolean;
  onMessage: (data: string) => void;
  onLoadEnd: () => void;
  onLoadError: () => void;
  /** A tap on the screen: click at this framebuffer pixel. */
  onTapAt: (x: number, y: number) => void;
  onZoomChange: (zoom: number) => void;
  children?: React.ReactNode;
}

/** Longest side of the native cursor, whatever the remote image size. */
const CURSOR_MAX_POINTS = 26;
const DEFAULT_CURSOR = { width: 17, height: 25, hotX: 1.5, hotY: 1.5 };

function cursorSize(cursor: DesktopCursorImage | null) {
  if (!cursor) return DEFAULT_CURSOR;
  const scale = Math.min(
    0.85,
    CURSOR_MAX_POINTS / Math.max(cursor.width, cursor.height),
  );
  return {
    width: cursor.width * scale,
    height: cursor.height * scale,
    hotX: cursor.hotX * scale,
    hotY: cursor.hotY * scale,
  };
}

/**
 * The remote screen: KasmVNC in a WebView laid out at the committed zoom and
 * panned natively, so panning and the pointer never wait for the page. The
 * page itself takes no touches; pinch zooms, one or two fingers pan a zoomed
 * view and a tap clicks at the touched pixel.
 */
export const DesktopScreen = forwardRef<
  DesktopScreenHandle,
  DesktopScreenProps
>(function DesktopScreen(
  {
    viewport,
    box,
    url,
    viewerKey,
    visible,
    cursor,
    showCursor,
    interactive,
    onMessage,
    onLoadEnd,
    onLoadError,
    onTapAt,
    onZoomChange,
    children,
  },
  ref,
) {
  const webViewRef = useRef<WebView>(null);
  useImperativeHandle(ref, () => ({
    inject: (script: string) => webViewRef.current?.injectJavaScript(script),
  }));
  const { fb, zoom, pinch, panX, panY, pointerX, pointerY } = viewport;
  const pinchStart = useSharedValue(1);
  const focal = useSharedValue({ x: 0, y: 0 });

  const content = useAnimatedStyle(() => {
    const width = viewport.box.value.width * zoom.value;
    const height = viewport.box.value.height * zoom.value;
    const scale = pinch.value;
    // Scale around the top-left corner: RN transforms scale around the center.
    return {
      width,
      height,
      transform: [
        { translateX: -panX.value - (width / 2) * (1 - scale) },
        { translateY: -panY.value - (height / 2) * (1 - scale) },
        { scale },
      ],
    };
  });

  const size = cursorSize(cursor);
  const cursorStyle = useAnimatedStyle(() => {
    const at = pointerOnScreen(
      { x: pointerX.value, y: pointerY.value },
      fb.value,
      viewport.box.value,
      zoom.value * pinch.value,
      { x: panX.value, y: panY.value },
    );
    return {
      transform: [
        { translateX: at.x - size.hotX },
        { translateY: at.y - size.hotY },
      ],
    };
  }, [size.hotX, size.hotY]);

  const pinchGesture = Gesture.Pinch()
    .enabled(interactive)
    .onStart((event) => {
      pinchStart.value = zoom.value;
      focal.value = { x: event.focalX, y: event.focalY };
    })
    .onUpdate((event) => {
      const max = maxZoomFor(fb.value, viewport.box.value);
      const previous = zoom.value * pinch.value;
      const next = clamp(pinchStart.value * event.scale, 1, max);
      const pan = zoomAround(
        previous,
        next,
        { x: panX.value, y: panY.value },
        { x: event.focalX, y: event.focalY },
        viewport.box.value,
      );
      pinch.value = next / zoom.value;
      panX.value = pan.x;
      panY.value = pan.y;
    })
    .onEnd(() => {
      let next = zoom.value * pinch.value;
      if (next < 1.08) next = 1;
      const pan = clampPan(
        { x: panX.value, y: panY.value },
        viewport.box.value,
        next,
      );
      zoom.value = next;
      pinch.value = 1;
      panX.value = pan.x;
      panY.value = pan.y;
      runOnJS(onZoomChange)(next);
    });

  const panGesture = Gesture.Pan()
    .enabled(interactive)
    .maxPointers(2)
    .minDistance(6)
    .onChange((event) => {
      const pan = clampPan(
        { x: panX.value - event.changeX, y: panY.value - event.changeY },
        viewport.box.value,
        zoom.value * pinch.value,
      );
      panX.value = pan.x;
      panY.value = pan.y;
    });

  const tapGesture = Gesture.Tap()
    .enabled(interactive)
    .maxDuration(300)
    .maxDistance(12)
    .onEnd((event, success) => {
      if (!success) return;
      const at = screenToFramebuffer(
        { x: event.x, y: event.y },
        fb.value,
        viewport.box.value,
        zoom.value * pinch.value,
        { x: panX.value, y: panY.value },
      );
      runOnJS(onTapAt)(at.x, at.y);
    });

  const gesture = Gesture.Race(
    Gesture.Simultaneous(pinchGesture, panGesture),
    tapGesture,
  );

  return (
    <GestureDetector gesture={gesture}>
      <View
        testID="desktop-screen"
        accessibilityLabel="Remote desktop"
        style={[styles.box, { width: box.width, height: box.height }]}
        collapsable={false}
      >
        <Animated.View
          style={[styles.content, content, visible ? null : styles.hidden]}
          pointerEvents="none"
        >
          {url ? (
            <WebView
              key={viewerKey}
              ref={webViewRef}
              source={{ uri: url }}
              originWhitelist={["*"]}
              injectedJavaScriptBeforeContentLoaded={DESKTOP_VIEWER_BRIDGE}
              injectedJavaScript={DESKTOP_VIEWER_BRIDGE}
              onMessage={(event: WebViewMessageEvent) =>
                onMessage(event.nativeEvent.data)
              }
              onLoadEnd={onLoadEnd}
              onError={onLoadError}
              onHttpError={onLoadError}
              onRenderProcessGone={onLoadError}
              onContentProcessDidTerminate={onLoadError}
              javaScriptEnabled
              domStorageEnabled
              scrollEnabled={false}
              bounces={false}
              overScrollMode="never"
              setBuiltInZoomControls={false}
              setSupportMultipleWindows={false}
              allowsLinkPreview={false}
              textInteractionEnabled={false}
              keyboardDisplayRequiresUserAction
              hideKeyboardAccessoryView
              mediaPlaybackRequiresUserAction
              allowsInlineMediaPlayback={false}
              webviewDebuggingEnabled={__DEV__}
              style={styles.webView}
              containerStyle={styles.webViewContainer}
              testID="desktop-webview"
            />
          ) : null}
        </Animated.View>
        {showCursor ? (
          <Animated.View
            pointerEvents="none"
            style={[
              styles.cursor,
              { width: size.width, height: size.height },
              cursorStyle,
            ]}
            testID="desktop-cursor"
          >
            {cursor ? (
              <Image
                source={{ uri: cursor.uri }}
                style={{ width: size.width, height: size.height }}
                fadeDuration={0}
              />
            ) : (
              <ArrowCursor />
            )}
          </Animated.View>
        ) : null}
        {children}
      </View>
    </GestureDetector>
  );
});

/** The pointer until the remote desktop reports its own cursor image. */
function ArrowCursor(): React.JSX.Element {
  return (
    <Svg
      width={DEFAULT_CURSOR.width}
      height={DEFAULT_CURSOR.height}
      viewBox="0 0 17 25"
    >
      <Path
        d="M1.5 1.5 L1.5 20.5 L6.2 16.2 L9.4 23.3 L12.6 21.9 L9.5 15 L15.5 15 Z"
        fill="#FFFFFF"
        stroke="#111111"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
    </Svg>
  );
}

const styles = StyleSheet.create({
  box: {
    overflow: "hidden",
    // A dark glass pane that reads as a screen in both themes, also while
    // nothing is connected.
    backgroundColor: "#111113",
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: "rgba(255, 255, 255, 0.12)",
  } satisfies ViewStyle,
  content: { position: "absolute", left: 0, top: 0 },
  hidden: { opacity: 0 },
  webView: { flex: 1, backgroundColor: "#000000" },
  webViewContainer: { flex: 1, backgroundColor: "#000000" },
  cursor: { position: "absolute", left: 0, top: 0 },
});
