// Adapted from t3code's JavaScriptSourceFileSurface (https://github.com/pingdotgg/t3code),
// MIT License, Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-highlights/LICENSE.
import React, { memo, useMemo } from "react";
import {
  FlatList,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useTheme } from "../../ui/theme";
import { resolveHighlightLanguage } from "./highlightLanguage";
import { tokenizeCode, type HighlightedToken } from "./highlights";

const MONO = Platform.OS === "ios" ? "Menlo" : "monospace";
const FONT_SIZE = 12.5;
const ROW_HEIGHT = 19;
/** Width of unwrapped source; longer lines scroll horizontally. */
const CONTENT_WIDTH = 1400;

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
 * scrolling unless wrapped.
 */
export function SourceView({
  path,
  contents,
  wrap,
  bottomInset = 0,
}: {
  path: string;
  contents: string;
  wrap: boolean;
  bottomInset?: number;
}): React.JSX.Element {
  const { theme } = useTheme();
  const lines = useMemo(() => contents.split(/\r?\n/), [contents]);
  const source = useMemo(
    () => tokenizeCode(contents, resolveHighlightLanguage(path)),
    [contents, path],
  );
  const gutter = String(lines.length).length * 7.6 + 14;
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
        ? {}
        : {
            getItemLayout: (_data: unknown, index: number) => ({
              length: ROW_HEIGHT,
              offset: ROW_HEIGHT * index,
              index,
            }),
          })}
      contentContainerStyle={{
        paddingTop: 8,
        paddingBottom: bottomInset + ROW_HEIGHT * 2,
      }}
      testID="file-source"
    />
  );
  if (wrap) return list;
  return (
    <ScrollView
      horizontal
      bounces={false}
      contentContainerStyle={{ minWidth: "100%" }}
    >
      <View style={{ width: CONTENT_WIDTH }}>{list}</View>
    </ScrollView>
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
});
