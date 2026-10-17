import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Linking,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Image } from "expo-image";
import { VideoView, useVideoPlayer } from "expo-video";
import { marked } from "marked";
import { EnrichedMarkdownText } from "react-native-enriched-markdown";
import { X } from "lucide-react-native";
import {
  classifyWorkspaceFile,
  parseMarkdownLinkDest,
  resolveWorkspaceLinkPath,
  resolveWorkspaceMediaPath,
} from "@opencuria/chat-core/workspaceFileRefs";
import remend from "remend";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { IconButton } from "../ui/primitives";
import { useTheme } from "../ui/theme";

export type ResolvedChatMedia = string | { uri: string; release: () => void };

export interface ChatMarkdownProps {
  markdown: string;
  streaming?: boolean;
  resolveMedia?: (workspacePath: string) => Promise<ResolvedChatMedia>;
  /** Changes with the host media-cache scope (workspace/account/server). */
  mediaScopeKey?: string | number;
  /** Parent list viewability gate; offscreen rows do not fetch workspace media. */
  mediaVisible?: boolean;
  onOpenFile?: (workspacePath: string) => void;
  testID?: string;
}

interface ImageReference {
  start: number;
  end: number;
  path?: string;
  destination: string;
  alt: string;
  safe: boolean;
  linked: boolean;
}
type MarkdownSegment =
  | { kind: "markdown"; text: string }
  | {
      kind: "image";
      uri?: string;
      path?: string;
      failed?: boolean;
      label: string;
    }
  | {
      kind: "video";
      uri?: string;
      path?: string;
      failed?: boolean;
      label: string;
    };

/** Render GFM natively; workspace media is loaded only for visible chat rows. */
export function ChatMarkdown({
  markdown,
  streaming = false,
  resolveMedia,
  mediaScopeKey = "",
  mediaVisible = true,
  onOpenFile,
  testID,
}: ChatMarkdownProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
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
  const source = useMemo(
    () => (streaming ? remend(streamText) : markdown),
    [markdown, streamText, streaming],
  );
  const references = useMemo(() => collectImageReferences(source), [source]);
  const paths = useMemo(
    () => [...new Set(references.flatMap(({ path }) => (path ? [path] : [])))],
    [references],
  );
  const scope = String(mediaScopeKey);
  const pathKey = useMemo(() => JSON.stringify([scope, paths]), [paths, scope]);
  const resolverRef = useRef(resolveMedia);
  resolverRef.current = resolveMedia;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const leaseEpoch = useRef(0);
  const leases = useRef(new Map<string, () => void>());
  const visibleRef = useRef(mediaVisible);
  visibleRef.current = mediaVisible;
  const requests = useRef(new Map<string, Promise<string | null>>());
  const settled = useRef(new Map<string, string | null>());
  const requestScope = useRef(scope);
  const [media, setMedia] = useState<Record<string, string | null>>({});
  const [preview, setPreview] = useState<{ uri: string; label: string } | null>(
    null,
  );

  useEffect(() => {
    return () => {
      leaseEpoch.current += 1;
      for (const release of leases.current.values()) release();
      leases.current.clear();
      requests.current.clear();
      settled.current.clear();
    };
  }, [scope, mediaVisible]);

  useEffect(() => {
    let alive = true;
    if (requestScope.current !== scope) {
      requestScope.current = scope;
      requests.current.clear();
      settled.current.clear();
      setMedia((current) => (Object.keys(current).length ? {} : current));
      setPreview(null);
    }
    if (!mediaVisible) {
      setMedia((current) => (Object.keys(current).length ? {} : current));
      setPreview(null);
      return () => {
        alive = false;
      };
    }
    if (!resolverRef.current || paths.length === 0)
      return () => {
        alive = false;
      };

    const keys = paths.map((path) => `${scope}\0${path}`);
    const activeKeys = new Set(keys);
    const pending = keys.map((key, index) => {
      if (settled.current.has(key))
        return Promise.resolve([key, settled.current.get(key)!] as const);
      const existing = requests.current.get(key);
      if (existing) return existing.then((uri) => [key, uri] as const);
      const path = paths[index]!;
      const epoch = leaseEpoch.current;
      const request = Promise.resolve()
        .then(
          () =>
            resolverRef.current?.(path) ??
            Promise.reject(new Error("Media resolver unavailable.")),
        )
        .then(
          (resolved) => {
            if (
              epoch !== leaseEpoch.current ||
              !visibleRef.current ||
              scopeRef.current !== scope
            ) {
              if (typeof resolved !== "string") resolved.release();
              return null;
            }
            if (typeof resolved !== "string")
              leases.current.set(key, resolved.release);
            return (
              (typeof resolved === "string" ? resolved : resolved.uri) || null
            );
          },
          () => null,
        );
      requests.current.set(key, request);
      void request.then((uri) => {
        if (requests.current.get(key) === request) requests.current.delete(key);
        if (scopeRef.current === scope && epoch === leaseEpoch.current)
          settled.current.set(key, uri);
      });
      return request.then((uri) => [key, uri] as const);
    });

    void Promise.all(pending).then((resolved) => {
      if (!alive || scopeRef.current !== scope) return;
      setMedia((previous) => {
        const next: Record<string, string | null> = {};
        for (const [key, uri] of resolved)
          if (activeKeys.has(key)) next[key] = uri;
        if (
          Object.keys(previous).length === Object.keys(next).length &&
          Object.keys(next).every((key) => previous[key] === next[key])
        )
          return previous;
        return next;
      });
    });
    return () => {
      alive = false;
    };
    // Media state is output, not an effect trigger. Streaming prose changes share
    // cached and in-flight requests for unchanged workspace paths.
  }, [pathKey, mediaVisible, Boolean(resolveMedia), scope]);

  const segments = useMemo(
    () => buildSegments(source, references, media, scope),
    [media, references, scope, source],
  );
  const markdownStyle = useMemo(
    () => ({
      paragraph: {
        color: theme.text,
        fontSize: 16,
        lineHeight: 24,
        marginTop: 2,
        marginBottom: 10,
      },
      h1: {
        color: theme.text,
        fontSize: 26,
        lineHeight: 32,
        fontWeight: "600" as const,
        marginBottom: 8,
      },
      h2: {
        color: theme.text,
        fontSize: 22,
        lineHeight: 29,
        fontWeight: "600" as const,
        marginBottom: 7,
      },
      h3: {
        color: theme.text,
        fontSize: 19,
        lineHeight: 27,
        fontWeight: "600" as const,
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
      strong: { color: theme.text },
      em: { color: theme.text },
      strikethrough: { color: theme.text },
      underline: { color: theme.text },
      link: { color: theme.primary, underline: true },
      code: {
        color: theme.text,
        backgroundColor: theme.codeBackground,
        borderColor: theme.border,
      },
      inlineImage: { size: 20 },
      taskList: {
        checkedColor: theme.primary,
        borderColor: theme.border,
        checkmarkColor: theme.primaryForeground,
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
        borderColor: theme.primary,
        backgroundColor: theme.surfaceMuted,
        borderWidth: 2,
        borderRadius: 12,
        padding: 12,
        marginBottom: 10,
      },
      list: {
        color: theme.text,
        bulletColor: theme.primary,
        markerColor: theme.mutedText,
        gapWidth: 8,
        marginLeft: 18,
        itemSpacing: 4,
      },
      table: {
        color: theme.text,
        headerBackgroundColor: theme.surfaceMuted,
        headerTextColor: theme.text,
        rowEvenBackgroundColor: theme.surface,
        rowOddBackgroundColor: theme.surfaceMuted,
        borderColor: theme.border,
        borderWidth: 1,
        borderRadius: 10,
        cellPaddingHorizontal: 10,
        cellPaddingVertical: 7,
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
      inlineMath: { color: theme.text },
    }),
    [theme],
  );
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
      {segments.map((segment, index) =>
        segment.kind === "markdown" ? (
          <EnrichedMarkdownText
            key={`markdown-${index}`}
            flavor="github"
            markdown={segment.text}
            markdownStyle={markdownStyle}
            onLinkPress={onLinkPress}
            enableTaskListItemToggle={false}
            selectable
          />
        ) : segment.kind === "video" ? (
          segment.uri ? (
            <NativeVideoCard
              key={`video-${index}`}
              uri={segment.uri}
              label={segment.label}
            />
          ) : (
            <View
              key={`video-pending-${index}`}
              style={styles.inlineVideoPlaceholder}
            >
              <Text style={{ color: theme.subtleText, fontSize: 13 }}>
                {segment.failed
                  ? `${segment.label} · video unavailable`
                  : segment.label}
              </Text>
            </View>
          )
        ) : segment.uri ? (
          <Pressable
            key={`image-${index}`}
            accessibilityRole="button"
            accessibilityLabel={`Open image preview ${segment.label}`}
            onPress={() =>
              setPreview({ uri: segment.uri!, label: segment.label })
            }
            style={styles.inlineImageButton}
          >
            <Image
              source={{ uri: segment.uri }}
              contentFit="contain"
              style={styles.inlineImage}
              accessibilityLabel={segment.label}
            />
            <Text
              numberOfLines={1}
              style={{ color: theme.subtleText, fontSize: 12 }}
            >
              {segment.label}
            </Text>
          </Pressable>
        ) : (
          <View key={`image-pending-${index}`} style={styles.inlineImageButton}>
            <Text style={{ color: theme.subtleText, fontSize: 13 }}>
              {segment.failed
                ? `${segment.label} · image unavailable`
                : segment.label}
            </Text>
          </View>
        ),
      )}
      <Modal
        visible={preview !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setPreview(null)}
        statusBarTranslucent
      >
        <View
          style={[
            styles.previewRoot,
            {
              backgroundColor: theme.overlay,
              paddingTop: insets.top,
              paddingBottom: insets.bottom,
            },
          ]}
        >
          <View style={[styles.previewHeader, { top: insets.top + 8 }]}>
            <Text numberOfLines={1} style={styles.previewLabel}>
              {preview?.label ?? ""}
            </Text>
            <IconButton
              icon={<X size={21} color="#FFFFFF" />}
              label="Close image preview"
              onPress={() => setPreview(null)}
            />
          </View>
          <Pressable
            accessibilityLabel="Close image preview"
            accessibilityRole="button"
            style={styles.previewImageArea}
            onPress={() => setPreview(null)}
          >
            {preview ? (
              <Image
                source={{ uri: preview.uri }}
                contentFit="contain"
                style={styles.previewImage}
                accessibilityLabel={preview.label}
              />
            ) : null}
          </Pressable>
        </View>
      </Modal>
    </View>
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
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Open file ${path}`}
      onPress={() => onPress?.(path)}
      style={styles.fileLabel}
    >
      <Text numberOfLines={1} style={{ color: theme.primary, fontSize: 14 }}>
        {path}
      </Text>
    </Pressable>
  );
}

function isSafeExternalUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const sensitiveParameter =
      /(?:^|[_-])(token|auth|key|secret|signature|credential|password)(?:$|[_-])/i;
    return (
      (url.protocol === "https:" || (__DEV__ && url.protocol === "http:")) &&
      Boolean(url.hostname) &&
      !url.username &&
      !url.password &&
      ![...url.searchParams.keys()].some((key) => sensitiveParameter.test(key))
    );
  } catch {
    return false;
  }
}
function filename(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? "Media";
}
interface LexToken {
  type?: string;
  raw?: string;
  href?: string;
  text?: string;
  tokens?: unknown[];
  items?: unknown[];
  [key: string]: unknown;
}
function asToken(value: unknown): LexToken | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as LexToken)
    : null;
}
function collectImageReferences(markdown: string): ImageReference[] {
  let tokens: unknown[];
  try {
    tokens = marked.lexer(markdown) as unknown[];
  } catch {
    return [];
  }
  const references: ImageReference[] = [];
  const walk = (
    siblings: unknown[],
    parentStart: number,
    insideLink = false,
  ): number => {
    let searchFrom = parentStart;
    for (const value of siblings) {
      const token = asToken(value);
      if (!token || typeof token.raw !== "string" || !token.raw) continue;
      const start = markdown.indexOf(token.raw, searchFrom);
      if (start < 0) continue;
      const end = start + token.raw.length;
      const nestedLink =
        insideLink || token.type === "link" || token.type === "link_ref";
      if (token.type === "image" && !insideLink) {
        const destination =
          typeof token.href === "string"
            ? parseMarkdownLinkDest(token.href)
            : "";
        const path = destination
          ? resolveWorkspaceMediaPath(destination)
          : null;
        references.push({
          start,
          end,
          path: path ?? undefined,
          destination,
          alt:
            typeof token.text === "string" && token.text
              ? token.text
              : path
                ? filename(path)
                : "Media",
          safe: Boolean(path) || isSafeExternalUrl(destination),
          linked: false,
        });
      }
      let nestedEnd = start;
      for (const child of Object.values(token))
        if (child !== token.raw && Array.isArray(child) && child.length)
          nestedEnd = Math.max(nestedEnd, walk(child, start, nestedLink));
      searchFrom = Math.max(end, nestedEnd);
    }
    return searchFrom;
  };
  walk(tokens, 0);
  return references.sort((a, b) => a.start - b.start);
}
function buildSegments(
  source: string,
  references: ImageReference[],
  media: Record<string, string | null>,
  scope: string,
): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  let cursor = 0;
  for (const reference of references) {
    if (reference.start < cursor || reference.end > source.length) continue;
    if (reference.start > cursor)
      segments.push({
        kind: "markdown",
        text: source.slice(cursor, reference.start),
      });
    if (reference.path) {
      const kind = classifyWorkspaceFile(reference.path);
      const uri = media[`${scope}\0${reference.path}`] ?? undefined;
      const failed = media[`${scope}\0${reference.path}`] === null;
      if (kind === "image")
        segments.push({
          kind: "image",
          path: reference.path,
          uri,
          failed,
          label: reference.alt,
        });
      else if (kind === "video")
        segments.push({
          kind: "video",
          path: reference.path,
          uri,
          failed,
          label: reference.alt,
        });
      else segments.push({ kind: "markdown", text: reference.alt });
    } else if (reference.safe)
      segments.push({
        kind: "image",
        uri: reference.destination,
        label: reference.alt,
      });
    else segments.push({ kind: "markdown", text: reference.alt });
    cursor = reference.end;
  }
  if (cursor < source.length)
    segments.push({ kind: "markdown", text: source.slice(cursor) });
  if (!segments.length && source)
    segments.push({ kind: "markdown", text: source });
  return segments;
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
  inlineVideoPlaceholder: { minHeight: 44, justifyContent: "center" },
  videoLabel: { color: "#858590", fontSize: 12 },
  previewRoot: { flex: 1, justifyContent: "center" },
  previewHeader: {
    position: "absolute",
    left: 16,
    right: 16,
    zIndex: 1,
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  previewLabel: { color: "#FFFFFF", fontSize: 14, flex: 1, marginRight: 8 },
  previewImageArea: { flex: 1, justifyContent: "center", alignItems: "center" },
  previewImage: { width: "100%", height: "82%" },
  fileLabel: { minHeight: 44, justifyContent: "center" },
});
