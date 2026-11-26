// Adapted from t3code's JavaScriptSourceFileSurface (https://github.com/pingdotgg/t3code),
// MIT License, Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-highlights/LICENSE.
import React, { memo, useCallback, useEffect, useMemo, useRef } from "react";
import {
  FlatList,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type LayoutChangeEvent,
  type LayoutRectangle,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
  type ViewToken,
} from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import type { VisualSurface } from "@opencuria/chat-core/visualRecorder";
import {
  PointerOverlay,
  type PointerOverlayEvent,
} from "../../components/PointerOverlay";
import { useTheme } from "../../ui/theme";
import { usePointMode } from "../../voice/voiceRecording";
import { resolveHighlightLanguage } from "./highlightLanguage";
import { tokenizeCode, type HighlightedToken } from "./highlights";

const MONO = Platform.OS === "ios" ? "Menlo" : "monospace";
const FONT_SIZE = 12.5;
const ROW_HEIGHT = 19;
/** Width of unwrapped source; longer lines scroll horizontally. */
const CONTENT_WIDTH = 1400;
const PADDING_TOP = 8;
/** Monospaced glyphs are about this share of the font size wide. */
const CHAR_WIDTH = FONT_SIZE * 0.6;
/** A line counts as seen when this much of it shows. */
const VIEWABILITY = { itemVisiblePercentThreshold: 60 };

function visibleWhitespace(text: string): string {
  return text.replace(/\t/g, "    ");
}

const SourceLine = memo(function SourceLine({
  index,
  line,
  tokens,
  gutter,
  wrap,
  color,
  mutedColor,
}: {
  index: number;
  line: string;
  tokens: ReadonlyArray<HighlightedToken> | undefined;
  gutter: number;
  wrap: boolean;
  color: string;
  mutedColor: string;
}) {
  return (
    <View style={[styles.line, { minHeight: ROW_HEIGHT }]}>
      <Text style={[styles.number, { width: gutter, color: mutedColor }]}>
        {index + 1}
      </Text>
      <Text
        selectable
        numberOfLines={wrap ? undefined : 1}
        style={[styles.code, { color }, wrap && styles.wrap]}
      >
        {tokens && tokens.length
          ? tokens.map((token, tokenIndex) => (
              <Text
                key={tokenIndex}
                style={{
                  color: token.color ?? color,
                  fontWeight:
                    token.fontStyle !== null && (token.fontStyle & 2) === 2
                      ? "700"
                      : "400",
                  fontStyle:
                    token.fontStyle !== null && (token.fontStyle & 1) === 1
                      ? "italic"
                      : "normal",
                }}
              >
                {visibleWhitespace(token.content) || " "}
              </Text>
            ))
          : visibleWhitespace(line) || " "}
      </Text>
    </View>
  );
});

/**
 * Highlighted source with a line-number gutter: native @pierre/highlights
 * lexers (plain text without the module), virtualized lines, horizontal
 * scrolling unless wrapped. With a recording's `surface` it reports the
 * lines on screen and the line under a finger: a tap in Use mode, the laser
 * pointer in Point mode.
 */
export function SourceView({
  path,
  contents,
  lines: givenLines,
  wrap,
  bottomInset = 0,
  surface = null,
}: {
  path: string;
  contents: string;
  /** `contents` split into lines, when the caller has them. */
  lines?: readonly string[];
  wrap: boolean;
  bottomInset?: number;
  surface?: VisualSurface | null;
}): React.JSX.Element {
  const { theme } = useTheme();
  const lines = useMemo(
    () => givenLines ?? contents.split(/\r?\n/),
    [contents, givenLines],
  );
  const source = useMemo(
    () => tokenizeCode(contents, resolveHighlightLanguage(path)),
    [contents, path],
  );
  const gutter = String(lines.length).length * 7.6 + 14;
  const pointMode = usePointMode();

  // Where the user looks: scroll offsets, rendered rows and visible lines.
  const surfaceRef = useRef(surface);
  surfaceRef.current = surface;
  const scrollY = useRef(0);
  const scrollX = useRef(0);
  const visible = useRef<[number, number] | null>(null);
  const cells = useRef(new Map<number, LayoutRectangle>());
  const reportView = useCallback(() => {
    const range = visible.current;
    if (!range) return;
    surfaceRef.current?.view([
      0,
      range[0],
      Number.MAX_SAFE_INTEGER,
      range[1] - range[0] + 1,
    ]);
  }, []);
  useEffect(() => {
    if (surface) reportView();
  }, [reportView, surface]);
  // FlatList keeps its first viewability callback; read the surface by ref.
  const onViewableItemsChanged = useRef(
    ({ viewableItems }: { viewableItems: ViewToken[] }) => {
      const indexes = viewableItems
        .map((item) => item.index)
        .filter((index): index is number => typeof index === "number");
      if (!indexes.length) return;
      visible.current = [Math.min(...indexes), Math.max(...indexes)];
      reportView();
    },
  ).current;
  /** The zero-based line and column under a point of this view. */
  const lineAt = useCallback(
    (x: number, y: number): { x: number; y: number } | null => {
      const contentY = y + scrollY.current;
      let line: number | null = null;
      if (!wrap) line = Math.floor((contentY - PADDING_TOP) / ROW_HEIGHT);
      else
        for (const [index, cell] of cells.current)
          if (contentY >= cell.y && contentY < cell.y + cell.height) {
            line = index;
            break;
          }
      if (line === null || line < 0 || line >= lines.length) return null;
      // Wrapped lines have no fixed columns.
      const column = wrap
        ? 0
        : Math.max(0, Math.floor((x + scrollX.current - gutter) / CHAR_WIDTH));
      return { x: column, y: line };
    },
    [gutter, lines.length, wrap],
  );
  const pointed = useCallback<PointerOverlayEvent>(
    (event, x, y) =>
      surfaceRef.current?.pointer({
        event,
        point: lineAt(x, y),
        mode: "point",
      }),
    [lineAt],
  );
  // One stable gesture; it only records while a recording runs in Use mode.
  const tapping = useRef<((x: number, y: number) => void) | null>(null);
  tapping.current =
    surface && !pointMode
      ? (x, y) => {
          const point = lineAt(x, y);
          surface.pointer({ event: "down", point, mode: "use" });
          surface.pointer({ event: "up", point, mode: "use" });
        }
      : null;
  const tap = useMemo(
    () =>
      Gesture.Tap()
        .withTestId("file-source-tap")
        .maxDistance(10)
        .runOnJS(true)
        .onEnd((event, success) => {
          if (success) tapping.current?.(event.x, event.y);
        }),
    [],
  );
  const onScroll = useCallback(
    (event: NativeSyntheticEvent<NativeScrollEvent>) => {
      scrollY.current = event.nativeEvent.contentOffset.y;
    },
    [],
  );
  const onScrollX = useCallback(
    (event: NativeSyntheticEvent<NativeScrollEvent>) => {
      scrollX.current = event.nativeEvent.contentOffset.x;
    },
    [],
  );
  // Wrapped rows vary in height: their measured cells find the line.
  const Cell = useMemo(
    () =>
      function SourceCell({
        index,
        onLayout,
        ...props
      }: React.ComponentProps<typeof View> & { index: number }) {
        return (
          <View
            {...props}
            onLayout={(event: LayoutChangeEvent) => {
              cells.current.set(index, event.nativeEvent.layout);
              onLayout?.(event);
            }}
          />
        );
      },
    [],
  );

  const list = (
    <FlatList
      data={lines}
      keyExtractor={(_line, index) => String(index)}
      renderItem={({ item, index }) => (
        <SourceLine
          index={index}
          line={item}
          tokens={source.highlightLine(index, theme.scheme)}
          gutter={gutter}
          wrap={wrap}
          color={theme.text}
          mutedColor={theme.subtleText}
        />
      )}
      initialNumToRender={80}
      maxToRenderPerBatch={80}
      windowSize={12}
      {...(wrap
        ? { CellRendererComponent: Cell }
        : {
            getItemLayout: (_data: unknown, index: number) => ({
              length: ROW_HEIGHT,
              offset: ROW_HEIGHT * index,
              index,
            }),
          })}
      onViewableItemsChanged={onViewableItemsChanged}
      viewabilityConfig={VIEWABILITY}
      onScroll={onScroll}
      // Offsets only matter while a recording looks.
      scrollEventThrottle={surface ? 16 : undefined}
      contentContainerStyle={{
        paddingTop: PADDING_TOP,
        paddingBottom: bottomInset + ROW_HEIGHT * 2,
      }}
      testID="file-source"
    />
  );
  const body = wrap ? (
    list
  ) : (
    <ScrollView
      horizontal
      bounces={false}
      onScroll={onScrollX}
      scrollEventThrottle={surface ? 16 : undefined}
      contentContainerStyle={{ minWidth: "100%" }}
    >
      <View style={{ width: CONTENT_WIDTH }}>{list}</View>
    </ScrollView>
  );
  // One structure whether or not a recording runs, so the list keeps its
  // scroll position when one starts, ends or changes mode.
  return (
    <View style={styles.fill}>
      <GestureDetector gesture={tap}>
        <View style={styles.fill} collapsable={false}>
          {body}
        </View>
      </GestureDetector>
      {surface && pointMode ? (
        <PointerOverlay onPointer={pointed} testID="file-source-pointer" />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  line: { flexDirection: "row", alignItems: "flex-start" },
  number: {
    fontFamily: MONO,
    fontSize: 11,
    lineHeight: ROW_HEIGHT,
    textAlign: "right",
    paddingRight: 12,
  },
  code: {
    fontFamily: MONO,
    fontSize: FONT_SIZE,
    lineHeight: ROW_HEIGHT,
    paddingRight: 16,
  },
  wrap: { flex: 1 },
  fill: { flex: 1 },
});
