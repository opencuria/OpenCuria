import React from "react";
import { Text } from "react-native";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { WorkspaceToolsSheet } from "./WorkspaceToolsSheet";
import type { WorkspaceToolProps } from "./types";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

let mockSegmentChange: ((index: number) => void) | null = null;
jest.mock("@expo/ui/community/segmented-control", () => ({
  SegmentedControl: (props: {
    values: string[];
    selectedIndex: number;
    onChange: (event: {
      nativeEvent: { selectedSegmentIndex: number };
    }) => void;
  }) => {
    mockSegmentChange = (index) =>
      props.onChange({ nativeEvent: { selectedSegmentIndex: index } });
    const { Text: NativeText } = require("react-native");
    return (
      <NativeText testID="sections">{`${props.values.join("|")}@${props.selectedIndex}`}</NativeText>
    );
  },
}));

const mockMounts: string[] = [];
const mockFullscreen = new Map<string, (fullscreen: boolean) => void>();
function mockPanel(name: string) {
  return function Panel({
    active,
    workspaceName,
    onFullscreenChange,
  }: WorkspaceToolProps) {
    React.useEffect(() => {
      mockMounts.push(name);
    }, []);
    if (onFullscreenChange) mockFullscreen.set(name, onFullscreenChange);
    return (
      <Text
        testID={`panel-${name}`}
      >{`${name}:${active ? "active" : "hidden"}:${workspaceName}`}</Text>
    );
  };
}
jest.mock("./panels", () => ({
  TOOL_PANELS: {
    git: mockPanel("git"),
    desktop: mockPanel("desktop"),
    terminal: mockPanel("terminal"),
    files: mockPanel("files"),
  },
}));

function renderSheet(visible = true, onClose = jest.fn()) {
  return render(
    <ThemeProvider>
      <WorkspaceToolsSheet
        visible={visible}
        onClose={onClose}
        client={{} as OpenCuriaClient}
        workspaceId="ws"
        workspaceName="demo-app"
        media={null}
      />
    </ThemeProvider>,
  );
}

beforeEach(() => {
  mockMounts.length = 0;
  mockFullscreen.clear();
  mockSegmentChange = null;
});

it("opens on Git with the sections trailing and mounts others on first use", () => {
  renderSheet();
  expect(screen.getByTestId("sections").props.children).toBe(
    "Git|Desktop|Terminal|Files@0",
  );
  expect(screen.getByTestId("panel-git").props.children).toBe(
    "git:active:demo-app",
  );
  expect(screen.queryByTestId("panel-desktop")).toBeNull();
  expect(screen.queryByTestId("panel-terminal")).toBeNull();

  React.act(() => mockSegmentChange?.(2));
  expect(screen.getByTestId("panel-terminal").props.children).toBe(
    "terminal:active:demo-app",
  );
  // The desktop between Git and Terminal starts only once it is shown.
  expect(screen.queryByTestId("panel-desktop")).toBeNull();
  // Git stays mounted (and keeps its state) while another section shows.
  // Git stays mounted (and keeps its state), hidden from assistive technology.
  expect(screen.queryByTestId("panel-git")).toBeNull();
  expect(
    screen.getByTestId("panel-git", { includeHiddenElements: true }).props
      .children,
  ).toBe("git:hidden:demo-app");
  expect(mockMounts).toEqual(["git", "terminal"]);
});

it("hides its header while a panel is full screen, without remounting it", () => {
  renderSheet();
  React.act(() => mockSegmentChange?.(1));
  expect(screen.getByTestId("workspace-tools-header")).toBeTruthy();

  React.act(() => mockFullscreen.get("desktop")?.(true));
  expect(screen.queryByTestId("workspace-tools-header")).toBeNull();
  expect(screen.getByTestId("panel-desktop").props.children).toBe(
    "desktop:active:demo-app",
  );

  React.act(() => mockFullscreen.get("desktop")?.(false));
  expect(screen.getByTestId("workspace-tools-header")).toBeTruthy();
  expect(mockMounts).toEqual(["git", "desktop"]);
});

it("closes from the leading close button and renders nothing while hidden", () => {
  const onClose = jest.fn();
  const { rerender } = renderSheet(true, onClose);
  fireEvent.press(screen.getByTestId("workspace-tools-close"));
  expect(onClose).toHaveBeenCalled();
  rerender(
    <ThemeProvider>
      <WorkspaceToolsSheet
        visible={false}
        onClose={onClose}
        client={{} as OpenCuriaClient}
        workspaceId="ws"
        workspaceName="demo-app"
        media={null}
      />
    </ThemeProvider>,
  );
  expect(screen.queryByTestId("workspace-tools-sheet")).toBeNull();
});
