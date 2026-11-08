import { describe, expect, it } from "vitest";

import type { HarnessQuestionRequest } from "../src/harness";
import {
  QUESTION_MULTI_SELECT_SEPARATOR as NUL,
  collectQuestionAnswers,
  emptyQuestionDraft,
  isQuestionCustomActive,
  setQuestionCustomAnswer,
  toggleQuestionOption,
} from "../src/harnessQuestion";

const request: HarnessQuestionRequest = {
  request_id: "q1",
  session_id: "session-1",
  workspace_id: "ws-1",
  questions: [
    {
      question: "Which outputs?",
      multiple: true,
      options: [{ label: "Text" }, { label: "Images" }],
    },
    {
      question: "Which branch?",
      options: [{ label: "main" }, { label: "dev" }],
    },
    { question: "Anything else?" },
  ],
};

describe("question drafts", () => {
  it("submits multi-select picks and free text NUL-joined", () => {
    let draft = emptyQuestionDraft(request);
    draft = toggleQuestionOption(draft, request, 0, "Text");
    draft = toggleQuestionOption(draft, request, 0, "Images");
    draft = setQuestionCustomAnswer(draft, request, 0, " Audio ");
    draft = toggleQuestionOption(draft, request, 1, "dev");
    draft = setQuestionCustomAnswer(draft, request, 2, "No");

    expect(collectQuestionAnswers(request, draft)).toEqual([
      `Text${NUL}Images${NUL}Audio`,
      "dev",
      "No",
    ]);
  });

  it("toggles a multi-select option off again", () => {
    let draft = emptyQuestionDraft(request);
    draft = toggleQuestionOption(draft, request, 0, "Text");
    draft = toggleQuestionOption(draft, request, 0, "Text");

    expect(collectQuestionAnswers(request, draft)[0]).toBe("");
  });

  it("lets free text and a single-select pick replace each other", () => {
    const empty = emptyQuestionDraft(request);
    const picked = toggleQuestionOption(empty, request, 1, "main");
    const typed = setQuestionCustomAnswer(picked, request, 1, "release");

    expect(collectQuestionAnswers(request, typed)[1]).toBe("release");
    expect(isQuestionCustomActive(typed, request, 1)).toBe(true);

    const repicked = toggleQuestionOption(typed, request, 1, "dev");
    expect(collectQuestionAnswers(request, repicked)[1]).toBe("dev");
    expect(isQuestionCustomActive(repicked, request, 1)).toBe(false);
    // Drafts are values: earlier steps are never mutated.
    expect(collectQuestionAnswers(request, empty)).toEqual(["", "", ""]);
  });

  it("never treats a free-text-only answer as a custom option", () => {
    const draft = setQuestionCustomAnswer(
      emptyQuestionDraft(request),
      request,
      2,
      "Yes",
    );
    expect(isQuestionCustomActive(draft, request, 2)).toBe(false);
  });
});
