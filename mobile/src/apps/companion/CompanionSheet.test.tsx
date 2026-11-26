import React from "react";
import { Dimensions, Text } from "react-native";
import { act, cleanup, render, screen } from "@testing-library/react-native";
import { State } from "react-native-gesture-handler";
import {
  fireGestureHandler,
  getByGestureTestId,
} from "react-native-gesture-handler/jest-utils";
import { getAnimatedStyle } from "react-native-reanimated";
import { ThemeProvider } from "../../ui/theme";
import {
  CompanionSheet,
  sheetGeometry,
  sheetHiddenBottom,
  sheetReleaseTarget,
  type SheetDetent,
} from "./CompanionSheet";

// An iPhone with a 59 pt top inset: full 785 pt, half 511 pt.
const geometry = sheetGeometry(852, 59);
const restHalf = geometry.full - geometry.half;

describe("sheetHiddenBottom", () => {
  it("keeps the composer on the screen's edge between half and full height", () => {
    expect(geometry).toEqual({ full: 785, half: 511 });
    expect(sheetHiddenBottom(0, geometry)).toBe(0);
    expect(sheetHiddenBottom(restHalf, geometry)).toBe(restHalf);
    // Halfway up, the content reaches exactly as far below the screen as
    // the sheet still has to rise, so the composer does not move.
    expect(sheetHiddenBottom(137, geometry)).toBe(137);
  });

  it("lets the composer leave with the sheet below half height", () => {
    expect(sheetHiddenBottom(restHalf + 120, geometry)).toBe(restHalf);
    expect(sheetHiddenBottom(geometry.full + 24, geometry)).toBe(restHalf);
  });

  it("holds the composer down while the sheet gives above full height", () => {
    expect(sheetHiddenBottom(-8, geometry)).toBe(-8);
  });
});

describe("sheetReleaseTarget", () => {
  it("moves one detent after a short drag or a flick", () => {
    expect(sheetReleaseTarget(restHalf, restHalf - 80, 0, geometry)).toBe(
      "full",
    );
    expect(sheetReleaseTarget(restHalf, restHalf - 20, -900, geometry)).toBe(
      "full",
    );
    expect(sheetReleaseTarget(0, 80, 0, geometry)).toBe("half");
    expect(sheetReleaseTarget(0, 20, 900, geometry)).toBe("half");
  });

  it("returns to its detent after a slow, short drag", () => {
    expect(sheetReleaseTarget(restHalf, restHalf - 30, -100, geometry)).toBe(
      "half",
    );
    expect(sheetReleaseTarget(0, 30, 100, geometry)).toBe("full");
  });

  it("leaves from half height, or from full height pulled below half", () => {
    expect(sheetReleaseTarget(restHalf, restHalf + 40, 1500, geometry)).toBe(
      "dismiss",
    );
    expect(sheetReleaseTarget(restHalf, restHalf + 140, 0, geometry)).toBe(
      "dismiss",
    );
    // A flick from full height stops at half height.
    expect(sheetReleaseTarget(0, 60, 1800, geometry)).toBe("half");
    expect(sheetReleaseTarget(0, restHalf + 20, 1800, geometry)).toBe(
      "dismiss",
    );
  });
});

describe("CompanionSheet", () => {
  // The test window, without insets.
  const live = sheetGeometry(Dimensions.get("window").height, 0);
  const liveHalf = live.full - live.half;
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  function renderSheet(
    props: Partial<React.ComponentProps<typeof CompanionSheet>> = {},
  ) {
    const handlers = {
      onDetentChange: jest.fn<void, [SheetDetent]>(),
      onDismiss: jest.fn(),
      onHidden: jest.fn(),
    };
    const sheet = (open: boolean, detent: SheetDetent = "half") => (
      <ThemeProvider>
        <CompanionSheet
          open={open}
          detent={detent}
          barHeight={44}
          testID="sheet"
          {...handlers}
          {...props}
        >
          <Text>Chat</Text>
        </CompanionSheet>
      </ThemeProvider>
    );
    const view = render(sheet(true));
    act(() => jest.advanceTimersByTime(600));
    return {
      ...handlers,
      rerender: (open: boolean) => view.rerender(sheet(open)),
      unmount: () => view.unmount(),
    };
  }

  const position = () =>
    (
      getAnimatedStyle(screen.getByTestId("sheet")).transform as Array<{
        translateY: number;
      }>
    )[0]!.translateY;

  const drag = (translationY: number, velocityY: number) =>
    act(() =>
      fireGestureHandler(getByGestureTestId("companion-sheet-drag"), [
        { state: State.BEGAN, translationY: 0, velocityY: 0 },
        { state: State.ACTIVE, translationY: translationY / 2, velocityY },
        { state: State.ACTIVE, translationY, velocityY },
        { state: State.END, translationY, velocityY },
      ]),
    );

  it("rests at half height after opening", () => {
    renderSheet();
    expect(position()).toBeCloseTo(liveHalf, 0);
  });

  it("leaves at the flick's speed before the screen closes it, and reports it once", () => {
    const sheet = renderSheet();
    drag(60, 1500);
    // The very next frames carry it on, without waiting for the screen.
    act(() => jest.advanceTimersByTime(32));
    expect(position()).toBeGreaterThan(liveHalf + 60 + 20);
    expect(sheet.onDismiss).toHaveBeenCalledTimes(1);
    // Closing lets it slide on and reports the end once.
    sheet.rerender(false);
    act(() => jest.advanceTimersByTime(16));
    const leaving = position();
    act(() => jest.advanceTimersByTime(16));
    expect(position()).toBeGreaterThan(leaving);
    act(() => jest.advanceTimersByTime(600));
    expect(position()).toBe(live.full + 24);
    expect(sheet.onHidden).toHaveBeenCalledTimes(1);
  });

  it("keeps the finger's speed when it lets go", () => {
    const firstFrame = (velocityY: number) => {
      const sheet = renderSheet();
      drag(-80, velocityY);
      const released = position();
      act(() => jest.advanceTimersByTime(16));
      const moved = released - position();
      sheet.unmount();
      return moved;
    };
    const slow = firstFrame(0);
    const flick = firstFrame(-1200);
    expect(slow).toBeGreaterThan(0);
    // About 1200 pt/s × 16 ms further, instead of starting from rest.
    expect(flick - slow).toBeGreaterThan(12);
  });

  it("settles on full height after a flick up and reports the detent", () => {
    const sheet = renderSheet();
    drag(-40, -1200);
    act(() => jest.advanceTimersByTime(600));
    expect(sheet.onDetentChange).toHaveBeenCalledWith("full");
    expect(position()).toBeCloseTo(0, 0);
    expect(sheet.onDismiss).not.toHaveBeenCalled();
  });
});
