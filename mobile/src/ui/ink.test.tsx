import React from "react";
import { DynamicColorIOS, Text } from "react-native";
import { render, screen } from "@testing-library/react-native";
import { isGlassEffectAPIAvailable } from "expo-glass-effect";
import { Check } from "lucide-react-native";
import { ComposerActionContent } from "./ComposerAction";
import { ComposerSurface } from "./ComposerSurface.ios";
import { useInk } from "./ink";
import { ThemeProvider, getTheme } from "./theme";

jest.unmock("./ComposerSurface.ios");
jest.mock("expo-glass-effect", () => ({
  isGlassEffectAPIAvailable: jest.fn(() => true),
  GlassView: ({ children }: React.PropsWithChildren) => {
    const { View: GlassView } = require("react-native");
    return <GlassView testID="glass">{children}</GlassView>;
  },
}));
jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

const light = getTheme("light");
const dark = getTheme("dark");

function Probe(): React.JSX.Element {
  const ink = useInk();
  return (
    <Text testID="probe" style={{ color: ink.text }}>
      {ink.scheme ?? "follows the surface"}
    </Text>
  );
}

describe("useInk", () => {
  it("is the theme's ink off glass", () => {
    render(
      <ThemeProvider initialMode="dark">
        <Probe />
      </ThemeProvider>,
    );
    expect(screen.getByTestId("probe")).toHaveStyle({ color: dark.text });
    expect(screen.getByText("dark")).toBeTruthy();
  });

  it("follows the glass's light or dark look on the composer's glass", () => {
    render(
      <ThemeProvider initialMode="dark">
        <ComposerSurface>
          <Probe />
          <ComposerActionContent icon={Check} title="Done" primary />
        </ComposerSurface>
      </ThemeProvider>,
    );
    expect(screen.getByTestId("probe")).toHaveStyle({
      color: DynamicColorIOS({ light: light.text, dark: dark.text }),
    });
    // SwiftUI hosts are not forced to the theme's scheme.
    expect(screen.getByText("follows the surface")).toBeTruthy();
    expect(screen.getByText("Done")).toHaveStyle({
      color: DynamicColorIOS({
        light: light.background,
        dark: dark.background,
      }),
    });
  });

  it("keeps the theme's ink on the opaque surface before iOS 26", () => {
    jest.mocked(isGlassEffectAPIAvailable).mockReturnValueOnce(false);
    const reactNative = require("react-native");
    // The surface reads glass support once, on import: load it again, with
    // the theme and ink it shares a context with, but the same React.
    jest.isolateModules(() => {
      jest.doMock("react", () => React);
      jest.doMock("react-native", () => reactNative);
      const theme = require("./theme") as typeof import("./theme");
      const ink = require("./ink") as typeof import("./ink");
      const surface =
        require("./ComposerSurface.ios") as typeof import("./ComposerSurface.ios");
      function IsolatedProbe(): React.JSX.Element {
        const colors = ink.useInk();
        return (
          <Text testID="probe" style={{ color: colors.text }}>
            {colors.scheme}
          </Text>
        );
      }
      render(
        <theme.ThemeProvider initialMode="light">
          <surface.ComposerSurface>
            <IsolatedProbe />
          </surface.ComposerSurface>
        </theme.ThemeProvider>,
      );
    });
    expect(screen.getByTestId("probe")).toHaveStyle({ color: light.text });
    expect(screen.getByText("light")).toBeTruthy();
  });
});
