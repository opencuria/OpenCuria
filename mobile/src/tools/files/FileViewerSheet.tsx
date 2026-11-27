import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ActivityIndicator,
  Alert,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type LayoutChangeEvent,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";
import { useVideoPlayer, VideoView } from "expo-video";
import * as Clipboard from "expo-clipboard";
import * as Sharing from "expo-sharing";
import type { OpenCuriaClient } from "@opencuria/client";
import { previewLines, type Size } from "@opencuria/chat-core/visualRecording";
import type {
  VisualSurface,
  VisualSurfaceSpec,
} from "@opencuria/chat-core/visualRecorder";
import { ChatMarkdown } from "../../components/ChatMarkdown";
import type { ChatMediaResolver } from "../../components/useWorkspaceMedia";
import type { MediaCache } from "../../platform";
import { Button } from "../../ui/controls";
import type { NativeMenuAction } from "../../ui/NativeMenu";
import { useTheme } from "../../ui/theme";
import { useRecordingSurface } from "../../voice/useRecordingSurface";
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
import { ImagePreview, SvgPreview } from "./ImagePreview";
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
 * Lines a rendered preview shows, estimated while it scrolls, for a
 * recording's `surface`.
 */
function usePreviewView(surface: VisualSurface | null, lineCount: number) {
  const metrics = useRef({ offset: 0, viewport: 0, content: 0 });
  const report = useCallback(() => {
    surface?.view(previewLines(metrics.current, lineCount));
  }, [lineCount, surface]);
  useEffect(() => {
    if (surface) report();
  }, [report, surface]);
  return {
    onScroll: (event: NativeSyntheticEvent<NativeScrollEvent>) => {
      metrics.current.offset = event.nativeEvent.contentOffset.y;
      report();
    },
    onLayout: (event: LayoutChangeEvent) => {
      metrics.current.viewport = event.nativeEvent.layout.height;
      report();
    },
    onContentSizeChange: (_width: number, height: number) => {
      metrics.current.content = height;
      report();
    },
    scrollEventThrottle: 100,
  };
}

/**
 * File viewer like t3code's file screen: highlighted source, Markdown, table
 * and media previews, Preview/Source, word wrap, copy and share actions.
 * While a recording runs, what it shows joins it: lines of source (with the
 * line under a finger), estimated lines of a rendered preview, an image's
 * pixels.
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
  // Pictures in a Markdown file load like those in the chat.
  const resolveMedia = useMemo<ChatMediaResolver | undefined>(
    () =>
      media
        ? Object.assign((target: string) => media.resolvePinned(target), {
            describe: (target: string) => media.describe(target),
          })
        : undefined,
    [media],
  );

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

  // What a running recording sees of this file.
  const text = content.phase === "text" ? content.file.text : null;
  const lines = useMemo(
    () => (text === null ? null : text.split(/\r?\n/)),
    [text],
  );
  const shows =
    !path || !lines
      ? null
      : mode === "preview" && isMarkdownPath(path)
        ? "markdown"
        : mode === "preview" && delimiterFor(path)
          ? "table"
          : mode === "preview" && isSvgPath(path)
            ? null
            : "source";
  const [imageSize, setImageSize] = useState<Size | null>(null);
  useEffect(() => setImageSize(null), [path]);
  const spec = useMemo<VisualSurfaceSpec | null>(() => {
    if (!path) return null;
    if (lines && shows)
      return shows === "source"
        ? { kind: "file", path, lines }
        : { kind: "file", path, lines, approximate: true };
    if (content.phase === "image" && imageSize)
      return { kind: "image", path, ...imageSize };
    return null;
  }, [content.phase, imageSize, lines, path, shows]);
  const surface = useRecordingSurface(spec);
  const preview = usePreviewView(
    shows === "markdown" || shows === "table" ? surface : null,
    lines?.length ?? 0,
  );

  if (!target) return null;
  const name = path?.split("/").pop() ?? attachment!.filename;
  const folder = path
    ? path.slice(0, path.length - name.length - 1) || "/"
    : "";
  const relative = folder.replace(/^\/workspace\/?/, "");
  const canToggle = path && previewable(path) && !isImagePath(path);
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
      <ImagePreview
        uri={content.uri}
        name={name}
        surface={surface}
        onSize={setImageSize}
      />
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
          {...preview}
          contentContainerStyle={[
            styles.markdown,
            { paddingBottom: bottomInset + 32 },
          ]}
        >
          {notice}
          <ChatMarkdown markdown={file.text} resolveMedia={resolveMedia} />
        </ScrollView>
      );
    else if (mode === "preview" && delimiter)
      body = (
        <>
          {notice}
          <DelimitedTable
            rows={parseDelimited(file.text, delimiter)}
            bottomInset={bottomInset}
            scroll={preview}
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
            lines={lines ?? undefined}
            wrap={wrap}
            bottomInset={bottomInset}
            surface={shows === "source" ? surface : null}
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

const MAX_TABLE_COLUMNS = 40;

function DelimitedTable({
  rows,
  bottomInset,
  scroll,
}: {
  rows: string[][];
  bottomInset: number;
  /** Vertical scroll handlers, e.g. a recording's view of the rows. */
  scroll?: Partial<React.ComponentProps<typeof ScrollView>>;
}): React.JSX.Element {
  const { theme } = useTheme();
  const columns = Math.min(
    MAX_TABLE_COLUMNS,
    rows.reduce((max, row) => Math.max(max, row.length), 0),
  );
  return (
    <ScrollView horizontal bounces={false}>
      <ScrollView
        {...scroll}
        contentContainerStyle={{ paddingBottom: bottomInset + 24 }}
      >
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
