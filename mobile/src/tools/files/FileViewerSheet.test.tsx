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
import { hasNativePdfView } from "./PdfPreview";

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
// Test boundary for the PDFKit / PdfRenderer view.
jest.mock("./PdfPreview", () => {
  const { Pressable } =
    require("react-native") as typeof import("react-native");
  return {
    hasNativePdfView: jest.fn(() => true),
    PdfPreview: ({
      uri,
      name,
      onError,
    }: {
      uri: string;
      name: string;
      onError: (message: string) => void;
    }) => (
      <Pressable
        testID="file-pdf"
        accessibilityLabel={`${name} ${uri}`}
        onPress={() => onError("This PDF is password-protected.")}
      />
    ),
  };
});
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

/** What `client.files.readBytes` returns for a small file. */
function textBytes(text: string) {
  const bytes = new TextEncoder().encode(text);
  return { link: { size: bytes.length }, bytes, truncated: false };
}

function setup(
  file: FileViewerTarget | null = "/workspace/index.ts",
  connected = true,
) {
  const readBytes = jest
    .fn()
    .mockResolvedValue(textBytes("const answer = 42;"));
  const client = { files: { readBytes } } as unknown as OpenCuriaClient;
  const release = jest.fn();
  const media = {
    describe: jest.fn().mockResolvedValue({ size: 1024, thumbnailUrl: "" }),
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
    readBytes,
    media,
    release,
    onInsertIntoChat,
    onClose,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.mocked(hasNativePdfView).mockReturnValue(true);
});

it("opens an explorer file in the same viewer and returns to the file tree", async () => {
  const readBytes = jest
    .fn()
    .mockResolvedValue(textBytes("const answer = 42;"));
  const client = {
    files: {
      listFiles: jest.fn().mockResolvedValue({
        entries: [{ path: "/workspace/index.ts", type: "file" }],
      }),
      readBytes,
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
  expect(readBytes).toHaveBeenCalledWith("ws", "/workspace/index.ts", {
    maxBytes: 1024 * 1024,
  });
  fireEvent.press(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByTestId("file-viewer")).toBeNull();
  expect(screen.getByRole("button", { name: "index.ts" })).toBeTruthy();
});

it("shows TypeScript source and offers the explorer's copy and mention actions", async () => {
  const { readBytes, media, onInsertIntoChat, onClose } = setup();
  await screen.findByTestId("file-source");
  expect(readBytes).toHaveBeenCalledWith("ws", "/workspace/index.ts", {
    maxBytes: 1024 * 1024,
  });
  expect(media.resolvePinned).not.toHaveBeenCalled();
  fireEvent.press(screen.getByRole("button", { name: "Copy contents" }));
  expect(Clipboard.setStringAsync).toHaveBeenCalledWith("const answer = 42;");
  fireEvent.press(screen.getByRole("button", { name: "Mention in chat" }));
  expect(onInsertIntoChat).toHaveBeenCalledWith("@file:/workspace/index.ts ");
  fireEvent.press(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalled();
});

it("streams workspace videos from their link", async () => {
  const { rerender, element, media } = setup(null);
  media.resolvePinned.mockResolvedValueOnce({
    uri: "https://curia.test/content/clip.mp4",
    release: jest.fn(),
  });
  rerender(element("/workspace/clip.mp4"));
  expect((await screen.findByTestId("file-video")).props.player.source).toBe(
    "https://curia.test/content/clip.mp4",
  );
});

it("shows large pictures of a Markdown file as previews that load on tap", async () => {
  const { rerender, element, readBytes, media } = setup(null);
  readBytes.mockResolvedValueOnce(textBytes("![chart](/workspace/chart.png)"));
  media.describe.mockResolvedValue({
    size: 6 * 1024 * 1024,
    thumbnailUrl: "https://curia.test/thumbnail/chart.png",
  });
  rerender(element("/workspace/notes.md"));
  fireEvent.press(
    await screen.findByRole("button", { name: "Load image chart, 6.0 MB" }),
  );
  expect(
    await screen.findByRole("button", { name: "Open image preview chart" }),
  ).toBeTruthy();
  expect(media.describe).toHaveBeenCalledWith("/workspace/chart.png");
  expect(media.resolvePinned).toHaveBeenCalledTimes(1);
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
  const { rerender, element, readBytes } = setup(null);
  readBytes.mockRejectedValueOnce(new Error("Permission denied"));
  rerender(element("/workspace/private.txt"));
  await screen.findByText("Permission denied");
  expect(screen.queryByTestId("file-source")).toBeNull();
  fireEvent.press(screen.getByRole("button", { name: "Try again" }));
  await screen.findByTestId("file-source");
});

it("offers sharing for binary files without displaying them as text", async () => {
  const { rerender, element, readBytes, media } = setup(null);
  readBytes.mockResolvedValueOnce(textBytes("PK\0"));
  media.openFile.mockResolvedValueOnce({
    uri: "file:///download",
    mimeType: "application/zip",
  });
  rerender(element("/workspace/archive.zip"));
  await screen.findByText("This file can't be shown as text.");
  expect(screen.queryByRole("button", { name: "Word wrap" })).toBeNull();
  // One Share button in the body, one action in the header.
  fireEvent.press(screen.getAllByRole("button", { name: "Share" })[0]!);
  await waitFor(() =>
    expect(Sharing.shareAsync).toHaveBeenCalledWith("file:///download", {
      mimeType: "application/zip",
      dialogTitle: "archive.zip",
    }),
  );
  expect(media.openFile).toHaveBeenCalledWith("/workspace/archive.zip");
});

it("shows workspace PDFs in the native document view and shares the same file", async () => {
  const { readBytes, media } = setup("/workspace/docs/report.pdf");
  await screen.findByLabelText("report.pdf file:///download");
  expect(media.openFile).toHaveBeenCalledWith("/workspace/docs/report.pdf");
  expect(readBytes).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Word wrap" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Copy contents" })).toBeNull();
  fireEvent.press(screen.getByRole("button", { name: "Share" }));
  await waitFor(() =>
    expect(Sharing.shareAsync).toHaveBeenCalledWith("file:///download", {
      mimeType: "application/pdf",
      dialogTitle: "report.pdf",
    }),
  );
  expect(media.openFile).toHaveBeenCalledTimes(1);
});

it("offers sharing when a PDF can't be displayed", async () => {
  const { media } = setup("/workspace/locked.pdf");
  fireEvent.press(await screen.findByTestId("file-pdf"));
  await screen.findByText("This PDF is password-protected.");
  fireEvent.press(screen.getAllByRole("button", { name: "Share" })[0]!);
  await waitFor(() => expect(Sharing.shareAsync).toHaveBeenCalled());
  expect(media.openFile).toHaveBeenLastCalledWith("/workspace/locked.pdf");
});

it("reports PDF download failures with a retry", async () => {
  const { rerender, element, media } = setup(null);
  media.openFile.mockRejectedValueOnce(new Error("Workspace is offline"));
  rerender(element("/workspace/report.pdf"));
  await screen.findByText("Workspace is offline");
  fireEvent.press(screen.getByRole("button", { name: "Try again" }));
  await screen.findByTestId("file-pdf");
});

it("keeps PDFs shareable in app builds without the native document view", async () => {
  jest.mocked(hasNativePdfView).mockReturnValue(false);
  const { media } = setup("/workspace/report.pdf");
  await screen.findByText("This app version can't preview PDFs.");
  expect(screen.queryByTestId("file-pdf")).toBeNull();
  fireEvent.press(screen.getAllByRole("button", { name: "Share" })[0]!);
  await waitFor(() =>
    expect(media.openFile).toHaveBeenCalledWith("/workspace/report.pdf"),
  );
});

it("shows PDF attachments in the native document view", async () => {
  const attachment = {
    uri: "data:application/pdf;base64,JVBERg==",
    mime: "application/pdf",
    filename: "report.pdf",
  };
  const { media } = setup(attachment);
  await screen.findByLabelText("report.pdf file:///attachment");
  expect(media.saveAttachment).toHaveBeenCalledWith(attachment.uri);
  fireEvent.press(screen.getByRole("button", { name: "Share" }));
  await waitFor(() =>
    expect(media.shareAttachment).toHaveBeenCalledWith(attachment.uri),
  );
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
  const { media, readBytes } = setup(attachment);
  await screen.findByLabelText("generated.png");
  expect(media.saveAttachment).toHaveBeenCalledWith(attachment.uri);
  expect(readBytes).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Copy path" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Mention in chat" })).toBeNull();
});

it("shares PDF attachments through the media cache and reports failures", async () => {
  jest.mocked(hasNativePdfView).mockReturnValue(false);
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
