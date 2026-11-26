import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ScrollView,
  StyleSheet,
  Text,
  View,
  type LayoutRectangle,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";
import { Image } from "expo-image";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import { SvgXml } from "react-native-svg";
import {
  containedRect,
  imagePixel,
  visibleImageRegion,
  type Rect,
  type Size,
} from "@opencuria/chat-core/visualRecording";
import type { VisualSurface } from "@opencuria/chat-core/visualRecorder";
import {
  PointerOverlay,
  type PointerOverlayEvent,
} from "../../components/PointerOverlay";
import { useImageSize } from "../../components/useImageSize";
import { useTheme } from "../../ui/theme";
import { usePointMode } from "../../voice/voiceRecording";

/**
 * A zoomable raster image (file or data URI). With a recording's `surface`
 * it reports the visible part in image pixels and the pixel under a finger:
 * a tap in Use mode, the laser pointer in Point mode.
 */
export function ImagePreview({
  uri,
  name,
  surface = null,
  onSize,
}: {
  uri: string;
  name: string;
  surface?: VisualSurface | null;
  /** The image's own pixel size, once loaded. */
  onSize?: (size: Size) => void;
}): React.JSX.Element {
  const pointMode = usePointMode();
  // The file's own pixels, not the size the image view decoded it at.
  const natural = useImageSize(uri);
  const sizeRef = useRef(onSize);
  sizeRef.current = onSize;
  useEffect(() => {
    if (natural) sizeRef.current?.(natural);
    // `natural` is a new object per render; its numbers identify it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [natural?.width, natural?.height]);
  const viewport = useRef<Size>({ width: 0, height: 0 });
  /** The image box in the scroll content at zoom 1. */
  const box = useRef<LayoutRectangle | null>(null);
  const zoom = useRef({ scale: 1, x: 0, y: 0 });
  /** Where the picture is drawn in the viewport now. */
  const shownRect = useCallback((): Rect | null => {
    const frame = box.current;
    if (!frame || !natural) return null;
    const { scale, x, y } = zoom.current;
    return containedRect(
      {
        x: frame.x * scale - x,
        y: frame.y * scale - y,
        width: frame.width * scale,
        height: frame.height * scale,
      },
      natural,
    );
  }, [natural]);
  const reportView = useCallback(() => {
    const rect = shownRect();
    if (surface && rect && natural)
      surface.view(visibleImageRegion(rect, viewport.current, natural));
  }, [natural, shownRect, surface]);
  useEffect(() => {
    if (surface) reportView();
  }, [reportView, surface]);
  const pointAt = useCallback(
    (
      event: "down" | "move" | "up",
      x: number,
      y: number,
      mode: "use" | "point",
    ) => {
      const rect = shownRect();
      if (surface && rect && natural)
        surface.pointer({
          event,
          point: imagePixel({ x, y }, rect, natural),
          mode,
        });
    },
    [natural, shownRect, surface],
  );
  const pointed = useCallback<PointerOverlayEvent>(
    (event, x, y) => pointAt(event, x, y, "point"),
    [pointAt],
  );
  // One stable gesture; it only records while a recording runs in Use mode.
  const tapping = useRef<((x: number, y: number) => void) | null>(null);
  tapping.current =
    surface && !pointMode
      ? (x, y) => {
          pointAt("down", x, y, "use");
          pointAt("up", x, y, "use");
        }
      : null;
  const tap = useMemo(
    () =>
      Gesture.Tap()
        .withTestId("file-image-tap")
        .maxDistance(10)
        .runOnJS(true)
        .onEnd((event, success) => {
          if (success) tapping.current?.(event.x, event.y);
        }),
    [],
  );
  const onScroll = (event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, zoomScale } = event.nativeEvent;
    zoom.current = {
      scale: zoomScale || 1,
      x: contentOffset.x,
      y: contentOffset.y,
    };
    reportView();
  };
  return (
    <View style={styles.fill}>
      <GestureDetector gesture={tap}>
        <ScrollView
          contentContainerStyle={styles.scroll}
          maximumZoomScale={4}
          minimumZoomScale={1}
          centerContent
          onScroll={onScroll}
          scrollEventThrottle={100}
          onLayout={(event) => {
            viewport.current = {
              width: event.nativeEvent.layout.width,
              height: event.nativeEvent.layout.height,
            };
          }}
        >
          <Image
            source={{ uri }}
            style={styles.image}
            contentFit="contain"
            accessibilityLabel={name}
            onLayout={(event) => {
              box.current = event.nativeEvent.layout;
              reportView();
            }}
          />
        </ScrollView>
      </GestureDetector>
      {surface && pointMode ? (
        <PointerOverlay onPointer={pointed} testID="file-image-pointer" />
      ) : null}
    </View>
  );
}

/** An SVG document; invalid markup points to the Source view. */
export function SvgPreview({ xml }: { xml: string }): React.JSX.Element {
  const { theme } = useTheme();
  const [failed, setFailed] = useState(false);
  if (failed)
    return (
      <Text style={{ color: theme.mutedText }}>
        This SVG can't be previewed. Use Source.
      </Text>
    );
  return (
    <SvgXml
      xml={xml}
      width="90%"
      height="70%"
      onError={() => setFailed(true)}
    />
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  scroll: { flexGrow: 1, alignItems: "center", justifyContent: "center" },
  image: { width: "100%", height: 480 },
});
