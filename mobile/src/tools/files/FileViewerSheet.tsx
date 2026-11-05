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
import { useVideoPlayer, VideoView } from "expo-video";
import * as Clipboard from "expo-clipboard";
import * as Sharing from "expo-sharing";
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
  isPdfPath,
  isSvgPath,
  isVideoPath,
  parseDelimited,
  readWorkspaceText,
  type WorkspaceText,
} from "./fileContent";
import { hasNativePdfView, PdfPreview } from "./PdfPreview";
import { SourceView } from "./SourceView";

type Mode = "preview" | "source";
/** A workspace path or an inline tool attachment from the chat. */
export type FileViewerTarget =
  | string
  | { uri: string; filename: string; mime: string };

type Content =
  | { phase: "loading" }
  | { phase: "error"; message: string; shareable?: boolean }
  | { phase: "text"; file: WorkspaceText }
  | { phase: "image" | "video" | "pdf"; uri: string }
  | { phase: "attachment" };

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
 * and media previews, Preview/Source, word wrap, copy and share actions.
 */
export function FileViewerSheet({
  client,
  workspaceId,
  media,
  file: target,
  bottomInset,
  onInsertIntoChat,
  onClose,
}: {
  client: OpenCuriaClient;
  workspaceId: string;
  media: MediaCache | null;
  file: FileViewerTarget | null;
  bottomInset: number;
  onInsertIntoChat?: (text: string) => void;
  onClose: () => void;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const [content, setContent] = useState<Content>({ phase: "loading" });
  const [mode, setMode] = useState<Mode>("preview");
  const [wrap, setWrap] = useState(wrapSource);
  const [attempt, setAttempt] = useState(0);
  const path = typeof target === "string" ? target : null;
  const attachment = typeof target === "object" ? target : null;

  useEffect(() => {
    if (!path && !attachment) return;
    setMode(
      !path || previewable(path) || isVideoPath(path) || isPdfPath(path)
        ? "preview"
        : "source",
    );
    setContent({ phase: "loading" });
    let live = true;
    let release: (() => void) | undefined;
    if (attachment) {
      if (!media) {
        setContent({
          phase: "error",
          message: "Attachments need a connected workspace.",
        });
        return;
      }
      const mime = attachment.mime.toLowerCase();
      const phase = mime.startsWith("image/")
        ? "image"
        : mime.startsWith("video/")
          ? "video"
          : mime === "application/pdf" && hasNativePdfView()
            ? "pdf"
            : "attachment";
      if (phase === "attachment") setContent({ phase });
      else
        media
          .saveAttachment(attachment.uri)
          .then((uri) => live && setContent({ phase, uri }))
          .catch((error: unknown) => {
            if (live)
              setContent({
                phase: "error",
                message:
                  error instanceof Error
                    ? error.message
                    : "Could not load the attachment",
              });
          });
    } else if (path && (isImagePath(path) || isVideoPath(path))) {
      const phase = isVideoPath(path) ? "video" : "image";
      if (!media) {
        setContent({
          phase: "error",
          message: "Media need a connected workspace.",
        });
        return;
      }
      media
        .resolvePinned(path)
        .then((pinned) => {
          if (!live) return pinned.release();
          release = () => pinned.release();
          setContent({ phase, uri: pinned.uri });
        })
        .catch((error: unknown) => {
          if (live)
            setContent({
              phase: "error",
              message:
                error instanceof Error
                  ? error.message
                  : "Could not load the media",
            });
        });
    } else if (path && isPdfPath(path)) {
      if (!media || !hasNativePdfView()) {
        setContent({
          phase: "error",
          message: media
            ? "This app version can't preview PDFs."
            : "Documents need a connected workspace.",
          shareable: true,
        });
        return;
      }
      media
        .openFile(path)
        .then((opened) => live && setContent({ phase: "pdf", uri: opened.uri }))
        .catch((error: unknown) => {
          if (live)
            setContent({
              phase: "error",
              message:
                error instanceof Error
                  ? error.message
                  : "Could not load the PDF",
            });
        });
    } else if (path) {
      readWorkspaceText(client, workspaceId, path)
        .then((file) => live && setContent({ phase: "text", file }))
        .catch((error: unknown) => {
          if (!live) return;
          setContent({
            phase: "error",
            shareable: error instanceof BinaryFileError,
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
  }, [attempt, attachment, client, media, path, workspaceId]);

  if (!target) return null;
  const name = path?.split("/").pop() ?? attachment!.filename;
  const folder = path
    ? path.slice(0, path.length - name.length - 1) || "/"
    : "";
  const relative = folder.replace(/^\/workspace\/?/, "");
  const canToggle = path && previewable(path) && !isImagePath(path);
  const text = content.phase === "text" ? content.file.text : null;
  const delimiter = path ? delimiterFor(path) : null;

  const share = async () => {
    if (!media) return;
    try {
      if (attachment) {
        await media.shareAttachment(attachment.uri);
        return;
      }
      if (!path) return;
      // A PDF on screen is already downloaded.
      const opened =
        content.phase === "pdf"
          ? { uri: content.uri, mimeType: "application/pdf" }
          : await media.openFile(path);
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
    ...(mode === "source" && text !== null
      ? [
          {
            id: "wrap",
            title: "Word wrap",
            symbol: "text.alignleft",
            selected: wrap,
          },
        ]
      : []),
    ...(onInsertIntoChat && path
      ? [{ id: "mention", title: "Mention in chat", symbol: "at" }]
      : []),
    ...(path
      ? [{ id: "copy-path", title: "Copy path", symbol: "doc.on.doc" }]
      : []),
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
    } else if (id === "mention" && path) onInsertIntoChat?.(`@file:${path} `);
    else if (id === "copy-path" && path) void Clipboard.setStringAsync(path);
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
        {content.shareable && media ? (
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
  else if (content.phase === "attachment")
    body = (
      <View style={styles.center}>
        <Text style={[styles.detail, { color: theme.mutedText }]}>
          Open or share this document with another app.
        </Text>
        <Button title="Share or open" onPress={() => void share()} />
      </View>
    );
  else if (content.phase === "video") body = <VideoPreview uri={content.uri} />;
  else if (content.phase === "pdf")
    body = (
      <PdfPreview
        uri={content.uri}
        name={name}
        bottomInset={bottomInset}
        onError={(message) =>
          setContent({ phase: "error", message, shareable: true })
        }
      />
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
  else if (content.phase === "text" && path) {
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
  } else body = null;

  return (
    <ToolDetailSheet
      visible
      onClose={onClose}
      title={name}
      subtitle={path ? relative || "workspace" : undefined}
      actions={actions}
      onAction={onAction}
      testID="file-viewer"
    >
      {body}
    </ToolDetailSheet>
  );
}

function VideoPreview({ uri }: { uri: string }): React.JSX.Element {
  const player = useVideoPlayer(uri, (instance) => instance.play());
  return (
    <VideoView
      player={player}
      nativeControls
      contentFit="contain"
      style={{ flex: 1 }}
      testID="file-video"
    />
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
