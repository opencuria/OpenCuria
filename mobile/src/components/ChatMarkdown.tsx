import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Linking,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Image } from "expo-image";
import { VideoView, useVideoPlayer } from "expo-video";
import { EnrichedMarkdownText } from "react-native-enriched-markdown";

import { resolveWorkspaceLinkPath } from "@opencuria/chat-core/workspaceFileRefs";
import remend from "remend";
import { NativeModal } from "../ui/primitives";
import { NativeAction } from "../ui/controls";
import { useTheme } from "../ui/theme";
import {
  isSafeExternalUrl,
  splitMarkdownMedia,
  type MarkdownMedia,
} from "./markdownMedia";
import {
  useWorkspaceMedia,
  type ResolvedChatMedia,
  type WorkspaceMediaState,
} from "./useWorkspaceMedia";

export type { ResolvedChatMedia } from "./useWorkspaceMedia";

export interface ChatMarkdownProps {
  markdown: string;
  streaming?: boolean;
  /** `quiet` is smaller, muted text for expanded tool and reasoning details. */
  variant?: "body" | "prompt" | "quiet";
  resolveMedia?: (workspacePath: string) => Promise<ResolvedChatMedia>;
  /** Changes with the host media-cache scope (workspace/account/server). */
  mediaScopeKey?: string | number;
  /** Parent list viewability gate; offscreen rows do not fetch workspace media. */
  mediaVisible?: boolean;
  onOpenFile?: (workspacePath: string) => void;
  testID?: string;
}

type MediaPreview = Pick<MarkdownMedia, "path" | "uri" | "label">;

/** Render GFM natively; workspace media is loaded only for visible chat rows. */
export function ChatMarkdown({
  markdown,
  streaming = false,
  variant = "body",
  resolveMedia,
  mediaScopeKey = "",
  mediaVisible = true,
  onOpenFile,
  testID,
}: ChatMarkdownProps): React.JSX.Element {
  const { theme } = useTheme();
  const latestMarkdown = useRef(markdown);
  latestMarkdown.current = markdown;
  const [streamText, setStreamText] = useState(markdown);
  const textTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!streaming) {
      if (textTimer.current) clearTimeout(textTimer.current);
      textTimer.current = null;
      setStreamText(markdown);
    } else if (!textTimer.current) {
      textTimer.current = setTimeout(() => {
        textTimer.current = null;
        setStreamText(latestMarkdown.current);
      }, 50);
    }
  }, [markdown, streaming]);
  useEffect(
    () => () => {
      if (textTimer.current) clearTimeout(textTimer.current);
    },
    [],
  );
  // remend drops a still-incomplete image, so partial paths are never read.
  const source = useMemo(
    () => (streaming ? remend(streamText) : markdown),
    [markdown, streamText, streaming],
  );
  const pieces = useMemo(() => splitMarkdownMedia(source), [source]);
  const paths = useMemo(
    () =>
      pieces.flatMap((piece) =>
        piece.kind === "media" && piece.media.path ? [piece.media.path] : [],
      ),
    [pieces],
  );
  const scope = String(mediaScopeKey);
  const { media, retry, reportDisplayError } = useWorkspaceMedia({
    paths,
    resolve: resolveMedia,
    scope,
    active: mediaVisible,
  });
  const [preview, setPreview] = useState<MediaPreview | null>(null);
  useEffect(() => setPreview(null), [scope]);
  const markdownStyle = useMemo(() => {
    const quiet = variant === "quiet";
    // The transcript stays uncolored: links and quotes only differ in shape.
    const ink = quiet ? theme.mutedText : theme.text;
    return {
      paragraph: {
        color: ink,
        fontSize: quiet ? 15 : 17,
        lineHeight: quiet ? 22 : 26,
        marginTop: 0,
        marginBottom: variant === "prompt" ? 0 : quiet ? 10 : 14,
      },
      h1: {
        color: theme.text,
        fontSize: 26,
        lineHeight: 32,
        fontWeight: "600" as const,
        marginTop: 20,
        marginBottom: 8,
      },
      h2: {
        color: theme.text,
        fontSize: 22,
        lineHeight: 29,
        fontWeight: "600" as const,
        marginTop: 18,
        marginBottom: 7,
      },
      h3: {
        color: theme.text,
        fontSize: 19,
        lineHeight: 27,
        fontWeight: "600" as const,
        marginTop: 16,
        marginBottom: 6,
      },
      h4: {
        color: theme.text,
        fontSize: 17,
        lineHeight: 24,
        fontWeight: "600" as const,
      },
      h5: { color: theme.text, fontSize: 16, fontWeight: "600" as const },
      h6: { color: theme.text, fontSize: 15, fontWeight: "600" as const },
      strong: { color: ink },
      em: { color: ink },
      strikethrough: { color: ink },
      underline: { color: ink },
      link: { color: ink, underline: true },
      code: {
        color: ink,
        backgroundColor: theme.codeBackground,
        borderColor: theme.border,
      },
      inlineImage: { size: 20 },
      taskList: {
        checkedColor: theme.mutedText,
        borderColor: theme.border,
        checkmarkColor: theme.background,
      },
      codeBlock: {
        color: theme.text,
        backgroundColor: theme.codeBackground,
        borderColor: theme.border,
        borderWidth: 1,
        borderRadius: 14,
        padding: 14,
        fontSize: 13,
        marginBottom: 12,
      },
      blockquote: {
        color: theme.mutedText,
        borderColor: theme.border,
        backgroundColor: theme.background,
        borderWidth: 2,
        borderRadius: 12,
        padding: 12,
        marginBottom: 10,
      },
      list: {
        color: ink,
        bulletColor: ink,
        markerColor: ink,
        gapWidth: 8,
        marginLeft: 18,
        itemSpacing: quiet ? 4 : 8,
      },
      table: {
        color: theme.text,
        headerBackgroundColor: theme.background,
        headerTextColor: theme.text,
        rowEvenBackgroundColor: theme.background,
        rowOddBackgroundColor: theme.background,
        borderColor: theme.border,
        borderWidth: 1,
        borderRadius: 0,
        cellPaddingHorizontal: 12,
        cellPaddingVertical: 12,
      },
      image: {
        borderRadius: 14,
        maxHeight: 320,
        resizeMode: "contain" as const,
        marginBottom: 10,
      },
      thematicBreak: {
        color: theme.border,
        height: 1,
        marginTop: 10,
        marginBottom: 10,
      },
      math: {
        color: theme.text,
        backgroundColor: theme.codeBackground,
        fontSize: 16,
      },
      inlineMath: { color: ink },
    };
  }, [theme, variant]);
  const onLinkPress = ({ url }: { url: string }) => {
    const path = resolveWorkspaceLinkPath(url);
    if (path) {
      onOpenFile?.(path);
      return;
    }
    if (!isSafeExternalUrl(url)) return;
    void Linking.openURL(url).catch(() => undefined);
  };

  return (
    <View testID={testID} style={styles.root}>
      {pieces.map((piece, index) =>
        piece.kind === "markdown" ? (
          <EnrichedMarkdownText
            key={`markdown-${index}`}
            flavor="github"
            markdown={piece.text}
            markdownStyle={markdownStyle}
            onLinkPress={onLinkPress}
            enableTaskListItemToggle={false}
            selectable
          />
        ) : (
          <InlineMedia
            key={`media-${index}`}
            media={piece.media}
            state={
              piece.media.path
                ? media[piece.media.path]
                : { status: "ready", uri: piece.media.uri ?? "" }
            }
            resolvable={Boolean(resolveMedia)}
            active={mediaVisible}
            onOpen={() => setPreview(piece.media)}
            onRetry={() => piece.media.path && retry(piece.media.path)}
            onDisplayError={() =>
              piece.media.path && reportDisplayError(piece.media.path)
            }
          />
        ),
      )}
      <MediaPreviewModal
        preview={preview}
        resolveMedia={resolveMedia}
        scope={scope}
        onClose={() => setPreview(null)}
      />
    </View>
  );
}

/**
 * One inline picture or video. Loading, offscreen and failed media keep the
 * loaded size, so rows do not jump when files arrive or are released.
 */
function InlineMedia({
  media,
  state,
  resolvable,
  active,
  onOpen,
  onRetry,
  onDisplayError,
}: {
  media: MarkdownMedia;
  state?: WorkspaceMediaState;
  /** Without a resolver (e.g. answered questions) workspace media shows its label. */
  resolvable: boolean;
  active: boolean;
  onOpen: () => void;
  onRetry: () => void;
  onDisplayError: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const video = media.kind === "video";
  const uri = state?.status === "ready" ? state.uri : undefined;
  const label = (
    <Text
      numberOfLines={1}
      style={[styles.mediaLabel, { color: theme.subtleText }]}
    >
      {media.label}
    </Text>
  );
  if (uri && video) return <NativeVideoCard uri={uri} label={media.label} />;
  if (uri)
    return (
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`Open image preview ${media.label}`}
        onPress={onOpen}
        style={styles.inlineImageButton}
      >
        <Image
          source={{ uri }}
          contentFit="contain"
          style={styles.inlineImage}
          accessibilityLabel={media.label}
          onError={media.path ? onDisplayError : undefined}
        />
        {label}
      </NativeAction>
    );
  if (media.path && !resolvable)
    return <View style={styles.inlineImageButton}>{label}</View>;
  const frame = [
    video ? styles.inlineVideo : styles.inlineImage,
    styles.placeholder,
    { backgroundColor: theme.surfaceMuted },
  ];
  if (state?.status === "failed")
    return (
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`Retry loading ${media.label}`}
        onPress={onRetry}
        style={video ? styles.inlineVideoCard : styles.inlineImageButton}
      >
        <View style={frame}>
          <Text style={[styles.placeholderText, { color: theme.mutedText }]}>
            {video ? "Video unavailable" : "Image unavailable"}
          </Text>
          <Text style={[styles.placeholderText, { color: theme.subtleText }]}>
            Tap to retry
          </Text>
        </View>
        {label}
      </NativeAction>
    );
  return (
    <View
      accessibilityLabel={`Loading ${media.label}`}
      style={video ? styles.inlineVideoCard : styles.inlineImageButton}
    >
      <View style={frame}>
        {active ? <ActivityIndicator color={theme.subtleText} /> : null}
      </View>
      {label}
    </View>
  );
}

/** Full-screen preview; it holds its own lease while open. */
function MediaPreviewModal({
  preview,
  resolveMedia,
  scope,
  onClose,
}: {
  preview: MediaPreview | null;
  resolveMedia?: ChatMarkdownProps["resolveMedia"];
  scope: string;
  onClose: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const paths = useMemo(
    () => (preview?.path ? [preview.path] : []),
    [preview?.path],
  );
  const { media } = useWorkspaceMedia({
    paths,
    resolve: resolveMedia,
    scope,
    active: preview !== null,
  });
  const state = preview?.path ? media[preview.path] : undefined;
  const uri = preview?.path
    ? state?.status === "ready"
      ? state.uri
      : undefined
    : preview?.uri;
  return (
    <NativeModal
      visible={preview !== null}
      onClose={onClose}
      title={preview?.label ?? "Image preview"}
      testID="image-preview"
      scrollable={false}
    >
      <ScrollView
        style={{ flex: 1 }}
        contentContainerStyle={{ flexGrow: 1 }}
        maximumZoomScale={4}
        minimumZoomScale={1}
        centerContent
        accessibilityLabel="Image preview"
      >
        {uri ? (
          <Image
            source={{ uri }}
            contentFit="contain"
            style={{ width: "100%", minHeight: 500, flex: 1 }}
            accessibilityLabel={preview?.label}
          />
        ) : preview ? (
          <View style={styles.previewStatus}>
            {state?.status === "failed" ? (
              <Text style={{ color: theme.mutedText }}>Image unavailable</Text>
            ) : (
              <ActivityIndicator color={theme.subtleText} />
            )}
          </View>
        ) : null}
      </ScrollView>
    </NativeModal>
  );
}

function NativeVideoCard({
  uri,
  label,
}: {
  uri: string;
  label: string;
}): React.JSX.Element {
  const player = useVideoPlayer(uri, (instance) => {
    instance.loop = false;
  });
  return (
    <View accessibilityLabel={`Video ${label}`} style={styles.inlineVideoCard}>
      <VideoView
        player={player}
        nativeControls
        contentFit="contain"
        style={styles.inlineVideo}
        accessibilityLabel={label}
      />
      <Text numberOfLines={1} style={styles.videoLabel}>
        {label}
      </Text>
    </View>
  );
}

export function WorkspaceFileLabel({
  path,
  onPress,
}: {
  path: string;
  onPress?: (path: string) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <NativeAction
      accessibilityRole="button"
      accessibilityLabel={`Open file ${path}`}
      onPress={() => onPress?.(path)}
      style={styles.fileLabel}
    >
      <Text
        numberOfLines={1}
        style={{
          color: theme.text,
          fontSize: 14,
          textDecorationLine: "underline",
        }}
      >
        {path}
      </Text>
    </NativeAction>
  );
}

const styles = StyleSheet.create({
  root: { minWidth: 0 },
  inlineImageButton: {
    minHeight: 44,
    alignSelf: "flex-start",
    maxWidth: "100%",
    gap: 5,
    paddingVertical: 4,
  },
  inlineImage: { width: 260, maxWidth: "100%", height: 220, borderRadius: 14 },
  inlineVideoCard: { width: "100%", maxWidth: 480, gap: 5, paddingVertical: 4 },
  inlineVideo: {
    width: "100%",
    height: 240,
    borderRadius: 14,
    backgroundColor: "#000000",
  },
  placeholder: { alignItems: "center", justifyContent: "center", gap: 4 },
  placeholderText: { fontSize: 13, textAlign: "center" },
  mediaLabel: { fontSize: 12 },
  previewStatus: { flex: 1, alignItems: "center", justifyContent: "center" },
  videoLabel: { color: "#858590", fontSize: 12 },
  fileLabel: { minHeight: 44, justifyContent: "center" },
});
