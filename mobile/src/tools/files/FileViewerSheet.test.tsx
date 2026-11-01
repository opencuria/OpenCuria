import React from "react";
import { Alert } from "react-native";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import * as Clipboard from "expo-clipboard";
import * as Sharing from "expo-sharing";
import type { MediaCache } from "../../platform";
import { ThemeProvider } from "../../ui/theme";
import type { ToolDetailSheetProps } from "../ToolDetailSheet";
import { FileViewerSheet, type FileViewerTarget } from "./FileViewerSheet";
import { FilesPanel } from "./FilesPanel";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

jest.mock(
  "remend",
  () => ({ __esModule: true, default: (markdown: string) => markdown }),
  { virtual: true },
);

jest.mock("expo-clipboard", () => ({ setStringAsync: jest.fn() }));
jest.mock("expo-sharing", () => ({
  isAvailableAsync: jest.fn().mockResolvedValue(true),
  shareAsync: jest.fn().mockResolvedValue(undefined),
}));
jest.mock("../ToolDetailSheet", () => {
  const { View, Text, Pressable } =
    require("react-native") as typeof import("react-native");
  return {
    ToolDetailSheet: ({
      children,
      title,
      actions,
      onAction,
      onClose,
      testID,
    }: ToolDetailSheetProps) => (
      <View testID={testID}>
        <Text>{title}</Text>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Close"
          onPress={onClose}
        />
        {actions?.map((action) => (
          <Pressable
            key={action.id}
            accessibilityRole="button"
            accessibilityLabel={action.title}
            onPress={() => onAction?.(action.id)}
          />
        ))}
        {children}
      </View>
    ),
  };
});

function setup(
  file: FileViewerTarget | null = "/workspace/index.ts",
  connected = true,
) {
  const readFile = jest.fn().mockResolvedValue({
    content: btoa("const answer = 42;"),
    size: 18,
  });
  const client = { files: { readFile } } as unknown as OpenCuriaClient;
  const release = jest.fn();
  const media = {
    resolvePinned: jest
      .fn()
      .mockResolvedValue({ uri: "file:///cached-media", release }),
    openFile: jest.fn().mockResolvedValue({
      uri: "file:///download",
      mimeType: "application/pdf",
    }),
    saveAttachment: jest.fn().mockResolvedValue("file:///attachment"),
    shareAttachment: jest.fn().mockResolvedValue(undefined),
  };
  const onInsertIntoChat = jest.fn();
  const onClose = jest.fn();
  const element = (target: FileViewerTarget | null) => (
    <ThemeProvider>
      <FileViewerSheet
        client={client}
        workspaceId="ws"
        media={connected ? (media as unknown as MediaCache) : null}
        file={target}
        bottomInset={0}
        onClose={onClose}
        onInsertIntoChat={onInsertIntoChat}
      />
    </ThemeProvider>
  );
  const tree = render(element(file));
  return {
    ...tree,
    element,
    readFile,
    media,
    release,
    onInsertIntoChat,
    onClose,
  };
}

beforeEach(() => jest.clearAllMocks());

it("opens an explorer file in the same viewer and returns to the file tree", async () => {
  const readFile = jest.fn().mockResolvedValue({
    content: btoa("const answer = 42;"),
    size: 18,
  });
  const client = {
    files: {
      listFiles: jest.fn().mockResolvedValue({
        entries: [{ path: "/workspace/index.ts", type: "file" }],
      }),
      readFile,
    },
  } as unknown as OpenCuriaClient;
  render(
    <ThemeProvider>
      <FilesPanel
        client={client}
        workspaceId="ws"
        workspaceName="demo"
        active
        media={null}
        bottomInset={0}
      />
    </ThemeProvider>,
  );
  fireEvent.press(await screen.findByRole("button", { name: "index.ts" }));
  await screen.findByTestId("file-source");
  expect(readFile).toHaveBeenCalledWith(
    "ws",
    "/workspace/index.ts",
    1024 * 1024,
  );
  fireEvent.press(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByTestId("file-viewer")).toBeNull();
  expect(screen.getByRole("button", { name: "index.ts" })).toBeTruthy();
});

it("shows TypeScript source and offers the explorer's copy and mention actions", async () => {
  const { readFile, media, onInsertIntoChat, onClose } = setup();
  await screen.findByTestId("file-source");
  expect(readFile).toHaveBeenCalledWith(
    "ws",
    "/workspace/index.ts",
    1024 * 1024,
  );
  expect(media.resolvePinned).not.toHaveBeenCalled();
  fireEvent.press(screen.getByRole("button", { name: "Copy contents" }));
  expect(Clipboard.setStringAsync).toHaveBeenCalledWith("const answer = 42;");
  fireEvent.press(screen.getByRole("button", { name: "Mention in chat" }));
  expect(onInsertIntoChat).toHaveBeenCalledWith("@file:/workspace/index.ts ");
  fireEvent.press(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalled();
});

it("renders Markdown and switches to source", async () => {
  setup("/workspace/README.md");
  await screen.findByText("const answer = 42;");
  expect(screen.queryByTestId("file-source")).toBeNull();
  fireEvent.press(screen.getByRole("button", { name: "Source" }));
  expect(screen.getByTestId("file-source")).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Preview" }));
  expect(screen.queryByTestId("file-source")).toBeNull();
});

it.each(["photo.png", "clip.mp4"])(
  "pins %s until the viewer closes",
  async (name) => {
    const { media, release, unmount } = setup(`/workspace/${name}`);
    if (name.endsWith("mp4")) await screen.findByTestId("file-video");
    else await screen.findByLabelText(name);
    expect(media.resolvePinned).toHaveBeenCalledWith(`/workspace/${name}`);
    expect(release).not.toHaveBeenCalled();
    unmount();
    expect(release).toHaveBeenCalledTimes(1);
  },
);

it("releases a media request that finishes after closing", async () => {
  const { rerender, element, media, release } = setup(null);
  let resolve!: (value: { uri: string; release: () => void }) => void;
  media.resolvePinned.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  rerender(element("/workspace/late.png"));
  rerender(element(null));
  await act(async () => resolve({ uri: "file:///late", release }));
  expect(release).toHaveBeenCalledTimes(1);
  expect(screen.queryByTestId("file-viewer")).toBeNull();
});

it("shows permission errors in the viewer and can retry", async () => {
  const { rerender, element, readFile } = setup(null);
  readFile.mockResolvedValueOnce({ error: "Permission denied" });
  rerender(element("/workspace/private.txt"));
  await screen.findByText("Permission denied");
  expect(screen.queryByTestId("file-source")).toBeNull();
  fireEvent.press(screen.getByRole("button", { name: "Try again" }));
  await screen.findByTestId("file-source");
});

it("offers sharing for binary files without displaying them as text", async () => {
  const { rerender, element, readFile, media } = setup(null);
  readFile.mockResolvedValueOnce({ content: btoa("%PDF\0"), size: 5 });
  rerender(element("/workspace/report.pdf"));
  await screen.findByText("This file can't be shown as text.");
  // One Share button in the body, one action in the header.
  fireEvent.press(screen.getAllByRole("button", { name: "Share" })[0]!);
  await waitFor(() =>
    expect(Sharing.shareAsync).toHaveBeenCalledWith("file:///download", {
      mimeType: "application/pdf",
      dialogTitle: "report.pdf",
    }),
  );
  expect(media.openFile).toHaveBeenCalledWith("/workspace/report.pdf");
});

it("requires a workspace media cache for image previews", async () => {
  setup("/workspace/private.png", false);
  await screen.findByText("Media need a connected workspace.");
  expect(screen.queryByLabelText("private.png")).toBeNull();
  expect(screen.queryByRole("button", { name: "Share" })).toBeNull();
});

it("opens inline image attachments in the same viewer without workspace-path actions", async () => {
  const attachment = {
    uri: "data:image/png;base64,aGVsbG8=",
    mime: "image/png",
    filename: "generated.png",
  };
  const { media, readFile } = setup(attachment);
  await screen.findByLabelText("generated.png");
  expect(media.saveAttachment).toHaveBeenCalledWith(attachment.uri);
  expect(readFile).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Copy path" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Mention in chat" })).toBeNull();
});

it("shares PDF attachments through the media cache and reports failures", async () => {
  const attachment = {
    uri: "data:application/pdf;base64,JVBERg==",
    mime: "application/pdf",
    filename: "report.pdf",
  };
  const { media } = setup(attachment);
  fireEvent.press(await screen.findByRole("button", { name: "Share or open" }));
  await waitFor(() =>
    expect(media.shareAttachment).toHaveBeenCalledWith(attachment.uri),
  );
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  media.shareAttachment.mockRejectedValueOnce(new Error("Sharing failed"));
  fireEvent.press(screen.getByRole("button", { name: "Share or open" }));
  await waitFor(() =>
    expect(alert).toHaveBeenCalledWith("Can't share file", "Sharing failed"),
  );
  alert.mockRestore();
});
