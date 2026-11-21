import { describe, expect, it, vi } from "vitest";
import { refreshError } from "./sections";

describe("refreshError", () => {
  const reload = vi.fn(async () => undefined);

  it("shows the reason and Retry while older data is shown", () => {
    const [section] = refreshError("runners", {
      data: [],
      error: "Server unavailable",
      loading: false,
      reload,
    });
    expect(section?.rows.map((row) => `${row.kind}:${row.id}`)).toEqual([
      "note:runners-error",
      "button:runners-retry",
    ]);
    const retry = section?.rows[1];
    if (retry?.kind === "button") retry.onPress();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("adds nothing without data or without an error", () => {
    expect(
      refreshError("runners", {
        data: undefined,
        error: "Server unavailable",
        loading: false,
        reload,
      }),
    ).toEqual([]);
    expect(
      refreshError("runners", {
        data: [],
        error: null,
        loading: false,
        reload,
      }),
    ).toEqual([]);
  });
});
