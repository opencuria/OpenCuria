import { describe, expect, it } from "vitest";
import {
  applyDisplayEdit,
  composerDisplay,
  displayOffset,
  draftOffset,
  draftSelection,
  shortFileName,
} from "./composerDisplay";

const FILE = "@file:/workspace/src/main.ts";
const draft = `Read ${FILE} now`;

describe("composer display", () => {
  it("shows file mentions by name and agents as typed", () => {
    expect(composerDisplay(draft).text).toBe("Read main.ts now");
    expect(composerDisplay("ask @agent:explore please").text).toBe(
      "ask @agent:explore please",
    );
    expect(composerDisplay("").text).toBe("");
    // Incomplete tokens stay text until they are mentions.
    expect(composerDisplay("@file:").text).toBe("@file:");
  });

  it("shortens long file names and keeps their extension", () => {
    expect(shortFileName("main.ts")).toBe("main.ts");
    expect(shortFileName("page-2026-09-25T15-33-36-617Z.png")).toBe(
      "page-2026-09-25…617Z.png",
    );
    expect(shortFileName("page-2026-09-25T15-33-36-617Z.png")).toHaveLength(24);
    expect(shortFileName("a-very-long-name-without-any-extension")).toBe(
      "a-very-long-name-wi…sion",
    );
    expect(
      composerDisplay("see @file:/workspace/page-2026-09-25T15-33-36-617Z.png")
        .text,
    ).toBe("see page-2026-09-25…617Z.png");
  });

  it("maps offsets between the draft and the shown text", () => {
    const display = composerDisplay(draft);
    // "Read |main.ts| now"
    expect(displayOffset(display, 5)).toBe(5);
    expect(displayOffset(display, 5 + FILE.length)).toBe(12);
    expect(displayOffset(display, 10)).toBe(12);
    expect(displayOffset(display, draft.length)).toBe(16);
    expect(draftOffset(display, 5)).toBe(5);
    expect(draftOffset(display, 12)).toBe(5 + FILE.length);
    expect(draftOffset(display, 16)).toBe(draft.length);
    // A caret inside the name snaps to the nearer edge, or to the bias.
    expect(draftOffset(display, 6)).toBe(5);
    expect(draftOffset(display, 11)).toBe(5 + FILE.length);
    expect(draftOffset(display, 11, "start")).toBe(5);
    expect(draftSelection(display, { start: 6, end: 6 })).toEqual({
      start: 5,
      end: 5,
    });
    expect(draftSelection(display, { start: 6, end: 14 })).toEqual({
      start: 5,
      end: 5 + FILE.length + 2,
    });
  });

  it("applies typing around mentions to the draft", () => {
    expect(applyDisplayEdit(draft, "Read main.ts now!")).toEqual({
      draft: `${draft}!`,
      cursor: draft.length + 1,
    });
    expect(applyDisplayEdit(draft, "Please read main.ts now").draft).toBe(
      `Please read ${FILE} now`,
    );
    // Repeated characters are placed at the caret.
    expect(applyDisplayEdit("aa", "aaa", 1)).toEqual({
      draft: "aaa",
      cursor: 2,
    });
    // Paste of a raw token becomes a mention.
    const pasted = applyDisplayEdit("see ", `see ${FILE} `);
    expect(pasted.draft).toBe(`see ${FILE} `);
    expect(composerDisplay(pasted.draft).text).toBe("see main.ts ");
  });

  it("deletes a mention whole when an edit reaches into its name", () => {
    // Backspace after the name.
    expect(applyDisplayEdit(draft, "Read main.t now")).toEqual({
      draft: "Read  now",
      cursor: 5,
    });
    // A selection from the text into the name.
    expect(applyDisplayEdit(draft, "Rea.ts now").draft).toBe("Rea now");
    expect(applyDisplayEdit(draft, "Read ").draft).toBe("Read ");
  });

  it("never lets typed text change or break a mention", () => {
    // Typed against the end of the name: a separator keeps the path.
    expect(applyDisplayEdit(`${FILE}`, "main.tsx")).toEqual({
      draft: `${FILE} x`,
      cursor: FILE.length + 2,
    });
    // Punctuation that ends a mention stays attached.
    expect(applyDisplayEdit(`${FILE}`, "main.ts,").draft).toBe(`${FILE},`);
    // Typed against the start of a mention.
    expect(applyDisplayEdit(`${FILE} now`, "Xmain.ts now")).toEqual({
      draft: `X ${FILE} now`,
      cursor: 1,
    });
    // Typed inside the name lands at its nearer edge.
    expect(applyDisplayEdit(draft, "Read maXin.ts now").draft).toBe(
      `Read X ${FILE} now`,
    );
    expect(applyDisplayEdit("@agent:build", "@agent:buildx").draft).toBe(
      "@agent:build x",
    );
  });
});
