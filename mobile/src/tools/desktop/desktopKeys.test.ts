import { describe, expect, it } from "vitest";
import {
  DESKTOP_ACCESSORY_KEYS,
  DESKTOP_KEYS,
  KEYBOARD_SENTINEL,
  diffKeyboardInput,
  modifiersOf,
  normalizeTypedText,
} from "./desktopKeys";

describe("hidden keyboard input", () => {
  it("types characters appended at the caret", () => {
    expect(diffKeyboardInput(`${KEYBOARD_SENTINEL}a`)).toEqual([
      { kind: "text", text: "a" },
    ]);
    // Fast typing delivers several characters in one change.
    expect(
      diffKeyboardInput(`${KEYBOARD_SENTINEL}abc`, `${KEYBOARD_SENTINEL}a`),
    ).toEqual([{ kind: "text", text: "bc" }]);
  });

  it("turns removed characters into backspaces", () => {
    expect(
      diffKeyboardInput(KEYBOARD_SENTINEL.slice(2), KEYBOARD_SENTINEL),
    ).toEqual([{ kind: "key", key: DESKTOP_KEYS.backspace, count: 2 }]);
    expect(diffKeyboardInput("", "ab")).toEqual([
      { kind: "key", key: DESKTOP_KEYS.backspace, count: 2 },
    ]);
  });

  it("replays an autocorrect replacement as backspaces plus the new word", () => {
    expect(diffKeyboardInput("  teh", "  tje")).toEqual([
      { kind: "key", key: DESKTOP_KEYS.backspace, count: 2 },
      { kind: "text", text: "eh" },
    ]);
  });

  it("sends newlines as Enter and ignores unchanged text", () => {
    expect(diffKeyboardInput("  ls\nx", "  ")).toEqual([
      { kind: "text", text: "ls" },
      { kind: "key", key: DESKTOP_KEYS.enter, count: 1 },
      { kind: "text", text: "x" },
    ]);
    expect(diffKeyboardInput("  a", "  a")).toEqual([]);
  });

  it("undoes smart punctuation of mobile keyboards", () => {
    expect(normalizeTypedText("“hi” it’s — ok…")).toBe('"hi" it\'s -- ok...');
    expect(diffKeyboardInput("  “x", "  ")).toEqual([
      { kind: "text", text: '"x' },
    ]);
  });
});

describe("accessory keys", () => {
  it("offers the keys phones lack and the Linux modifiers", () => {
    const ids = DESKTOP_ACCESSORY_KEYS.map((entry) => entry.id);
    expect(ids).toEqual(
      expect.arrayContaining([
        "esc",
        "ctrl",
        "alt",
        "super",
        "tab",
        "left",
        "right",
      ]),
    );
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("maps sticky modifiers to the page's flags", () => {
    expect(modifiersOf(new Set(["ctrl", "meta"]))).toEqual({
      ctrl: true,
      alt: false,
      shift: false,
      meta: true,
    });
  });
});
