/** Select plain message text that is safe and useful to copy from the chat. */

import { buildMessageBlocks, type MessageRenderBlock } from "./harnessBlocks";
import type { HarnessMessage, HarnessPart } from "./harness";

/**
 * Return a user message verbatim, or only the assistant's closing prose.
 * Assistant work, plans, reasoning, and message metadata are intentionally
 * excluded; the visible block model determines where the final answer ends.
 */
export function getMessageCopyText(
  message: HarnessMessage,
  streaming = false,
): string | null {
  if (message.role === "user") return message.content;
  if (streaming) return null;

  const blocks = buildMessageBlocks(message.parts, { finished: true });
  const answerParts = trailingTextParts(blocks);
  if (answerParts.length === 0) return null;

  const text = answerParts.map((part) => part.output).join("");
  return text.trim() ? text : null;
}

/** Collect the final adjacent text blocks, without crossing work or plans. */
function trailingTextParts(blocks: MessageRenderBlock[]): HarnessPart[] {
  const parts: HarnessPart[] = [];
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block?.kind !== "text") break;
    parts.unshift(block.part);
  }
  return parts;
}
