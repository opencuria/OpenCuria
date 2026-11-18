import React from "react";
import { render, screen } from "@testing-library/react-native";
import { Banner } from "./primitives";
import { ThemeProvider, getTheme } from "./theme";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

describe("Banner", () => {
  it("draws a notice without a tone in ink, not a status color", () => {
    render(
      <ThemeProvider initialMode="light">
        <Banner title="Run failed" message="The agent stopped." />
      </ThemeProvider>,
    );
    expect(screen.getByText("Run failed")).toHaveStyle({
      color: getTheme("light").text,
    });
  });

  it("keeps the danger color for errors", () => {
    render(
      <ThemeProvider initialMode="light">
        <Banner tone="error" title="Sign-in failed" message="Wrong password." />
      </ThemeProvider>,
    );
    expect(screen.getByText("Sign-in failed")).toHaveStyle({
      color: getTheme("light").danger,
    });
  });
});
