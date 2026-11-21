import fs from "fs";
import path from "path";

// Metro bundles the XML files; Jest cannot load them, so read the mapping.
const source = fs.readFileSync(path.join(__dirname, "androidIcons.ts"), "utf8");
const icons = new Map(
  [...source.matchAll(/^\s*"?([a-z0-9.]+)"?:\s*require\("([^"]+)"\)/gm)].map(
    (match) => [match[1]!, path.join(__dirname, match[2]!)],
  ),
);

describe("androidIcons", () => {
  it("maps the symbols of the sheets and settings", () => {
    const symbols = [
      "exclamationmark.triangle",
      "wifi.slash",
      "arrow.clockwise",
      "info.circle",
      "checkmark.circle.fill",
      "xmark.circle.fill",
      "minus.circle.fill",
      "clock",
      "square.grid.2x2.fill",
      "calendar.badge.clock",
      "magnifyingglass",
      "plus",
      "pause.fill",
      "play.fill",
      "checkmark",
      "server.rack",
    ];
    expect(symbols.filter((symbol) => !icons.has(symbol))).toEqual([]);
  });

  it("points at vectors that Compose can draw", () => {
    for (const [symbol, file] of icons) {
      expect({ symbol, exists: fs.existsSync(file) }).toEqual({
        symbol,
        exists: true,
      });
      // @expo/ui ignores <group> transforms: paths must be absolute.
      expect({
        symbol,
        transformed: /<group[^>]*(translate|scale|rotation|pivot)/.test(
          fs.readFileSync(file, "utf8"),
        ),
      }).toEqual({ symbol, transformed: false });
    }
  });
});
