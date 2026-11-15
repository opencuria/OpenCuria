import React from "react";
import { act, render, screen } from "@testing-library/react-native";
import { ThemeProvider } from "../../ui/theme";
import { hasNativePdfView, PdfPreview } from "./PdfPreview";

type NativeProps = Record<string, unknown> & {
  onLoad?: (event: { nativeEvent: { pageCount: number } }) => void;
  onError?: (event: { nativeEvent: { message: string } }) => void;
  onPageChange?: (event: {
    nativeEvent: { page: number; pageCount: number };
  }) => void;
};

let nativeProps: NativeProps = {};

jest.mock("expo", () => ({
  requireNativeView: () => (props: NativeProps) => {
    const { View } = require("react-native") as typeof import("react-native");
    nativeProps = props;
    return <View testID={props.testID as string} />;
  },
}));

const expoGlobal = globalThis as { expo?: unknown };
const originalExpo = expoGlobal.expo;

function preview(name = "report.pdf", onError = jest.fn()) {
  render(
    <ThemeProvider>
      <PdfPreview
        uri={`file:///${name}`}
        name={name}
        bottomInset={0}
        onError={onError}
      />
    </ThemeProvider>,
  );
}

afterAll(() => {
  expoGlobal.expo = originalExpo;
});

// The resolved native view is cached, so the missing-view case runs first.
it("renders nothing in app builds without the native view", () => {
  expoGlobal.expo = undefined;
  expect(hasNativePdfView()).toBe(false);
  preview();
  expect(screen.queryByTestId("file-pdf")).toBeNull();
});

it("treats a failing native view lookup as a missing view", () => {
  expoGlobal.expo = {
    getViewConfig: () => {
      throw new Error("Method not implemented.");
    },
  };
  expect(hasNativePdfView()).toBe(false);
});

describe("with the native view", () => {
  beforeAll(() => {
    expoGlobal.expo = { getViewConfig: () => ({}) };
  });

  it("shows the document, counts pages and reports errors", () => {
    const onError = jest.fn();
    expect(hasNativePdfView()).toBe(true);
    preview("report.pdf", onError);
    expect(screen.getByTestId("file-pdf")).toBeTruthy();
    expect(nativeProps.source).toBe("file:///report.pdf");
    act(() => nativeProps.onLoad?.({ nativeEvent: { pageCount: 3 } }));
    act(() =>
      nativeProps.onPageChange?.({ nativeEvent: { page: 2, pageCount: 3 } }),
    );
    expect(screen.getByText("2 of 3")).toBeTruthy();
    act(() =>
      nativeProps.onError?.({ nativeEvent: { message: "Can't open" } }),
    );
    expect(onError).toHaveBeenCalledWith("Can't open");
  });

  it("shows no page counter for a single page", () => {
    preview("card.pdf");
    act(() =>
      nativeProps.onPageChange?.({ nativeEvent: { page: 1, pageCount: 1 } }),
    );
    expect(screen.queryByText("1 of 1")).toBeNull();
  });
});
