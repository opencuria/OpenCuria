import { describe, expect, it, vi } from "vitest";
import {
  errorRows,
  loadErrorState,
  showsEmptyAction,
  visiblePrimaryAction,
} from "./formStates";

const create = { title: "New workspace", onPress: () => undefined };
const empty = {
  title: "No workspaces yet",
  symbol: "server.rack" as const,
  action: { title: "New workspace", onPress: () => undefined },
};

describe("visiblePrimaryAction", () => {
  it("hides the main action while the empty state offers the same one", () => {
    expect(
      visiblePrimaryAction(create, { sections: [], empty }),
    ).toBeUndefined();
  });

  it("keeps it while loading, with content, or for a different empty action", () => {
    expect(
      visiblePrimaryAction(create, { sections: [], empty, loading: true }),
    ).toBe(create);
    expect(
      visiblePrimaryAction(create, {
        sections: [{ id: "running", rows: [] }],
        empty,
      }),
    ).toBe(create);
    const failed = loadErrorState("workspaces", "Offline", () => undefined);
    expect(visiblePrimaryAction(create, { sections: [], empty: failed })).toBe(
      create,
    );
    expect(visiblePrimaryAction(create, undefined)).toBe(create);
  });
});

describe("form states", () => {
  it("describes a load failure with Retry", () => {
    const retry = vi.fn();
    const state = loadErrorState("apps", "Offline", retry);
    expect(state).toMatchObject({
      title: "Couldn’t load apps",
      description: "Offline",
      symbol: "exclamationmark.triangle",
      action: { title: "Retry", symbol: "arrow.clockwise" },
    });
    state.action?.onPress();
    expect(retry).toHaveBeenCalledTimes(1);
    expect(showsEmptyAction({ sections: [], empty: state })).toBe(true);
  });

  it("puts an inline error before its Retry button", () => {
    const rows = errorRows("runs", "Server error", () => undefined, true);
    expect(rows.map((row) => `${row.kind}:${row.id}`)).toEqual([
      "note:runs-error",
      "button:runs-retry",
    ]);
    expect(rows[1]).toMatchObject({ title: "Retry", busy: true });
  });
});
