import { sheetGeometry, sheetHiddenBottom } from "./CompanionSheet";

describe("sheetHiddenBottom", () => {
  // An iPhone with a 59 pt top inset: full 785 pt, half 511 pt.
  const geometry = sheetGeometry(852, 59);
  const restHalf = geometry.full - geometry.half;

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
