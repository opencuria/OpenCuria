import React, { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Image } from "expo-image";
import * as Clipboard from "expo-clipboard";
import { SvgXml } from "react-native-svg";
import type { OpenCuriaClient } from "@opencuria/client";
import { ChatMarkdown } from "../../components/ChatMarkdown";
import type { MediaCache } from "../../platform";
import { Button } from "../../ui/controls";
import type { NativeMenuAction } from "../../ui/NativeMenu";
import { useTheme } from "../../ui/theme";
import { ToolDetailSheet } from "../ToolDetailSheet";
import {
  BinaryFileError,
  delimiterFor,
  isImagePath,
  isMarkdownPath,
  isSvgPath,
  parseDelimited,
  readWorkspaceText,
  type WorkspaceText,
} from "./fileContent";
import { SourceView } from "./SourceView";

type Mode = "preview" | "source";
type Content =
  | { phase: "loading" }
  | { phase: "error"; message: string; binary?: boolean }
  | { phase: "text"; file: WorkspaceText }
  | { phase: "image"; uri: string };

/** Source wrap preference for this app session. */
let wrapSource = false;

function previewable(path: string): boolean {
  return (
    isMarkdownPath(path) ||
    delimiterFor(path) !== null ||
    isSvgPath(path) ||
    isImagePath(path)
  );
}

/**
 * File viewer like t3code's file screen: highlighted source, Markdown, table
 * and image previews, Preview/Source, word wrap, copy and share actions.
 */
export function FileViewerSheet({
  client,
  workspaceId,
  media,
  path,
  bottomInset,
  onInsertIntoChat,
  onClose,
}: {
  client: OpenCuriaClient;
  workspaceId: string;
  media: MediaCache | null;
  path: string | null;
  bottomInset: number;
  onInsertIntoChat?: (text: string) => void;
  onClose: () => void;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const [content, setContent] = useState<Content>({ phase: "loading" });
  const [mode, setMode] = useState<Mode>("preview");
  const [wrap, setWrap] = useState(wrapSource);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!path) return;
    setMode(previewable(path) ? "preview" : "source");
    setContent({ phase: "loading" });
    let live = true;
    let release: (() => void) | undefined;
    if (isImagePath(path)) {
      if (!media) {
        setContent({
          phase: "error",
          message: "Images need a connected workspace.",
        });
        return;
      }
      media
        .resolvePinned(path)
        .then((pinned) => {
          if (!live) return pinned.release();
          release = () => pinned.release();
          setContent({ phase: "image", uri: pinned.uri });
        })
        .catch((error: unknown) => {
          if (live)
            setContent({
              phase: "error",
              message:
                error instanceof Error
                  ? error.message
                  : "Could not load the image",
            });
        });
    } else {
      readWorkspaceText(client, workspaceId, path)
        .then((file) => live && setContent({ phase: "text", file }))
        .catch((error: unknown) => {
          if (!live) return;
          setContent({
            phase: "error",
            binary: error instanceof BinaryFileError,
            message:
              error instanceof BinaryFileError
                ? "This file can't be shown as text."
                : error instanceof Error
                  ? error.message
                  : "Could not read the file",
          });
        });
    }
    return () => {
      live = false;
      release?.();
    };
  }, [attempt, client, media, path, workspaceId]);

  if (!path) return null;
  const name = path.split("/").pop() ?? path;
  const folder = path.slice(0, path.length - name.length - 1) || "/";
  const relative = folder.replace(/^\/workspace\/?/, "");
  const canToggle = previewable(path) && !isImagePath(path);
  const text = content.phase === "text" ? content.file.text : null;
  const delimiter = delimiterFor(path);

  const share = async () => {
    if (!media) return;
    try {
      const opened = await media.openFile(path);
      const Sharing = await import("expo-sharing");
      if (await Sharing.isAvailableAsync())
        await Sharing.shareAsync(opened.uri, {
          mimeType: opened.mimeType,
          dialogTitle: name,
        });
    } catch (error) {
      Alert.alert(
        "Can't share file",
        error instanceof Error ? error.message : "Please try again.",
      );
    }
  };

  const actions: NativeMenuAction[] = [
    ...(canToggle
      ? [
          {
            id: "preview",
            title: delimiter ? "Table" : "Preview",
            symbol: delimiter ? "tablecells" : "eye",
            selected: mode === "preview",
          },
          {
            id: "source",
            title: "Source",
            symbol: "doc.text",
            selected: mode === "source",
          },
        ]
      : []),
    ...(mode === "source"
      ? [
          {
            id: "wrap",
            title: "Word wrap",
            symbol: "text.alignleft",
            selected: wrap,
          },
        ]
      : []),
    ...(onInsertIntoChat
      ? [{ id: "mention", title: "Mention in chat", symbol: "at" }]
      : []),
    { id: "copy-path", title: "Copy path", symbol: "doc.on.doc" },
    ...(text !== null
      ? [
          {
            id: "copy-contents",
            title:
              content.phase === "text" && content.file.truncated
                ? "Copy preview"
                : "Copy contents",
            symbol: "doc.on.clipboard",
          },
        ]
      : []),
    ...(media
      ? [{ id: "share", title: "Share", symbol: "square.and.arrow.up" }]
      : []),
  ];
  const onAction = (id: string) => {
    if (id === "preview" || id === "source") setMode(id);
    else if (id === "wrap") {
      wrapSource = !wrap;
      setWrap(!wrap);
    } else if (id === "mention") onInsertIntoChat?.(`@file:${path} `);
    else if (id === "copy-path") void Clipboard.setStringAsync(path);
    else if (id === "copy-contents" && text !== null)
      void Clipboard.setStringAsync(text);
    else if (id === "share") void share();
  };

  let body: React.ReactNode;
  if (content.phase === "loading")
    body = (
      <View style={styles.center}>
        <ActivityIndicator color={theme.subtleText} />
      </View>
    );
  else if (content.phase === "error")
    body = (
      <View style={styles.center}>
        <Text style={[styles.title, { color: theme.text }]}>
          File unavailable
        </Text>
        <Text style={[styles.detail, { color: theme.mutedText }]}>
          {content.message}
        </Text>
        {content.binary && media ? (
          <Button
            title="Share"
            symbol="square.and.arrow.up"
            compact
            onPress={() => void share()}
            style={{ alignSelf: "center" }}
          />
        ) : (
          <Button
            title="Try again"
            compact
            onPress={() => setAttempt((value) => value + 1)}
            style={{ alignSelf: "center" }}
          />
        )}
      </View>
    );
  else if (content.phase === "image")
    body = (
      <ScrollView
        contentContainerStyle={styles.imageScroll}
        maximumZoomScale={4}
        minimumZoomScale={1}
        centerContent
      >
        <Image
          source={{ uri: content.uri }}
          style={styles.image}
          contentFit="contain"
          accessibilityLabel={name}
        />
      </ScrollView>
    );
  else {
    const file = content.file;
    const notice = file.truncated ? (
      <View
        style={[
          styles.notice,
          { backgroundColor: theme.surfaceMuted, borderColor: theme.border },
        ]}
      >
        <Text style={[styles.noticeTitle, { color: theme.text }]}>
          Partial file
        </Text>
        <Text style={[styles.noticeText, { color: theme.mutedText }]}>
          Preview limited to the first 1 MB.
        </Text>
      </View>
    ) : null;
    if (mode === "preview" && isMarkdownPath(path))
      body = (
        <ScrollView
          contentContainerStyle={[
            styles.markdown,
            { paddingBottom: bottomInset + 32 },
          ]}
        >
          {notice}
          <ChatMarkdown
            markdown={file.text}
            resolveMedia={
              media ? (target) => media.resolvePinned(target) : undefined
            }
          />
        </ScrollView>
      );
    else if (mode === "preview" && delimiter)
      body = (
        <>
          {notice}
          <DelimitedTable
            rows={parseDelimited(file.text, delimiter)}
            bottomInset={bottomInset}
          />
        </>
      );
    else if (mode === "preview" && isSvgPath(path))
      body = (
        <View style={styles.center}>
          <SvgPreview xml={file.text} />
        </View>
      );
    else
      body = (
        <View style={{ flex: 1 }}>
          {notice}
          <SourceView
            path={path}
            contents={file.text}
            wrap={wrap}
            bottomInset={bottomInset}
          />
        </View>
      );
  }

  return (
    <ToolDetailSheet
      visible
      onClose={onClose}
      title={name}
      subtitle={relative || "workspace"}
      actions={actions}
      onAction={onAction}
      testID="file-viewer"
    >
      {body}
    </ToolDetailSheet>
  );
}

function SvgPreview({ xml }: { xml: string }): React.JSX.Element {
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

const MAX_TABLE_COLUMNS = 40;

function DelimitedTable({
  rows,
  bottomInset,
}: {
  rows: string[][];
  bottomInset: number;
}): React.JSX.Element {
  const { theme } = useTheme();
  const columns = Math.min(
    MAX_TABLE_COLUMNS,
    rows.reduce((max, row) => Math.max(max, row.length), 0),
  );
  return (
    <ScrollView horizontal bounces={false}>
      <ScrollView contentContainerStyle={{ paddingBottom: bottomInset + 24 }}>
        {rows.map((row, rowIndex) => (
          <View
            key={rowIndex}
            style={[
              styles.tableRow,
              {
                borderColor: theme.border,
                backgroundColor:
                  rowIndex === 0 ? theme.surfaceMuted : undefined,
              },
            ]}
          >
            {Array.from({ length: columns }, (_, column) => (
              <Text
                key={column}
                numberOfLines={2}
                style={[
                  styles.cell,
                  { color: theme.text, borderColor: theme.border },
                  rowIndex === 0 && styles.headerCell,
                ]}
              >
                {row[column] ?? ""}
              </Text>
            ))}
          </View>
        ))}
      </ScrollView>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  center: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    padding: 32,
  },
  title: { fontSize: 17, fontWeight: "600" },
  detail: { fontSize: 14, textAlign: "center" },
  imageScroll: { flexGrow: 1, alignItems: "center", justifyContent: "center" },
  image: { width: "100%", height: 480 },
  markdown: { padding: 20 },
  notice: {
    margin: 12,
    padding: 12,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    gap: 2,
  },
  noticeTitle: { fontSize: 14, fontWeight: "600" },
  noticeText: { fontSize: 13 },
  tableRow: {
    flexDirection: "row",
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  cell: {
    width: 140,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
    borderRightWidth: StyleSheet.hairlineWidth,
  },
  headerCell: { fontWeight: "600" },
});
