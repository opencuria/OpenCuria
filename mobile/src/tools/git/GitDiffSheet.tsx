import React, { useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import * as Clipboard from "expo-clipboard";
import type { PinnedWorkspaceMedia } from "../../platform";
import { Button } from "../../ui/controls";
import type { NativeMenuAction } from "../../ui/NativeMenu";
import { useTheme } from "../../ui/theme";
import { isImagePath, isSvgPath } from "../files/fileContent";
import { resolveHighlightLanguage } from "../files/highlightLanguage";
import { ImagePreview, SvgPreview } from "../files/ImagePreview";
import { tokenizeCode, type HighlightedToken } from "../files/highlights";
import { ToolDetailSheet } from "../ToolDetailSheet";
import { diffColors, dirName, fileName } from "./gitStyle";
import {
  buildLineDiff,
  exceedsDiffLimit,
  type DiffRow,
  type LineDiff,
} from "./lineDiff";

const MONO = Platform.OS === "ios" ? "Menlo" : "monospace";
const ROW_HEIGHT = 20;
/** Word wrap preference for this app session. */
let wrapLines = false;

export interface GitDiffTarget {
  path: string;
  /** Second header line, e.g. "Staged" or a short commit hash. */
  label: string;
  load: () => Promise<{ original: string; modified: string }>;
  /** The new side of a raster image, cached on the device; null when deleted. */
  loadImage: () => Promise<PinnedWorkspaceMedia | null>;
  /** Extra actions (stage, discard …) above the shared ones. */
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
}

type State =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "large"; modified: string }
  | { phase: "image"; uri: string | null }
  | { phase: "ready"; diff: LineDiff; original: string; modified: string };

/**
 * Unified diff of one file side in a native sheet (no WebView, no Monaco).
 * Images show their new version; SVGs also offer the textual diff as Source.
 */
export function GitDiffSheet({
  target,
  onClose,
}: {
  target: GitDiffTarget | null;
  onClose: () => void;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const [state, setState] = useState<State>({ phase: "loading" });
  const [wrap, setWrap] = useState(wrapLines);
  const [preview, setPreview] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!target) return;
    let live = true;
    setState({ phase: "loading" });
    setPreview(true);
    const fail = (error: unknown) => {
      if (live)
        setState({
          phase: "error",
          message:
            error instanceof Error ? error.message : "Failed to load diff",
        });
    };
    if (isImagePath(target.path)) {
      let release: (() => void) | undefined;
      target
        .loadImage()
        .then((pinned) => {
          if (!live) return pinned?.release();
          release = pinned ? () => pinned.release() : undefined;
          setState({ phase: "image", uri: pinned?.uri ?? null });
        })
        .catch(fail);
      return () => {
        live = false;
        release?.();
      };
    }
    target
      .load()
      .then(({ original, modified }) => {
        if (!live) return;
        if (exceedsDiffLimit(original, modified))
          setState({ phase: "large", modified });
        else
          setState({
            phase: "ready",
            diff: buildLineDiff(original, modified),
            original,
            modified,
          });
      })
      .catch(fail);
    return () => {
      live = false;
    };
  }, [target, attempt]);

  if (!target) return null;
  const counts =
    state.phase === "ready" && !state.diff.identical
      ? ` · +${state.diff.additions} −${state.diff.deletions}`
      : "";
  const folder = dirName(target.path);
  const svg = isSvgPath(target.path);
  // An SVG preview needs only the new markup, so it works above the diff limit.
  const svgXml =
    svg && preview && (state.phase === "ready" || state.phase === "large")
      ? state.modified
      : null;
  const showsText = state.phase === "ready" && svgXml === null;
  const actions: NativeMenuAction[] = [
    ...(svg
      ? [
          {
            id: "preview",
            title: "Preview",
            symbol: "eye",
            selected: preview,
          },
          {
            id: "source",
            title: "Source",
            symbol: "doc.text",
            selected: !preview,
          },
        ]
      : []),
    ...(target.actions ?? []),
    ...(showsText
      ? [
          {
            id: "wrap",
            title: "Word wrap",
            symbol: "text.word.spacing",
            selected: wrap,
          },
        ]
      : []),
    { id: "copy-path", title: "Copy path", symbol: "doc.on.doc" },
  ];
  const onAction = (id: string) => {
    if (id === "preview" || id === "source") setPreview(id === "preview");
    else if (id === "wrap") {
      wrapLines = !wrap;
      setWrap(!wrap);
    } else if (id === "copy-path") void Clipboard.setStringAsync(target.path);
    else target.onAction?.(id);
  };

  return (
    <ToolDetailSheet
      visible
      onClose={onClose}
      title={fileName(target.path)}
      subtitle={`${target.label}${folder ? ` · ${folder}` : ""}${counts}`}
      actions={actions}
      onAction={onAction}
      testID="git-diff-sheet"
    >
      {state.phase === "loading" ? (
        <View style={styles.center}>
          <ActivityIndicator color={theme.subtleText} />
        </View>
      ) : state.phase === "error" ? (
        <View style={styles.center}>
          <Text style={[styles.message, { color: theme.mutedText }]}>
            {state.message}
          </Text>
          <Button
            title="Try again"
            compact
            onPress={() => setAttempt((value) => value + 1)}
            style={{ alignSelf: "center" }}
          />
        </View>
      ) : svgXml !== null ? (
        svgXml ? (
          <View style={styles.center}>
            <SvgPreview xml={svgXml} />
          </View>
        ) : (
          <Deleted />
        )
      ) : state.phase === "large" ? (
        <View style={styles.center}>
          <Text style={[styles.message, { color: theme.mutedText }]}>
            This file is too large to compare on a phone.
          </Text>
        </View>
      ) : state.phase === "image" ? (
        state.uri ? (
          <ImagePreview uri={state.uri} name={fileName(target.path)} />
        ) : (
          <Deleted />
        )
      ) : state.diff.identical ? (
        <View style={styles.center}>
          <Text style={[styles.message, { color: theme.mutedText }]}>
            {state.diff.empty ? "Empty file" : "No textual changes"}
          </Text>
        </View>
      ) : (
        <DiffView
          path={target.path}
          rows={state.diff.rows}
          original={state.original}
          modified={state.modified}
          wrap={wrap}
        />
      )}
    </ToolDetailSheet>
  );
}

function Deleted(): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={styles.center}>
      <Text style={[styles.message, { color: theme.mutedText }]}>
        This image was deleted.
      </Text>
    </View>
  );
}

function DiffView({
  path,
  rows,
  original,
  modified,
  wrap,
}: {
  path: string;
  rows: DiffRow[];
  original: string;
  modified: string;
  wrap: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  // Both sides are highlighted once; deleted lines take the old side's tokens.
  const sides = useMemo(() => {
    const language = resolveHighlightLanguage(path);
    return {
      old: tokenizeCode(original, language),
      new: tokenizeCode(modified, language),
    };
  }, [modified, original, path]);
  const tokensFor = (
    row: DiffRow,
  ): ReadonlyArray<HighlightedToken> | undefined =>
    row.kind === "del"
      ? sides.old.highlightLine((row.oldNo ?? 0) - 1, theme.scheme)
      : sides.new.highlightLine((row.newNo ?? 0) - 1, theme.scheme);
  const colors = diffColors(theme);
  const gutter = useMemo(() => {
    let max = 1;
    for (const row of rows) max = Math.max(max, row.oldNo ?? 0, row.newNo ?? 0);
    return String(max).length * 7.5 + 10;
  }, [rows]);
  const renderItem = ({ item }: { item: DiffRow }) => {
    if (item.kind === "gap")
      return (
        <View style={[styles.gap, { backgroundColor: theme.surfaceMuted }]}>
          <Text style={[styles.gapText, { color: theme.subtleText }]}>
            {item.hidden === 1
              ? "1 unchanged line"
              : `${item.hidden} unchanged lines`}
          </Text>
        </View>
      );
    const background =
      item.kind === "add"
        ? colors.addBackground
        : item.kind === "del"
          ? colors.delBackground
          : undefined;
    const marker = item.kind === "add" ? "+" : item.kind === "del" ? "−" : " ";
    return (
      <View style={[styles.row, { backgroundColor: background }]}>
        <Text
          style={[styles.number, { width: gutter, color: theme.subtleText }]}
        >
          {item.oldNo ?? ""}
        </Text>
        <Text
          style={[styles.number, { width: gutter, color: theme.subtleText }]}
        >
          {item.newNo ?? ""}
        </Text>
        <Text
          style={[
            styles.marker,
            {
              color:
                item.kind === "add"
                  ? colors.addMarker
                  : item.kind === "del"
                    ? colors.delMarker
                    : theme.subtleText,
            },
          ]}
        >
          {marker}
        </Text>
        <Text
          selectable
          numberOfLines={wrap ? undefined : 1}
          style={[
            styles.code,
            { color: theme.text },
            wrap ? styles.wrap : null,
          ]}
        >
          {highlighted(tokensFor(item), item.text, theme.text)}
        </Text>
      </View>
    );
  };
  const list = (
    <FlatList
      data={rows}
      keyExtractor={(row) => row.key}
      renderItem={renderItem}
      initialNumToRender={60}
      maxToRenderPerBatch={60}
      windowSize={12}
      contentContainerStyle={styles.listContent}
      testID="git-diff-rows"
    />
  );
  if (wrap) return list;
  return (
    <ScrollView
      horizontal
      bounces={false}
      contentContainerStyle={{ minWidth: "100%" }}
    >
      <View style={{ minWidth: 1200 }}>{list}</View>
    </ScrollView>
  );
}

function highlighted(
  tokens: ReadonlyArray<HighlightedToken> | undefined,
  text: string,
  color: string,
): React.ReactNode {
  if (!tokens?.length) return text.replace(/\t/g, "  ") || " ";
  return tokens.map((token, index) => (
    <Text
      key={index}
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
      {token.content.replace(/\t/g, "  ")}
    </Text>
  ));
}

const styles = StyleSheet.create({
  center: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    padding: 32,
  },
  message: { fontSize: 15, textAlign: "center" },
  listContent: { paddingVertical: 8, paddingBottom: 48 },
  row: {
    flexDirection: "row",
    minHeight: ROW_HEIGHT,
    alignItems: "flex-start",
  },
  number: {
    fontFamily: MONO,
    fontSize: 11,
    lineHeight: ROW_HEIGHT,
    textAlign: "right",
    paddingRight: 4,
  },
  marker: {
    fontFamily: MONO,
    fontSize: 12,
    lineHeight: ROW_HEIGHT,
    width: 16,
    textAlign: "center",
  },
  code: {
    fontFamily: MONO,
    fontSize: 12,
    lineHeight: ROW_HEIGHT,
    paddingRight: 16,
  },
  wrap: { flex: 1 },
  gap: { paddingVertical: 6, paddingHorizontal: 16 },
  gapText: { fontSize: 12, fontStyle: "italic" },
});
