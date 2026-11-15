import type { ToolAttachment } from '@opencuria/chat-core/harnessAttachments'

/** Thumbnail tokens for attachments whose bytes live in the workspace. */
export function workspaceAttachmentTokens(
  attachments: ToolAttachment[],
  label: (attachment: ToolAttachment, index: number) => string,
): { kind: 'file'; path: string; label: string; start: number; end: number }[] {
  return attachments.flatMap((attachment, index) =>
    attachment.path
      ? [
          {
            kind: 'file',
            path: attachment.path,
            label: label(attachment, index),
            start: index,
            end: index,
          },
        ]
      : [],
  )
}
