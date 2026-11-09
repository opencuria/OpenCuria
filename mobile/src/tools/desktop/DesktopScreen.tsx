import React, { forwardRef, useImperativeHandle, useRef } from "react";
import {
  Image,
  PixelRatio,
  StyleSheet,
  View,
  type ViewStyle,
} from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, {
  cancelAnimation,
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  withDecay,
  withTiming,
  type SharedValue,
} from "react-native-reanimated";
import Svg, { Path } from "react-native-svg";
import { WebView, type WebViewMessageEvent } from "react-native-webview";
import {
  screenCancel,
  screenDown,
  screenIdle,
  screenMove,
  screenUp,
  type ScreenStep,
} from "./desktopGestures";
import {
  clamp,
  clampPan,
  maxZoomFor,
  pointerOnScreen,
  screenToFramebuffer,
  viewerPageSize,
  zoomAround,
  type Size,
} from "./pointerMath";
import {
  DESKTOP_VIEWER_BRIDGE,
  isDesktopViewerNavigation,
} from "./viewerBridge";

/** Shared values describing what the screen shows; owned by the panel. */
export interface DesktopViewport {
  fb: SharedValue<Size>;
  box: SharedValue<Size>;
  /** Visual zoom (≥ 1); the page is scaled natively, never re-laid out. */
  zoom: SharedValue<number>;
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
  /** Remote framebuffer size in pixels. */
  framebuffer: Size;
  box: Size;
  /** A framed pane with rounded corners instead of edge to edge. */
  inset: boolean;
  url: string;
  /** Remount key: a new viewer session loads a fresh page. */
  viewerKey: string;
  /** Hide the page until KasmVNC reports a live connection. */
  visible: boolean;
  cursor: DesktopCursorImage | null;
  showCursor: boolean;
  /** Zoom and pan the view; also while only watching Computer Use. */
  zoomable: boolean;
  /** Taps click; off while Computer Use drives the desktop. */
  interactive: boolean;
  onMessage: (data: string) => void;
  onLoadEnd: () => void;
  onLoadError: () => void;
  /** A tap on the screen: click at this framebuffer pixel. */
  onTapAt: (x: number, y: number) => void;
  /** The zoom after the fingers left the screen. */
  onZoomChange: (zoom: number) => void;
  children?: React.ReactNode;
}

/** Below this zoom a released pinch snaps back to the fitted view. */
const SNAP_ZOOM = 1.05;

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
 * The remote screen: KasmVNC in a WebView laid out once at the framebuffer's
 * detail (`viewerPageSize`) and zoomed and panned purely natively, so zoom,
 * pan and the pointer never wait for the page or re-lay it out. The page
 * itself takes no touches; two fingers zoom around their midpoint and pan,
 * one finger pans a zoomed view (with momentum) and a tap clicks at the
 * touched pixel.
 */
export const DesktopScreen = forwardRef<
  DesktopScreenHandle,
  DesktopScreenProps
>(function DesktopScreen(
  {
    viewport,
    framebuffer,
    box,
    inset,
    url,
    viewerKey,
    visible,
    cursor,
    showCursor,
    zoomable,
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
  const { fb, zoom, panX, panY, pointerX, pointerY } = viewport;
  const machine = useSharedValue(screenIdle());
  const page = viewerPageSize(framebuffer, box, PixelRatio.get());

  const content = useAnimatedStyle(() => {
    const scale = page.width > 0 ? (box.width / page.width) * zoom.value : 1;
    // Scale around the top-left corner: RN transforms scale around the center.
    return {
      transform: [
        { translateX: -panX.value - (page.width / 2) * (1 - scale) },
        { translateY: -panY.value - (page.height / 2) * (1 - scale) },
        { scale },
      ],
    };
  }, [box.width, page.width, page.height]);

  const size = cursorSize(cursor);
  const cursorStyle = useAnimatedStyle(() => {
    const at = pointerOnScreen(
      { x: pointerX.value, y: pointerY.value },
      fb.value,
      viewport.box.value,
      zoom.value,
      { x: panX.value, y: panY.value },
    );
    return {
      transform: [
        { translateX: at.x - size.hotX },
        { translateY: at.y - size.hotY },
      ],
    };
  }, [size.hotX, size.hotY]);

  const run = (step: ScreenStep) => {
    "worklet";
    machine.value = step.state;
    const area = viewport.box.value;
    for (const action of step.actions) {
      if (action.type === "stop") {
        cancelAnimation(zoom);
        cancelAnimation(panX);
        cancelAnimation(panY);
      } else if (action.type === "pan") {
        const pan = clampPan(
          { x: panX.value - action.dx, y: panY.value - action.dy },
          area,
          zoom.value,
        );
        panX.value = pan.x;
        panY.value = pan.y;
      } else if (action.type === "zoom") {
        const next = clamp(
          zoom.value * action.scale,
          1,
          maxZoomFor(fb.value, area),
        );
        const pan = zoomAround(
          zoom.value,
          next,
          { x: panX.value, y: panY.value },
          { x: action.x, y: action.y },
          area,
        );
        zoom.value = next;
        panX.value = pan.x;
        panY.value = pan.y;
      } else if (action.type === "tap") {
        if (!interactive) continue;
        const at = screenToFramebuffer(
          { x: action.x, y: action.y },
          fb.value,
          area,
          zoom.value,
          { x: panX.value, y: panY.value },
        );
        runOnJS(onTapAt)(at.x, at.y);
      } else if (action.type === "fling") {
        const maxX = Math.max(0, area.width * zoom.value - area.width);
        const maxY = Math.max(0, area.height * zoom.value - area.height);
        // Velocities are finger movement in points/ms; the pan runs opposite.
        panX.value = withDecay({
          velocity: -action.vx * 1000,
          clamp: [0, maxX],
        });
        panY.value = withDecay({
          velocity: -action.vy * 1000,
          clamp: [0, maxY],
        });
        runOnJS(onZoomChange)(zoom.value);
      } else if (zoom.value < SNAP_ZOOM) {
        // Settled almost fitted: snap back to the whole screen.
        const timing = { duration: 180 };
        zoom.value = withTiming(1, timing);
        panX.value = withTiming(0, timing);
        panY.value = withTiming(0, timing);
        runOnJS(onZoomChange)(1);
      } else {
        runOnJS(onZoomChange)(zoom.value);
      }
    }
  };

  const gesture = Gesture.Manual()
    .enabled(zoomable)
    .onTouchesDown((event, manager) => {
      manager.activate();
      run(screenDown(machine.value, event.changedTouches, performance.now()));
    })
    .onTouchesMove((event) => {
      run(screenMove(machine.value, event.allTouches, performance.now()));
    })
    .onTouchesUp((event, manager) => {
      run(screenUp(machine.value, event.changedTouches, performance.now()));
      if (machine.value.down.length === 0) manager.end();
    })
    .onTouchesCancelled((_event, manager) => {
      run(screenCancel(machine.value));
      manager.end();
    })
    .onFinalize(() => {
      if (machine.value.phase !== "idle") run(screenCancel(machine.value));
    });

  return (
    <GestureDetector gesture={gesture}>
      <View
        testID="desktop-screen"
        accessibilityLabel="Remote desktop"
        style={[
          styles.box,
          inset ? styles.inset : styles.edge,
          { width: box.width, height: box.height },
        ]}
        collapsable={false}
      >
        <Animated.View
          style={[
            styles.content,
            { width: page.width, height: page.height },
            content,
            visible ? null : styles.hidden,
          ]}
          pointerEvents="none"
        >
          {url ? (
            <WebView
              key={viewerKey}
              ref={webViewRef}
              source={{ uri: url }}
              // Every navigation reaches the filter below, which keeps the
              // WebView on this desktop's route; nothing is opened externally.
              originWhitelist={["*"]}
              onShouldStartLoadWithRequest={(request) =>
                isDesktopViewerNavigation(url, request.url)
              }
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
    // A dark pane that reads as a screen in both themes, also while nothing
    // is connected.
    backgroundColor: "#111113",
  } satisfies ViewStyle,
  inset: {
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: "rgba(255, 255, 255, 0.12)",
  } satisfies ViewStyle,
  // Android does not hold a zoomed WebView inside a plain rectangular clip:
  // the page spills over the panel. Any rounded clip, even this one, holds it.
  edge: { borderRadius: 1 } satisfies ViewStyle,
  content: { position: "absolute", left: 0, top: 0 },
  hidden: { opacity: 0 },
  webView: { flex: 1, backgroundColor: "#000000" },
  webViewContainer: { flex: 1, backgroundColor: "#000000" },
  cursor: { position: "absolute", left: 0, top: 0 },
});
