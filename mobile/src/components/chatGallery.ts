import type { HarnessMessage } from "@opencuria/chat-core/harness";
import { parseComposerSegments } from "@opencuria/chat-core/composerTokens";
import { classifyWorkspaceFile } from "@opencuria/chat-core/workspaceFileRefs";
import { splitMarkdownMedia, type MarkdownMedia } from "./markdownMedia";

/** Identity of a picture shared by the transcript and the media viewer. */
export function mediaKey(media: Pick<MarkdownMedia, "path" | "uri">): string {
  return media.path ?? media.uri ?? "";
}

/** Markdown that shows an `@file:` image mention as a picture. */
export function imageMentionMarkdown(path: string, name: string): string {
  return `![${name.replace(/[[\]]/g, "")}](<${path}>)`;
}

/** Pictures of a Markdown text in reading order; videos stay inline. */
export function markdownImages(markdown: string): MarkdownMedia[] {
  return splitMarkdownMedia(markdown).flatMap((piece) =>
    piece.kind === "media" && piece.media.kind === "image" ? [piece.media] : [],
  );
}

/** Pictures of a prompt: Markdown images and image file mentions. */
function promptImages(content: string): MarkdownMedia[] {
  return parseComposerSegments(content).flatMap((segment) => {
    if (segment.kind === "text") return markdownImages(segment.text);
    if (
      segment.kind === "file" &&
      classifyWorkspaceFile(segment.path) === "image"
    )
      return markdownImages(imageMentionMarkdown(segment.path, segment.name));
    return [];
  });
}

/** Snapshots are immutable, so each message is read once per version. */
const messageCache = new WeakMap<HarnessMessage, MarkdownMedia[]>();

function messageImages(message: HarnessMessage): MarkdownMedia[] {
  const cached = messageCache.get(message);
  if (cached) return cached;
  const images =
    message.role === "user"
      ? promptImages(message.content)
      : [...message.parts]
          .sort((left, right) => (left.position ?? 0) - (right.position ?? 0))
          .flatMap((part) =>
            part.type === "text"
              ? markdownImages(part.output ?? "")
              : part.type === "steer"
                ? promptImages(part.output ?? "")
                : [],
          );
  messageCache.set(message, images);
  return images;
}

/**
 * Every picture of the conversation, oldest first and each file once: what
 * the user sent and what the agent showed. Tool output stays out, it is
 * reached through its own disclosure.
 */
export function chatImages(
  orderedMessages: readonly HarnessMessage[],
): MarkdownMedia[] {
  const seen = new Set<string>();
  const images: MarkdownMedia[] = [];
  for (const message of orderedMessages)
    for (const media of messageImages(message)) {
      const key = mediaKey(media);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      images.push(media);
    }
  return images;
}

/** A set of pictures to page through, opened at `index`. */
export interface MediaGallery {
  items: readonly MarkdownMedia[];
  index: number;
}

/**
 * The gallery for a tapped picture: the whole conversation when it belongs
 * to it, otherwise the pictures shown beside it (e.g. inside a tool result).
 */
export function galleryFor(
  media: MarkdownMedia,
  conversation: readonly MarkdownMedia[],
  siblings: readonly MarkdownMedia[],
): MediaGallery {
  const key = mediaKey(media);
  const index = conversation.findIndex((item) => mediaKey(item) === key);
  if (index >= 0) return { items: conversation, index };
  const local = siblings.length ? siblings : [media];
  return {
    items: local,
    index: Math.max(
      0,
      local.findIndex((item) => mediaKey(item) === key),
    ),
  };
}
