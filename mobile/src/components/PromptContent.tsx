import React, { useContext, useMemo, useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Image } from "expo-image";
import { Sparkles } from "lucide-react-native";
import type {
  ComposerFileToken,
  ComposerToken,
} from "@opencuria/chat-core/composerTokens";
import { workspaceRelativePath } from "@opencuria/chat-core/harnessMentions";
import { resolveWorkspaceLinkPath } from "@opencuria/chat-core/workspaceFileRefs";
import { NativeAction } from "../ui/controls";
import { useTheme } from "../ui/theme";
import { WorkspaceFileIcon } from "../tools/files/WorkspaceFileIcon";
import { galleryFor, type MediaGallery } from "./chatGallery";
import { isSafeExternalUrl, type MarkdownMedia } from "./markdownMedia";
import { MediaPlaceholder } from "./MediaPlaceholder";
import { MediaGalleryContext, MediaViewer } from "./MediaViewer";
import {
  placeholderState,
  useWorkspaceMedia,
  type ChatMediaResolver,
} from "./useWorkspaceMedia";
import type { PromptRun } from "./promptBlocks";
import { shortFileName } from "./composerDisplay";

/** Type of the prompt text; chips are sized against it. */
const PROMPT_FONT = 17;
const PROMPT_LINE = 26;

/**
 * A prompt paragraph laid out natively, so mention chips flow with its text
 * like the webapp's badges.
 */
export function PromptParagraph({
  runs,
  onOpenFile,
  testID,
}: {
  runs: PromptRun[];
  onOpenFile?: (path: string) => void;
  testID?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  const openLink = (href: string) => {
    const path = resolveWorkspaceLinkPath(href);
    if (path) onOpenFile?.(path);
    else if (isSafeExternalUrl(href))
      void Linking.openURL(href).catch(() => undefined);
  };
  return (
    <Text testID={testID} style={[styles.paragraph, { color: theme.text }]}>
      {runs.map((run, index) =>
        run.kind === "mention" ? (
          <MentionChip
            key={`mention:${index}`}
            token={run.token}
            onOpenFile={onOpenFile}
          />
        ) : (
          <Text
            key={`text:${index}`}
            onPress={run.href ? () => openLink(run.href!) : undefined}
            style={[
              run.strong && styles.strong,
              run.em && styles.em,
              (run.strike || run.href) && {
                textDecorationLine: run.strike
                  ? run.href
                    ? "underline line-through"
                    : "line-through"
                  : "underline",
              },
              run.code && [
                styles.code,
                { backgroundColor: theme.codeBackground },
              ],
            ]}
          >
            {run.text}
          </Text>
        ),
      )}
    </Text>
  );
}

/** The webapp's mention badge: file icon and name, or the agent. */
export function MentionChip({
  token,
  onOpenFile,
}: {
  token: ComposerToken;
  onOpenFile?: (path: string) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const file = token.kind === "file";
  const chip = (
    <>
      {file ? (
        <WorkspaceFileIcon path={token.path} size={14} />
      ) : (
        <Sparkles size={13} color={theme.primary} />
      )}
      <Text
        style={[styles.chipLabel, { color: file ? theme.text : theme.primary }]}
      >
        {file ? shortFileName(token.name) : `@agent:${token.name}`}
      </Text>
    </>
  );
  const style = [
    styles.chip,
    { backgroundColor: theme.surface, borderColor: theme.border },
  ];
  return file && onOpenFile ? (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Open file ${workspaceRelativePath(token.path)}`}
      onPress={() => onOpenFile(token.path)}
      style={({ pressed }) => [style, pressed && styles.pressed]}
      testID="prompt-mention-file"
    >
      {chip}
    </Pressable>
  ) : (
    <View
      accessibilityLabel={
        file ? workspaceRelativePath(token.path) : `Agent ${token.name}`
      }
      style={style}
      testID={file ? "prompt-mention-file" : "prompt-mention-agent"}
    >
      {chip}
    </View>
  );
}

/**
 * Thumbnails of the pictures a prompt mentions, below its bubble as in the
 * webapp. A tap opens the chat's gallery.
 */
export function PromptImages({
  images,
  resolveMedia,
  mediaVisible = true,
  mediaScopeKey,
  testID = "prompt-images",
}: {
  images: ComposerFileToken[];
  resolveMedia?: ChatMediaResolver;
  mediaVisible?: boolean;
  mediaScopeKey?: string | number;
  testID?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  const scope = String(mediaScopeKey ?? "");
  const items = useMemo<MarkdownMedia[]>(
    () =>
      images.map((image) => ({
        kind: "image",
        path: image.path,
        label: image.name,
      })),
    [images],
  );
  const paths = useMemo(() => images.map((image) => image.path), [images]);
  const { media, retry, request, reportDisplayError } = useWorkspaceMedia({
    paths,
    resolve: resolveMedia,
    scope,
    active: mediaVisible,
  });
  const openChatGallery = useContext(MediaGalleryContext);
  const [gallery, setGallery] = useState<MediaGallery | null>(null);
  return (
    <View testID={testID} style={styles.images}>
      {items.map((item) => {
        const state = media[item.path!];
        const frame = [
          styles.thumbnail,
          { backgroundColor: theme.surfaceMuted, borderColor: theme.border },
        ];
        if (state?.status === "ready")
          return (
            <NativeAction
              key={item.path}
              accessibilityRole="button"
              accessibilityLabel={`Open image preview ${item.label}`}
              onPress={() =>
                openChatGallery
                  ? openChatGallery(item, items)
                  : setGallery(galleryFor(item, [], items))
              }
              style={frame}
            >
              <Image
                source={{ uri: state.uri }}
                contentFit="cover"
                style={styles.thumbnailImage}
                accessibilityLabel={item.label}
                onError={() => reportDisplayError(item.path!)}
              />
            </NativeAction>
          );
        const placeholder = placeholderState(state);
        if (placeholder)
          return (
            <MediaPlaceholder
              key={item.path}
              state={placeholder}
              kind="image"
              label={item.label}
              fit="cover"
              compact
              onLoad={() => request(item.path!)}
              style={frame}
            />
          );
        if (state?.status === "failed")
          return (
            <NativeAction
              key={item.path}
              accessibilityRole="button"
              accessibilityLabel={`Retry loading ${item.label}`}
              onPress={() => retry(item.path!)}
              style={[frame, styles.thumbnailFallback]}
            >
              <WorkspaceFileIcon path={item.path!} size={16} />
              <Text
                numberOfLines={1}
                style={[styles.thumbnailLabel, { color: theme.mutedText }]}
              >
                {item.label}
              </Text>
            </NativeAction>
          );
        return (
          <View
            key={item.path}
            accessibilityLabel={`Loading ${item.label}`}
            style={[frame, styles.thumbnailFallback]}
          >
            {resolveMedia && mediaVisible ? (
              <ActivityIndicator color={theme.subtleText} />
            ) : (
              <>
                <WorkspaceFileIcon path={item.path!} size={16} />
                <Text
                  numberOfLines={1}
                  style={[styles.thumbnailLabel, { color: theme.mutedText }]}
                >
                  {item.label}
                </Text>
              </>
            )}
          </View>
        );
      })}
      {gallery ? (
        <MediaViewer
          gallery={gallery}
          resolveMedia={resolveMedia}
          scope={scope}
          onClose={() => setGallery(null)}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  paragraph: { fontSize: PROMPT_FONT, lineHeight: PROMPT_LINE },
  strong: { fontWeight: "600" },
  em: { fontStyle: "italic" },
  code: { fontFamily: "monospace", fontSize: 15 },
  // Inline views sit on the baseline; the nudge centers the chip on the text.
  // Sized by its short label: the text layout wraps it as one word.
  chip: {
    height: 24,
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 7,
    borderRadius: 7,
    borderWidth: StyleSheet.hairlineWidth,
    transform: [{ translateY: 5 }],
  },
  pressed: { opacity: 0.6 },
  chipLabel: { fontSize: 14, lineHeight: 18, fontWeight: "500" },
  images: {
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "flex-end",
    gap: 6,
  },
  thumbnail: {
    width: 88,
    height: 66,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    overflow: "hidden",
  },
  thumbnailImage: { width: "100%", height: "100%" },
  thumbnailFallback: {
    alignItems: "center",
    justifyContent: "center",
    gap: 4,
    paddingHorizontal: 6,
  },
  thumbnailLabel: { fontSize: 10, maxWidth: "100%" },
});
