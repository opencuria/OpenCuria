/**
 * Answer rules for pending `question` gates and parsing helpers for answered
 * `question` tool calls.
 *
 * The backend persists the asked questions in `input.arguments.questions`
 * (mirrored into `meta.questions` on live parts) and the user answers as
 * `{"answers": [...]}` in `output` (mirrored into `meta.answers`). Skipped
 * or timed-out questions land as `error` parts whose output carries the
 * failure reason. Extracted from `ToolDetailQuestion.vue` so both the chat
 * card and tests share one implementation.
 */

import type { HarnessPart, HarnessQuestionRequest } from "./harness";
import { parseToolArguments } from "./toolDisplay";

/** Joins multi-select answers; the backend splits submitted answers on NUL. */
export const QUESTION_MULTI_SELECT_SEPARATOR = String.fromCharCode(0);

/**
 * Unsent answers of one pending question request, one entry per question.
 * `answers` holds picked option labels (multi-select joined by
 * `QUESTION_MULTI_SELECT_SEPARATOR`); `custom` holds the free-text answer,
 * available even when the question offers options. Helpers return new drafts.
 */
export interface QuestionDraft {
  answers: string[];
  custom: string[];
}

export function emptyQuestionDraft(
  request: HarnessQuestionRequest,
): QuestionDraft {
  return {
    answers: request.questions.map(() => ""),
    custom: request.questions.map(() => ""),
  };
}

function replaceAt(values: string[], index: number, value: string): string[] {
  const next = [...values];
  next[index] = value;
  return next;
}

export function selectedQuestionOptions(
  draft: QuestionDraft,
  questionIndex: number,
): string[] {
  const raw = draft.answers[questionIndex] ?? "";
  return raw ? raw.split(QUESTION_MULTI_SELECT_SEPARATOR).filter(Boolean) : [];
}

/** Multi-select toggles the label; single-select picks it and clears free text. */
export function toggleQuestionOption(
  draft: QuestionDraft,
  request: HarnessQuestionRequest,
  questionIndex: number,
  label: string,
): QuestionDraft {
  if (request.questions[questionIndex]?.multiple) {
    const current = selectedQuestionOptions(draft, questionIndex);
    const next = current.includes(label)
      ? current.filter((item) => item !== label)
      : [...current, label];
    return {
      ...draft,
      answers: replaceAt(
        draft.answers,
        questionIndex,
        next.join(QUESTION_MULTI_SELECT_SEPARATOR),
      ),
    };
  }
  return {
    answers: replaceAt(draft.answers, questionIndex, label),
    custom: replaceAt(draft.custom, questionIndex, ""),
  };
}

/** Free text replaces a single-select pick; multi-select keeps its picks. */
export function setQuestionCustomAnswer(
  draft: QuestionDraft,
  request: HarnessQuestionRequest,
  questionIndex: number,
  value: string,
): QuestionDraft {
  const question = request.questions[questionIndex];
  return {
    answers:
      question?.options?.length && !question.multiple
        ? replaceAt(draft.answers, questionIndex, "")
        : draft.answers,
    custom: replaceAt(draft.custom, questionIndex, value),
  };
}

/** Whether the free-text row counts as a chosen option of a question with options. */
export function isQuestionCustomActive(
  draft: QuestionDraft,
  request: HarnessQuestionRequest,
  questionIndex: number,
): boolean {
  const question = request.questions[questionIndex];
  const custom = (draft.custom[questionIndex] ?? "").trim();
  if (!question?.options?.length || !custom) return false;
  return (
    Boolean(question.multiple) ||
    selectedQuestionOptions(draft, questionIndex).length === 0
  );
}

/** One submitted answer per question, in the backend's NUL-joined format. */
export function collectQuestionAnswers(
  request: HarnessQuestionRequest,
  draft: QuestionDraft,
): string[] {
  return request.questions.map((question, index) => {
    const custom = (draft.custom[index] ?? "").trim();
    if (!question.options?.length) return custom;
    const selected = selectedQuestionOptions(draft, index);
    if (question.multiple)
      return [...selected, ...(custom ? [custom] : [])].join(
        QUESTION_MULTI_SELECT_SEPARATOR,
      );
    return custom || selected[0] || "";
  });
}

export interface HarnessQuestionRow {
  question: string;
  answer: string;
}

function asQuestionList(value: unknown): { question: string }[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const question = (item as { question?: unknown }).question;
    if (typeof question !== "string" || !question) return [];
    return [{ question }];
  });
}

function asAnswerList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) =>
    typeof item === "string" ? item : JSON.stringify(item),
  );
}

/** Pair asked questions with their answers (index-aligned). */
export function parseQuestionRows(part: HarnessPart): HarnessQuestionRow[] {
  const displayRows = part.display?.question_rows;
  if (
    part.detail_loaded === false &&
    Array.isArray(displayRows) &&
    displayRows.length > 0
  ) {
    return displayRows.flatMap((row) =>
      typeof row.question === "string"
        ? [
            {
              question: row.question,
              answer: typeof row.answer === "string" ? row.answer : "",
            },
          ]
        : [],
    );
  }
  const args = parseToolArguments(part);
  const questions = asQuestionList(part.meta?.["questions"] ?? args.questions);
  let answers = asAnswerList(part.meta?.["answers"]);
  if (answers.length === 0 && part.output) {
    try {
      const parsed: unknown = JSON.parse(part.output);
      if (parsed && typeof parsed === "object" && "answers" in parsed) {
        answers = asAnswerList((parsed as { answers: unknown }).answers);
      }
    } catch {
      answers = [];
    }
  }
  if (questions.length === 0 && answers.length === 0) return [];
  if (questions.length === 0) {
    return answers.map((answer) => ({ question: "Answer", answer }));
  }
  return questions.map((item, index) => ({
    question: item.question,
    answer: answers[index] ?? "",
  }));
}

export type QuestionCardTone = "ok" | "skipped" | "failed";

export interface QuestionCardStatus {
  tone: QuestionCardTone;
  /** Badge label; null when answered normally. */
  label: string | null;
  /** Short failure reason for error parts (args suffix stripped). */
  detail: string | null;
}

/** Strip the `Tool 'question' failed: … (args: …)` wrapper for compact display. */
export function cleanQuestionError(output: string): string {
  let text = (output || "").trim();
  text = text.replace(/^Tool 'question' failed:\s*/i, "");
  text = text.replace(/\s*\(args:.*\)\s*$/s, "").trim();
  return text;
}

/** Card status: answered parts show no badge, error parts map to Skip/Timeout/Failed. */
export function questionCardStatus(part: HarnessPart): QuestionCardStatus {
  if (part.state !== "error") {
    return { tone: "ok", label: null, detail: null };
  }
  const detail = cleanQuestionError(part.output);
  const lowered = detail.toLowerCase();
  if (/reject|cancel|skip|abort/.test(lowered)) {
    return { tone: "skipped", label: "Skipped", detail: detail || null };
  }
  if (/timed?\s*out|timeout/.test(lowered)) {
    return { tone: "skipped", label: "Timed out", detail: detail || null };
  }
  return { tone: "failed", label: "Failed", detail: detail || null };
}
