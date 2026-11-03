import React, {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ActivityIndicator,
  Alert,
  Modal,
  Platform,
  ScrollView,
  Share,
  StyleSheet,
  Text,
  View,
  useWindowDimensions,
  type LayoutChangeEvent,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";
import { Image } from "expo-image";
import * as Sharing from "expo-sharing";
import { StatusBar } from "expo-status-bar";
import {
  Gesture,
  GestureDetector,
  GestureHandlerRootView,
} from "react-native-gesture-handler";
import Animated, {
  cancelAnimation,
  interpolateColor,
  useAnimatedStyle,
  useSharedValue,
  withDecay,
  withSpring,
  withTiming,
  type SharedValue,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { runOnJS } from "react-native-worklets";
import { Button, NativeAction } from "../ui/controls";
import { MediaViewerFrame } from "../ui/MediaViewerFrame";
import { useTheme } from "../ui/theme";
import { mediaKey, type MediaGallery } from "./chatGallery";
import type { MarkdownMedia } from "./markdownMedia";
import {
  useWorkspaceMedia,
  type ResolvedChatMedia,
  type WorkspaceMediaState,
} from "./useWorkspaceMedia";

/**
 * Opens a tapped picture in the host's gallery. `siblings` are the pictures
 * shown beside it, for pictures the host does not list (e.g. tool output).
 */
export type OpenMediaGallery = (
  media: MarkdownMedia,
  siblings: readonly MarkdownMedia[],
) => void;

/** Provided by the transcript so every picture opens in one chat gallery. */
export const MediaGalleryContext = createContext<OpenMediaGallery | null>(null);

export interface MediaViewerProps {
  gallery: MediaGallery;
  resolveMedia?: (path: string) => Promise<ResolvedChatMedia>;
  /** Media-cache scope, as for the transcript's inline pictures. */
  scope: string;
  onClose: () => void;
}

const MAX_SCALE = 4;
const DOUBLE_TAP_SCALE = 2.5;
const THUMB = 40;
const THUMB_GAP = 4;
const THUMB_STEP = THUMB + THUMB_GAP;
/** Thumbnails loaded around the strip's visible range before it reports one. */
const THUMB_WINDOW = 4;
const FLING_VELOCITY = 500;
/** Share of the width a slow drag needs to turn the page. */
const PAGE_DISTANCE = 0.25;
const DISMISS_DISTANCE = 100;
const DISMISS_VELOCITY = 800;
/** Critically damped: pages settle without bouncing past their edge. */
const PAGE_SPRING = {
  mass: 1,
  stiffness: 300,
  damping: 35,
  overshootClamping: true,
};
const FADE = { duration: 180 };
const ORIENTATIONS = [
  "portrait",
  "portrait-upside-down",
  "landscape",
  "landscape-left",
  "landscape-right",
] as const;

const enum Drag {
  None,
  /** Moving, direction not yet known. */
  Undecided,
  Page,
  Pan,
  Dismiss,
}

const LOADING: WorkspaceMediaState = { status: "loading" };
const FAILED: WorkspaceMediaState = { status: "failed" };

/** Furthest a picture of `content` points, scaled by `scale`, may move in `view`. */
function panLimit(content: number, scale: number, view: number): number {
  "worklet";
  return Math.max(0, (content * scale - view) / 2);
}

function clamp(value: number, min: number, max: number): number {
  "worklet";
  return Math.min(max, Math.max(min, value));
}

/** Movement past an edge follows the finger at a third of its speed. */
function resist(value: number, min: number, max: number): number {
  "worklet";
  if (value > max) return max + (value - max) / 3;
  if (value < min) return min + (value - min) / 3;
  return value;
}

/**
 * Full-screen picture viewer in the manner of messaging apps: a native bar
 * with back and share, horizontal paging through the gallery, pinch and
 * double-tap zoom, a thumbnail strip, tap to hide the chrome and a vertical
 * swipe to close. It holds its own media leases while open.
 */
export function MediaViewer({
  gallery,
  resolveMedia,
  scope,
  onClose,
}: MediaViewerProps): React.JSX.Element | null {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const dimensions = useWindowDimensions();
  const { items } = gallery;
  const count = items.length;
  const [size, setSize] = useState({
    width: dimensions.width,
    height: dimensions.height,
  });
  const [index, setIndex] = useState(() =>
    Math.min(Math.max(0, gallery.index), Math.max(0, count - 1)),
  );
  const [chrome, setChrome] = useState(true);
  const [thumbRange, setThumbRange] = useState<[number, number]>(() => [
    index - THUMB_WINDOW,
    index + THUMB_WINDOW,
  ]);
  const [natural, setNatural] = useState<
    Record<string, { width: number; height: number }>
  >({});

  const paths = useMemo(() => {
    const wanted = new Set<string>();
    const add = (position: number) => {
      const path = items[position]?.path;
      if (path) wanted.add(path);
    };
    for (let position = index - 1; position <= index + 1; position += 1)
      add(position);
    if (count > 1)
      for (
        let position = thumbRange[0];
        position <= thumbRange[1];
        position += 1
      )
        add(position);
    return [...wanted];
  }, [count, index, items, thumbRange]);
  const { media, retry, reportDisplayError } = useWorkspaceMedia({
    paths,
    resolve: resolveMedia,
    scope,
    active: true,
  });
  const stateOf = useCallback(
    (item: MarkdownMedia | undefined): WorkspaceMediaState | undefined => {
      if (!item) return undefined;
      if (!item.path)
        return item.uri ? { status: "ready", uri: item.uri } : FAILED;
      if (!resolveMedia) return FAILED;
      return media[item.path];
    },
    [media, resolveMedia],
  );

  // Gesture state lives on the UI thread; JavaScript owns the page index.
  const width = useSharedValue(size.width);
  const height = useSharedValue(size.height);
  const pageX = useSharedValue(-index * size.width);
  const current = useSharedValue(index);
  const last = useSharedValue(count - 1);
  const scale = useSharedValue(1);
  const offsetX = useSharedValue(0);
  const offsetY = useSharedValue(0);
  const contentWidth = useSharedValue(size.width);
  const contentHeight = useSharedValue(size.height);
  const dismissY = useSharedValue(0);
  /** Fades the picture and its backdrop as a swipe carries it away. */
  const fade = useSharedValue(1);
  const dark = useSharedValue(0);
  const drag = useSharedValue(Drag.None);
  const pinching = useSharedValue(false);
  const rebase = useSharedValue(false);
  const start = useSharedValue({
    page: 0,
    x: 0,
    y: 0,
    scale: 1,
    focalX: 0,
    focalY: 0,
  });
  const closing = useRef(false);
  const indexRef = useRef(index);
  indexRef.current = index;
  /** Page a swipe already moved the pager to. */
  const swiped = useRef<number | null>(null);

  useEffect(() => {
    dark.value = withTiming(chrome ? 0 : 1, FADE);
  }, [chrome, dark]);
  useEffect(() => {
    last.value = count - 1;
  }, [count, last]);
  useEffect(() => {
    width.value = size.width;
    height.value = size.height;
    cancelAnimation(pageX);
    pageX.value = -indexRef.current * size.width;
    scale.value = 1;
    offsetX.value = 0;
    offsetY.value = 0;
  }, [height, offsetX, offsetY, pageX, scale, size, width]);
  // A swipe has already moved the pager; a thumbnail jumps straight there.
  const widthRef = useRef(size.width);
  widthRef.current = size.width;
  useEffect(() => {
    scale.value = 1;
    offsetX.value = 0;
    offsetY.value = 0;
    // Pages a swipe reports arrive in order; the pager already shows the last.
    if (swiped.current !== null) {
      if (swiped.current === index) swiped.current = null;
      return;
    }
    current.value = index;
    cancelAnimation(pageX);
    pageX.value = -index * widthRef.current;
  }, [current, index, offsetX, offsetY, pageX, scale]);

  const currentItem = items[index];
  const currentKey = currentItem ? mediaKey(currentItem) : "";
  const naturalSize = natural[currentKey];
  useEffect(() => {
    const fit = naturalSize
      ? Math.min(
          size.width / naturalSize.width,
          size.height / naturalSize.height,
        )
      : 1;
    contentWidth.value = naturalSize ? naturalSize.width * fit : size.width;
    contentHeight.value = naturalSize ? naturalSize.height * fit : size.height;
  }, [contentHeight, contentWidth, naturalSize, size]);

  // The system fades the modal in and out; a swipe fades it away itself.
  const close = useCallback(() => {
    if (closing.current) return;
    closing.current = true;
    onClose();
  }, [onClose]);
  const toggleChrome = useCallback(() => setChrome((shown) => !shown), []);
  const show = useCallback(
    (position: number) => {
      if (position >= 0 && position < count) setIndex(position);
    },
    [count],
  );
  const settlePage = useCallback((position: number) => {
    swiped.current = position;
    setIndex(position);
  }, []);

  const gesture = useMemo(() => {
    const settleZoom = () => {
      "worklet";
      const target = clamp(scale.value, 1, MAX_SCALE);
      const ratio = target / scale.value;
      const limitX = panLimit(contentWidth.value, target, width.value);
      const limitY = panLimit(contentHeight.value, target, height.value);
      scale.value = withTiming(target, FADE);
      offsetX.value = withTiming(
        target === 1 ? 0 : clamp(offsetX.value * ratio, -limitX, limitX),
        FADE,
      );
      offsetY.value = withTiming(
        target === 1 ? 0 : clamp(offsetY.value * ratio, -limitY, limitY),
        FADE,
      );
    };
    const pinch = Gesture.Pinch()
      .onStart((event) => {
        "worklet";
        pinching.value = true;
        rebase.value = true;
        cancelAnimation(scale);
        cancelAnimation(offsetX);
        cancelAnimation(offsetY);
        start.value = {
          ...start.value,
          x: offsetX.value,
          y: offsetY.value,
          scale: scale.value,
          focalX: event.focalX - width.value / 2,
          focalY: event.focalY - height.value / 2,
        };
      })
      .onUpdate((event) => {
        "worklet";
        const origin = start.value;
        const next = clamp(origin.scale * event.scale, 0.5, MAX_SCALE * 1.5);
        const ratio = next / origin.scale;
        // The point first pinched stays beneath the fingers as they move.
        offsetX.value =
          event.focalX - width.value / 2 - (origin.focalX - origin.x) * ratio;
        offsetY.value =
          event.focalY - height.value / 2 - (origin.focalY - origin.y) * ratio;
        scale.value = next;
      })
      .onEnd(() => {
        "worklet";
        settleZoom();
      })
      .onFinalize(() => {
        "worklet";
        pinching.value = false;
      });

    const pan = Gesture.Pan()
      .withTestId("image-preview-pan")
      .minDistance(8)
      .averageTouches(true)
      .onStart((event) => {
        "worklet";
        rebase.value = false;
        if (pinching.value) drag.value = Drag.None;
        else if (scale.value > 1.01) {
          drag.value = Drag.Pan;
          cancelAnimation(offsetX);
          cancelAnimation(offsetY);
        } else drag.value = Drag.Undecided;
        start.value = {
          ...start.value,
          page: pageX.value,
          x: offsetX.value - event.translationX,
          y: offsetY.value - event.translationY,
        };
      })
      .onUpdate((event) => {
        "worklet";
        if (pinching.value) return;
        if (rebase.value) {
          // A pinch moved the picture; continue from where it left it.
          rebase.value = false;
          if (scale.value > 1.01) drag.value = Drag.Pan;
          start.value = {
            ...start.value,
            x: offsetX.value - event.translationX,
            y: offsetY.value - event.translationY,
          };
        }
        if (drag.value === Drag.Undecided) {
          // Android starts without movement; the first move picks the axis.
          const dx = Math.abs(event.translationX);
          const dy = Math.abs(event.translationY);
          if (dx < 2 && dy < 2) return;
          drag.value = dx >= dy ? Drag.Page : Drag.Dismiss;
          // A vertical swipe leaves a settling page alone.
          if (drag.value === Drag.Page) {
            cancelAnimation(pageX);
            start.value = { ...start.value, page: pageX.value };
          }
        }
        if (drag.value === Drag.Page) {
          pageX.value = resist(
            start.value.page + event.translationX,
            -last.value * width.value,
            0,
          );
        } else if (drag.value === Drag.Pan) {
          const limitX = panLimit(contentWidth.value, scale.value, width.value);
          const limitY = panLimit(
            contentHeight.value,
            scale.value,
            height.value,
          );
          offsetX.value = resist(
            start.value.x + event.translationX,
            -limitX,
            limitX,
          );
          offsetY.value = resist(
            start.value.y + event.translationY,
            -limitY,
            limitY,
          );
        } else if (drag.value === Drag.Dismiss) {
          dismissY.value = event.translationY;
        }
      })
      .onEnd((event) => {
        "worklet";
        if (drag.value === Drag.Page) {
          const from = current.value;
          const moved = pageX.value + from * width.value;
          let target = from;
          if (Math.abs(event.velocityX) > FLING_VELOCITY)
            target = event.velocityX < 0 ? from + 1 : from - 1;
          else if (Math.abs(moved) > width.value * PAGE_DISTANCE)
            target = moved < 0 ? from + 1 : from - 1;
          target = clamp(target, 0, last.value);
          pageX.value = withSpring(-target * width.value, {
            ...PAGE_SPRING,
            velocity: event.velocityX,
          });
          if (target !== from) {
            current.value = target;
            runOnJS(settlePage)(target);
          }
        } else if (drag.value === Drag.Pan) {
          const limitX = panLimit(contentWidth.value, scale.value, width.value);
          const limitY = panLimit(
            contentHeight.value,
            scale.value,
            height.value,
          );
          offsetX.value = withDecay({
            velocity: event.velocityX,
            clamp: [-limitX, limitX],
            rubberBandEffect: true,
          });
          offsetY.value = withDecay({
            velocity: event.velocityY,
            clamp: [-limitY, limitY],
            rubberBandEffect: true,
          });
        } else if (drag.value === Drag.Dismiss) {
          const away =
            Math.abs(dismissY.value) > DISMISS_DISTANCE ||
            Math.abs(event.velocityY) > DISMISS_VELOCITY;
          if (away) {
            const direction =
              (Math.abs(event.velocityY) > DISMISS_VELOCITY
                ? event.velocityY
                : dismissY.value) < 0
                ? -1
                : 1;
            fade.value = withTiming(0, FADE);
            dismissY.value = withTiming(direction * height.value, FADE, () =>
              runOnJS(close)(),
            );
          } else {
            dismissY.value = withSpring(0, PAGE_SPRING);
          }
        }
        drag.value = Drag.None;
      });

    const doubleTap = Gesture.Tap()
      .numberOfTaps(2)
      .maxDuration(250)
      .onEnd((event, success) => {
        "worklet";
        if (!success) return;
        if (scale.value > 1.01) {
          scale.value = withTiming(1, FADE);
          offsetX.value = withTiming(0, FADE);
          offsetY.value = withTiming(0, FADE);
          return;
        }
        // Zoom into the tapped point, keeping the picture on screen.
        const focusX = event.x - width.value / 2;
        const focusY = event.y - height.value / 2;
        const limitX = panLimit(
          contentWidth.value,
          DOUBLE_TAP_SCALE,
          width.value,
        );
        const limitY = panLimit(
          contentHeight.value,
          DOUBLE_TAP_SCALE,
          height.value,
        );
        scale.value = withTiming(DOUBLE_TAP_SCALE, FADE);
        offsetX.value = withTiming(
          clamp(focusX * (1 - DOUBLE_TAP_SCALE), -limitX, limitX),
          FADE,
        );
        offsetY.value = withTiming(
          clamp(focusY * (1 - DOUBLE_TAP_SCALE), -limitY, limitY),
          FADE,
        );
      });
    const singleTap = Gesture.Tap()
      .withTestId("image-preview-tap")
      .onEnd((_event, success) => {
        "worklet";
        if (success) runOnJS(toggleChrome)();
      });

    return Gesture.Race(
      Gesture.Simultaneous(pinch, pan),
      Gesture.Exclusive(doubleTap, singleTap),
    );
  }, [
    close,
    contentHeight,
    contentWidth,
    current,
    dismissY,
    drag,
    fade,
    height,
    last,
    offsetX,
    offsetY,
    pageX,
    pinching,
    rebase,
    scale,
    settlePage,
    start,
    toggleChrome,
    width,
  ]);

  const backdropStyle = useAnimatedStyle(() => ({
    opacity:
      fade.value *
      (1 - Math.min(1, Math.abs(dismissY.value) / (height.value * 0.6))),
    backgroundColor: interpolateColor(
      dark.value,
      [0, 1],
      [theme.background, "#000000"],
    ),
  }));
  const stageStyle = useAnimatedStyle(() => ({
    opacity: fade.value,
    transform: [
      { translateY: dismissY.value },
      {
        scale: 1 - Math.min(1, Math.abs(dismissY.value) / height.value) * 0.25,
      },
    ],
  }));
  const trackStyle = useAnimatedStyle(() => ({
    transform: [{ translateX: pageX.value }],
  }));
  const zoom = useMemo(
    () => ({ current, scale, offsetX, offsetY }),
    [current, offsetX, offsetY, scale],
  );

  const onLayout = useCallback((event: LayoutChangeEvent) => {
    const { width: nextWidth, height: nextHeight } = event.nativeEvent.layout;
    setSize((previous) =>
      previous.width === nextWidth && previous.height === nextHeight
        ? previous
        : { width: nextWidth, height: nextHeight },
    );
  }, []);
  const rememberSize = useCallback(
    (key: string, naturalWidth: number, naturalHeight: number) => {
      if (!naturalWidth || !naturalHeight) return;
      setNatural((previous) =>
        previous[key]?.width === naturalWidth &&
        previous[key]?.height === naturalHeight
          ? previous
          : {
              ...previous,
              [key]: { width: naturalWidth, height: naturalHeight },
            },
      );
    },
    [],
  );

  const currentState = stateOf(currentItem);
  const share = useCallback(async () => {
    if (!currentItem) return;
    try {
      if (currentItem.path) {
        if (currentState?.status !== "ready") return;
        if (!(await Sharing.isAvailableAsync()))
          throw new Error("Sharing is not available on this device.");
        await Sharing.shareAsync(currentState.uri, {
          dialogTitle: currentItem.label,
        });
      } else if (currentItem.uri) {
        await Share.share(
          Platform.OS === "ios"
            ? { url: currentItem.uri }
            : { message: currentItem.uri },
        );
      }
    } catch (error) {
      Alert.alert(
        "Can't share image",
        error instanceof Error ? error.message : "Please try again.",
      );
    }
  }, [currentItem, currentState]);

  if (!currentItem) return null;
  const pages: number[] = [];
  for (let position = index - 1; position <= index + 1; position += 1)
    if (position >= 0 && position < count) pages.push(position);
  const positionLabel = count > 1 ? `${index + 1} of ${count}` : "";

  return (
    <Modal
      visible
      transparent
      animationType="fade"
      statusBarTranslucent
      navigationBarTranslucent
      supportedOrientations={[...ORIENTATIONS]}
      onRequestClose={close}
    >
      <StatusBar
        hidden={!chrome}
        style={chrome && theme.scheme === "light" ? "dark" : "light"}
        animated
      />
      <GestureHandlerRootView
        accessibilityViewIsModal
        onAccessibilityEscape={close}
        style={styles.fill}
      >
        <MediaViewerFrame
          title={currentItem.label || "Image"}
          chromeVisible={chrome}
          onBack={close}
          onShare={() => void share()}
          shareDisabled={
            currentItem.path ? currentState?.status !== "ready" : false
          }
        >
          <View testID="image-preview" onLayout={onLayout} style={styles.fill}>
            <Animated.View
              pointerEvents="none"
              style={[StyleSheet.absoluteFill, backdropStyle]}
            />
            <GestureDetector gesture={gesture}>
              <Animated.View
                accessible
                accessibilityRole="adjustable"
                accessibilityLabel={currentItem.label || "Image"}
                accessibilityValue={
                  positionLabel ? { text: positionLabel } : undefined
                }
                accessibilityActions={[
                  { name: "increment" },
                  { name: "decrement" },
                  { name: "activate" },
                ]}
                onAccessibilityAction={({ nativeEvent }) => {
                  if (nativeEvent.actionName === "increment") show(index + 1);
                  else if (nativeEvent.actionName === "decrement")
                    show(index - 1);
                  else if (
                    currentState?.status === "failed" &&
                    currentItem.path
                  )
                    retry(currentItem.path);
                  else toggleChrome();
                }}
                style={[styles.fill, stageStyle]}
              >
                <Animated.View
                  style={[
                    styles.track,
                    { width: size.width * count, height: size.height },
                    trackStyle,
                  ]}
                >
                  {pages.map((position) => {
                    const item = items[position]!;
                    const key = mediaKey(item);
                    return (
                      <ViewerPage
                        key={`${key}:${position}`}
                        item={item}
                        state={stateOf(item) ?? LOADING}
                        position={position}
                        width={size.width}
                        height={size.height}
                        zoom={zoom}
                        onSize={(naturalWidth, naturalHeight) =>
                          rememberSize(key, naturalWidth, naturalHeight)
                        }
                        onRetry={() => item.path && retry(item.path)}
                        onDisplayError={() =>
                          item.path && reportDisplayError(item.path)
                        }
                      />
                    );
                  })}
                </Animated.View>
              </Animated.View>
            </GestureDetector>
            {count > 1 ? (
              <ThumbnailStrip
                items={items}
                index={index}
                width={size.width}
                visible={chrome}
                bottomInset={insets.bottom}
                stateOf={stateOf}
                onSelect={show}
                onRangeChange={setThumbRange}
              />
            ) : null}
          </View>
        </MediaViewerFrame>
      </GestureHandlerRootView>
    </Modal>
  );
}

interface PageZoom {
  /** Page being shown; only it follows pinch and pan. */
  current: SharedValue<number>;
  scale: SharedValue<number>;
  offsetX: SharedValue<number>;
  offsetY: SharedValue<number>;
}

function ViewerPage({
  item,
  state,
  position,
  width,
  height,
  zoom,
  onSize,
  onRetry,
  onDisplayError,
}: {
  item: MarkdownMedia;
  state: WorkspaceMediaState;
  position: number;
  width: number;
  height: number;
  zoom: PageZoom;
  onSize: (width: number, height: number) => void;
  onRetry: () => void;
  onDisplayError: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const { current, scale, offsetX, offsetY } = zoom;
  const zoomStyle = useAnimatedStyle(() => {
    const shown = current.value === position;
    return {
      transform: [
        { translateX: shown ? offsetX.value : 0 },
        { translateY: shown ? offsetY.value : 0 },
        { scale: shown ? scale.value : 1 },
      ],
    };
  });
  return (
    <View style={[styles.page, { left: position * width, width, height }]}>
      {state.status === "ready" ? (
        <Animated.View style={[styles.fill, zoomStyle]}>
          <Image
            source={{ uri: state.uri }}
            contentFit="contain"
            style={styles.fill}
            accessibilityLabel={item.label}
            onLoad={({ source }) => onSize(source.width, source.height)}
            onError={item.path ? onDisplayError : undefined}
          />
        </Animated.View>
      ) : state.status === "failed" ? (
        <View style={styles.status}>
          <Text style={[styles.statusText, { color: theme.mutedText }]}>
            Image unavailable
          </Text>
          {item.path ? (
            <Button title="Try again" compact onPress={onRetry} />
          ) : null}
        </View>
      ) : (
        <View style={styles.status}>
          <ActivityIndicator color={theme.subtleText} />
        </View>
      )}
    </View>
  );
}

/** Small previews of every picture; the shown one stays centered. */
function ThumbnailStrip({
  items,
  index,
  width,
  visible,
  bottomInset,
  stateOf,
  onSelect,
  onRangeChange,
}: {
  items: readonly MarkdownMedia[];
  index: number;
  width: number;
  visible: boolean;
  bottomInset: number;
  stateOf: (item: MarkdownMedia) => WorkspaceMediaState | undefined;
  onSelect: (index: number) => void;
  onRangeChange: (range: [number, number]) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const scrollRef = useRef<ScrollView>(null);
  const range = useRef<[number, number]>([-1, -1]);
  const inset = Math.max(0, (width - THUMB) / 2);
  const shown = useSharedValue(visible ? 1 : 0);
  useEffect(() => {
    shown.value = withTiming(visible ? 1 : 0, FADE);
  }, [shown, visible]);
  const fadeStyle = useAnimatedStyle(() => ({
    opacity: shown.value,
    transform: [{ translateY: (1 - shown.value) * 12 }],
  }));
  const placed = useRef(false);
  useEffect(() => {
    if (placed.current)
      scrollRef.current?.scrollTo({ x: index * THUMB_STEP, animated: true });
  }, [index]);
  const reportRange = useCallback(
    (offset: number) => {
      const first = Math.max(0, Math.floor((offset - inset) / THUMB_STEP));
      const lastVisible = Math.min(
        items.length - 1,
        Math.floor((offset + width - inset) / THUMB_STEP),
      );
      if (range.current[0] === first && range.current[1] === lastVisible)
        return;
      range.current = [first, lastVisible];
      onRangeChange([first, lastVisible]);
    },
    [inset, items.length, onRangeChange, width],
  );
  const onScroll = useCallback(
    (event: NativeSyntheticEvent<NativeScrollEvent>) =>
      reportRange(event.nativeEvent.contentOffset.x),
    [reportRange],
  );
  return (
    <Animated.View
      pointerEvents={visible ? "auto" : "none"}
      accessibilityElementsHidden={!visible}
      importantForAccessibility={visible ? "auto" : "no-hide-descendants"}
      style={[
        styles.strip,
        { paddingBottom: bottomInset + 10, backgroundColor: theme.background },
        fadeStyle,
      ]}
    >
      <ScrollView
        ref={scrollRef}
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={{ paddingHorizontal: inset, gap: THUMB_GAP }}
        onContentSizeChange={() => {
          // Open centered on the shown picture, without scrolling there.
          if (placed.current) return;
          placed.current = true;
          scrollRef.current?.scrollTo({
            x: index * THUMB_STEP,
            animated: false,
          });
          reportRange(index * THUMB_STEP);
        }}
        onScroll={onScroll}
        scrollEventThrottle={100}
        testID="image-preview-thumbnails"
      >
        {items.map((item, position) => {
          const state = stateOf(item);
          const selected = position === index;
          return (
            <NativeAction
              key={`${mediaKey(item)}:${position}`}
              accessibilityRole="button"
              accessibilityLabel={`Show image ${position + 1} of ${items.length}${item.label ? `, ${item.label}` : ""}`}
              accessibilityState={{ selected }}
              onPress={() => onSelect(position)}
              style={styles.thumbButton}
            >
              <View
                style={[
                  styles.thumb,
                  {
                    backgroundColor: theme.surfaceMuted,
                    borderColor: selected ? theme.text : "transparent",
                    opacity: selected ? 1 : 0.7,
                  },
                ]}
              >
                {state?.status === "ready" ? (
                  <Image
                    source={{ uri: state.uri }}
                    contentFit="cover"
                    style={styles.fill}
                    recyclingKey={mediaKey(item)}
                  />
                ) : null}
              </View>
            </NativeAction>
          );
        })}
      </ScrollView>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  track: { position: "absolute", top: 0, left: 0 },
  page: { position: "absolute", top: 0, overflow: "hidden" },
  status: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    padding: 32,
  },
  statusText: { fontSize: 15, textAlign: "center" },
  strip: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    paddingTop: 10,
  },
  thumbButton: { width: THUMB, height: THUMB },
  thumb: {
    width: THUMB,
    height: THUMB,
    borderRadius: 6,
    borderWidth: 2,
    overflow: "hidden",
  },
});
