# Chat message footers

## Design

Use one compact footer component for both message roles. Message content stays
primary; the footer is a quiet row without a background, border, or separate
usage line. Align it with its message: user actions on the right, assistant
actions on the left. Keep only a 2 px gap before the footer and remove trailing
prose margins so the controls stay visually attached to their message.

| Message | Actions, in order | Metadata |
| --- | --- | --- |
| User | Copy message, Edit & rerun | Skills and attachments stay with the message content. |
| Assistant | Copy answer, Fork after this answer | Model, reasoning effort, cost, input/output tokens in the same row, separated from actions by a subtle divider. |

Reuse the shared ghost buttons, clipboard feedback, tooltips, and popover.
Buttons use equal 28 px targets (32 px for coarse pointers) with 12 px icons
and 11 px inline metadata. Metadata shrinks
and truncates before actions do; hover/focus reveals the full text in a tooltip,
and clicking/tapping opens a popover. Do not wrap a long usage string into an
extra footer row or introduce horizontal scrolling.

## Visibility and states

- On large screens (768 px and up), every footer appears on message hover or
  keyboard focus, including the latest exchange. Space remains reserved to
  avoid layout jumps.
- On smaller screens and devices without hover, all footers stay visible.
- Streaming shows model metadata but exposes neither copy-answer nor fork,
  following the same responsive visibility rules.
- Read-only and subagent views retain copy/metadata but hide edit/fork. Busy
  sessions hide fork only.
- The inline user editor temporarily replaces its footer. It preselects the
  session model/effort and lets the user change them for the rerun. Rerun
  works with an unchanged prompt; on a busy session it first asks to stop and
  discard the running turn (queued steers return to the composer, like Stop).
- Keep accessible action labels and the existing success/failure clipboard UX.

## Fork semantics

Fork belongs to the assistant answer, not to the user prompt. The new root chat
includes the selected answer and all preceding messages/parts, opens with an
empty composer, and never automatically sends a prompt. The source conversation
is unchanged. This copies chat history only, not workspace files or snapshots.

The REST fork contract includes the assistant answer selected by `message_id`.
Omitting the ID copies the entire session. User-message cutoffs are rejected.

## OpenWebUI reference

OpenWebUI's `src/lib/components/chat/Messages/UserMessage.svelte` and
`ResponseMessage.svelte` place role-specific actions below each message and fork
below completed assistant responses. `src/app.css` reveals older actions on
hover/focus. OpenCuria adopts that placement and quiet icon language, but uses
one shared footer, identical visibility rules for both roles, and explicit touch
support. It deliberately does not add ratings, delete, regenerate, timestamps,
or branch navigation without corresponding product behavior.
