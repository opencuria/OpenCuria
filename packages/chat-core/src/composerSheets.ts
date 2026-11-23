import type {
  HarnessMessage,
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessScheduledMessage,
  HarnessSteer,
  HarnessTodo,
} from "./harness";
import type { MentionCandidate } from "./harnessMentions";

/**
 * One entry of the composer sheet stack shared by web and mobile.
 *
 * Sheets are ordered by interaction priority (highest first): `mention` >
 * `processes` > `question` > `permission` > `queue` > `notice` > `context` >
 * `todos`. User-opened inputs (mention suggestions, processes panel) stay on
 * top; agent gates (questions, permissions) follow; the ambient queue of
 * outgoing messages (steers of the running turn, then scheduled messages)
 * comes next; passive notices come last.
 * Only the topmost sheet is interactive; lower sheets render as
 * non-interactive peek edges, iOS sheet-stack style.
 */
export type ComposerSheetKind =
  | "mention"
  | "question"
  | "permission"
  | "queue"
  | "notice"
  | "processes"
  | "context"
  | "todos";

export interface ContextSheetState {
  used: number;
  limit: number;
  percent: number;
  promptTokens?: number;
  completionTokens?: number;
}

export interface NoticeSheetState {
  messageId: string;
  text: string;
  tone: "error" | "info";
}

/** Message fields the run notice reads. */
export type RunNoticeMessage = Pick<
  HarnessMessage,
  "id" | "role" | "error" | "finish" | "notice_dismissed_at"
>;

/**
 * The failed or stopped run the composer reports in its `notice` sheet: the
 * newest undismissed assistant error after the last user message, so a new
 * prompt clears it at once (the backend also dismisses older notices on
 * send). A stop by the user reads as information, not as an error.
 * `dismissedIds` holds optimistic dismissals the server has not confirmed.
 */
export function runNotice(
  messages: readonly RunNoticeMessage[],
  dismissedIds: Readonly<Record<string, boolean>> = {},
): NoticeSheetState | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (!message) continue;
    if (message.role === "user") return null;
    if (!message.error) continue;
    if (message.notice_dismissed_at || dismissedIds[message.id]) continue;
    const stoppedByUser =
      message.finish === "aborted" && message.error === "aborted by user";
    return {
      messageId: message.id,
      text: stoppedByUser ? "Run stopped by user" : message.error,
      tone: stoppedByUser ? "info" : "error",
    };
  }
  return null;
}

export interface MentionSheetState {
  candidates: MentionCandidate[];
  /** Keyboard-highlighted candidate; -1 when the client has no highlight. */
  activeIndex: number;
}

export interface ComposerSheet {
  kind: ComposerSheetKind;
  mention?: MentionSheetState;
  question?: HarnessQuestionRequest;
  /** All pending question requests (for the `i of N` pager). */
  questions?: HarnessQuestionRequest[];
  permission?: HarnessPermissionRequest;
  /** All pending permission requests (for the `i of N` pager). */
  permissions?: HarnessPermissionRequest[];
  /** Messages queued for the running turn (FIFO). */
  queue?: HarnessSteer[];
  /** Messages scheduled for later, earliest first (same sheet as `queue`). */
  scheduled?: HarnessScheduledMessage[];
  notice?: NoticeSheetState;
  todos?: HarnessTodo[];
  context?: ContextSheetState;
}

export interface ComposerSheetInput {
  mention?: MentionSheetState | null;
  questions?: HarnessQuestionRequest[];
  permissions?: HarnessPermissionRequest[];
  queue?: HarnessSteer[];
  scheduled?: HarnessScheduledMessage[];
  notice?: NoticeSheetState | null;
  todos?: HarnessTodo[];
  processesOpen?: boolean;
  contextOpen?: boolean;
  context?: ContextSheetState | null;
}

/** Priority rank: lower order renders on top of the stack. */
const SHEET_ORDER: Record<ComposerSheetKind, number> = {
  mention: 0,
  processes: 1,
  question: 2,
  permission: 3,
  queue: 4,
  notice: 5,
  context: 6,
  todos: 7,
};

/**
 * Build the ordered composer sheet stack from the currently active
 * overlays. Empty sources contribute no sheet.
 */
export function buildComposerSheets(
  input: ComposerSheetInput,
): ComposerSheet[] {
  const sheets: ComposerSheet[] = [];
  if (input.mention && input.mention.candidates.length > 0) {
    sheets.push({ kind: "mention", mention: input.mention });
  }
  const questions = (input.questions ?? []).filter(
    (request) => request.questions.length > 0,
  );
  if (questions.length > 0) {
    sheets.push({ kind: "question", question: questions[0], questions });
  }
  const permissions = input.permissions ?? [];
  if (permissions.length > 0 && permissions[0]) {
    sheets.push({
      kind: "permission",
      permission: permissions[0],
      permissions,
    });
  }
  const queue = input.queue ?? [];
  const scheduled = [...(input.scheduled ?? [])].sort(
    (a, b) =>
      Date.parse(a.send_at) - Date.parse(b.send_at) ||
      Date.parse(a.created_at) - Date.parse(b.created_at),
  );
  if (queue.length > 0 || scheduled.length > 0) {
    sheets.push({ kind: "queue", queue, scheduled });
  }
  if (input.notice) {
    sheets.push({ kind: "notice", notice: input.notice });
  }
  if (input.processesOpen) {
    sheets.push({ kind: "processes" });
  }
  if (input.contextOpen && input.context) {
    sheets.push({ kind: "context", context: input.context });
  }
  if ((input.todos ?? []).length > 0) {
    sheets.push({ kind: "todos", todos: input.todos });
  }
  sheets.sort((a, b) => SHEET_ORDER[a.kind] - SHEET_ORDER[b.kind]);
  return sheets;
}

/** Letter label for option rows (`A`, `B`, …, `Z`, `AA`, …). */
export function optionLetter(index: number): string {
  let label = "";
  let n = index;
  do {
    label = String.fromCharCode(65 + (n % 26)) + label;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return label;
}
