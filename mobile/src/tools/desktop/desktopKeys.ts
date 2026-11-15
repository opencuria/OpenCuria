import type { DesktopKey, DesktopModifiers } from "./viewerBridge";

export type DesktopModifier = keyof DesktopModifiers;

export type DesktopAccessoryKey =
  | { kind: "key"; id: string; label: string; key: DesktopKey }
  | { kind: "modifier"; id: string; label: string; modifier: DesktopModifier };

const key = (key: string, code: string, keyCode: number): DesktopKey => ({
  key,
  code,
  keyCode,
});

export const DESKTOP_KEYS = {
  enter: key("Enter", "Enter", 13),
  backspace: key("Backspace", "Backspace", 8),
  escape: key("Escape", "Escape", 27),
  tab: key("Tab", "Tab", 9),
  left: key("ArrowLeft", "ArrowLeft", 37),
  up: key("ArrowUp", "ArrowUp", 38),
  right: key("ArrowRight", "ArrowRight", 39),
  down: key("ArrowDown", "ArrowDown", 40),
  delete: key("Delete", "Delete", 46),
  home: key("Home", "Home", 36),
  end: key("End", "End", 35),
  pageUp: key("PageUp", "PageUp", 33),
  pageDown: key("PageDown", "PageDown", 34),
  v: key("v", "KeyV", 86),
} as const;

/**
 * Keys a phone keyboard lacks, in the terminal's order. Workspaces are Linux
 * desktops: ctrl, alt and super are their modifiers.
 */
export const DESKTOP_ACCESSORY_KEYS: DesktopAccessoryKey[] = [
  { kind: "key", id: "esc", label: "esc", key: DESKTOP_KEYS.escape },
  { kind: "modifier", id: "ctrl", label: "ctrl", modifier: "ctrl" },
  { kind: "modifier", id: "alt", label: "alt", modifier: "alt" },
  { kind: "modifier", id: "super", label: "super", modifier: "meta" },
  { kind: "modifier", id: "shift", label: "shift", modifier: "shift" },
  { kind: "key", id: "tab", label: "tab", key: DESKTOP_KEYS.tab },
  { kind: "key", id: "left", label: "←", key: DESKTOP_KEYS.left },
  { kind: "key", id: "up", label: "↑", key: DESKTOP_KEYS.up },
  { kind: "key", id: "down", label: "↓", key: DESKTOP_KEYS.down },
  { kind: "key", id: "right", label: "→", key: DESKTOP_KEYS.right },
  { kind: "key", id: "del", label: "del", key: DESKTOP_KEYS.delete },
  { kind: "key", id: "home", label: "home", key: DESKTOP_KEYS.home },
  { kind: "key", id: "end", label: "end", key: DESKTOP_KEYS.end },
  { kind: "key", id: "pgup", label: "pg up", key: DESKTOP_KEYS.pageUp },
  { kind: "key", id: "pgdn", label: "pg dn", key: DESKTOP_KEYS.pageDown },
];

/**
 * Initial text of the hidden input, so the soft keyboard has something to
 * delete and reports backspaces as text changes. The input starts over with
 * it each time the keyboard opens.
 */
export const KEYBOARD_SENTINEL = " ".repeat(16);

export type KeyboardInputAction =
  | { kind: "text"; text: string }
  | { kind: "key"; key: DesktopKey; count: number };

/** Typographic replacements of mobile keyboards, back to what was typed. */
const SMART_PUNCTUATION: Record<string, string> = {
  "\u201C": '"',
  "\u201D": '"',
  "\u201E": '"',
  "\u2018": "'",
  "\u2019": "'",
  "\u201A": "'",
  "\u2014": "--",
  "\u2026": "...",
};

export function normalizeTypedText(text: string): string {
  return text.replace(
    /[\u201C\u201D\u201E\u2018\u2019\u201A\u2014\u2026]/g,
    (match) => SMART_PUNCTUATION[match] ?? match,
  );
}

/**
 * Translate an edit of the hidden input into desktop input. Soft keyboards
 * edit at the caret, which stays at the end: removed characters become
 * backspaces and inserted ones are typed (an autocorrect replacement becomes
 * backspaces followed by the replacement). `previous` is the text the input
 * held before this edit.
 */
export function diffKeyboardInput(
  next: string,
  previous: string = KEYBOARD_SENTINEL,
): KeyboardInputAction[] {
  if (next === previous) return [];
  let common = 0;
  while (
    common < previous.length &&
    common < next.length &&
    previous[common] === next[common]
  )
    common += 1;
  const actions: KeyboardInputAction[] = [];
  const removed = previous.length - common;
  if (removed > 0)
    actions.push({ kind: "key", key: DESKTOP_KEYS.backspace, count: removed });
  const inserted = normalizeTypedText(next.slice(common));
  if (inserted) {
    // Enter arrives as a newline from multiline inputs.
    const parts = inserted.split("\n");
    parts.forEach((part, index) => {
      if (part) actions.push({ kind: "text", text: part });
      if (index < parts.length - 1)
        actions.push({ kind: "key", key: DESKTOP_KEYS.enter, count: 1 });
    });
  }
  return actions;
}

/** Sticky modifiers as the page expects them. */
export function modifiersOf(
  active: ReadonlySet<DesktopModifier>,
): DesktopModifiers {
  return {
    ctrl: active.has("ctrl"),
    alt: active.has("alt"),
    shift: active.has("shift"),
    meta: active.has("meta"),
  };
}
