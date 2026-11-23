import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { ThemeProvider } from "../../ui/theme";
import type { ToolDetailSheetProps } from "../ToolDetailSheet";
import { GitDiffSheet, type GitDiffTarget } from "./GitDiffSheet";
import { MAX_DIFF_LINES } from "./lineDiff";

jest.mock("react-native-svg", () => {
  const { View } = require("react-native") as typeof import("react-native");
  return {
    SvgXml: ({ xml }: { xml: string }) => (
      <View testID="svg-preview" accessibilityLabel={xml} />
    ),
  };
});
jest.mock("../ToolDetailSheet", () => {
  const { View, Pressable } =
    require("react-native") as typeof import("react-native");
  return {
    ToolDetailSheet: ({
      children,
      actions,
      onAction,
      testID,
    }: ToolDetailSheetProps) => (
      <View testID={testID}>
        {actions?.map((action) => (
          <Pressable
            key={action.id}
            accessibilityRole="button"
            accessibilityLabel={action.title}
            accessibilityState={{ selected: Boolean(action.selected) }}
            onPress={() => onAction?.(action.id)}
          />
        ))}
        {children}
      </View>
    ),
  };
});

const release = jest.fn();
const cached = { uri: "file:///cache/logo.png", release };

beforeEach(() => release.mockClear());

function open(patch: Partial<GitDiffTarget>) {
  const target: GitDiffTarget = {
    path: "src/a.ts",
    label: "Working tree",
    load: jest.fn(async () => ({ original: "a\n", modified: "b\n" })),
    loadImage: jest.fn(async () => cached),
    ...patch,
  };
  const view = render(
    <ThemeProvider>
      <GitDiffSheet target={target} onClose={() => undefined} />
    </ThemeProvider>,
  );
  return { target, ...view };
}

it("shows the cached new version of an image until the sheet closes", async () => {
  const { target, unmount } = open({ path: "assets/logo.png" });
  const image = await screen.findByLabelText("logo.png");
  expect(image.props.source).toEqual([
    expect.objectContaining({ uri: "file:///cache/logo.png" }),
  ]);
  expect(target.load).not.toHaveBeenCalled();
  expect(screen.queryByText(/UTF-8/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Word wrap" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Source" })).toBeNull();
  expect(release).not.toHaveBeenCalled();
  unmount();
  expect(release).toHaveBeenCalledTimes(1);
});

it("releases an image that arrives after the sheet closed", async () => {
  let resolve!: (value: typeof cached) => void;
  const { unmount } = open({
    path: "late.webp",
    loadImage: () => new Promise((done) => (resolve = done)),
  });
  unmount();
  await act(async () => resolve(cached));
  expect(release).toHaveBeenCalledTimes(1);
});

it("says when the change deletes an image", async () => {
  open({ path: "photo.JPG", loadImage: jest.fn(async () => null) });
  expect(await screen.findByText("This image was deleted.")).toBeTruthy();
});

it("reports image load failures with a retry", async () => {
  const loadImage = jest
    .fn()
    .mockRejectedValueOnce(new Error("Files up to 500 MB can be transferred."))
    .mockResolvedValueOnce(cached);
  open({ path: "big.gif", loadImage });
  expect(
    await screen.findByText("Files up to 500 MB can be transferred."),
  ).toBeTruthy();
  fireEvent.press(screen.getByText("Try again"));
  expect(await screen.findByLabelText("big.gif")).toBeTruthy();
  expect(loadImage).toHaveBeenCalledTimes(2);
});

it("previews an SVG and switches to the textual diff as Source", async () => {
  open({
    path: "icon.svg",
    load: jest.fn(async () => ({
      original: '<svg><circle r="1"/></svg>\n',
      modified: '<svg><circle r="2"/></svg>\n',
    })),
  });
  expect(
    (await screen.findByTestId("svg-preview")).props.accessibilityLabel,
  ).toBe('<svg><circle r="2"/></svg>\n');
  expect(screen.queryByTestId("git-diff-rows")).toBeNull();
  expect(screen.queryByRole("button", { name: "Word wrap" })).toBeNull();

  fireEvent.press(screen.getByRole("button", { name: "Source" }));
  expect(screen.getByTestId("git-diff-rows")).toBeTruthy();
  expect(screen.queryByTestId("svg-preview")).toBeNull();
  expect(screen.getByRole("button", { name: "Word wrap" })).toBeTruthy();

  fireEvent.press(screen.getByRole("button", { name: "Preview" }));
  expect(screen.getByTestId("svg-preview")).toBeTruthy();
});

it("previews an SVG that is too large to diff", async () => {
  const large = `<svg>\n${"<g/>\n".repeat(MAX_DIFF_LINES)}</svg>\n`;
  open({
    path: "map.svg",
    load: jest.fn(async () => ({ original: "", modified: large })),
  });
  expect(await screen.findByTestId("svg-preview")).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Source" }));
  expect(
    screen.getByText("This file is too large to compare on a phone."),
  ).toBeTruthy();
});

it("says when the change deletes an SVG", async () => {
  open({
    path: "old.svg",
    load: jest.fn(async () => ({ original: "<svg/>\n", modified: "" })),
  });
  expect(await screen.findByText("This image was deleted.")).toBeTruthy();
});
