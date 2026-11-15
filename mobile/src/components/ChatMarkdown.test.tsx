import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { Pressable, Text } from "react-native";
import { ChatMarkdown } from "./ChatMarkdown";
import { ThemeProvider } from "../ui/theme";

jest.mock("lucide-react-native", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  const icon = (props: Record<string, unknown>) =>
    React.createElement(ReactNative.View, props);
  return new Proxy({}, { get: () => icon });
});
jest.mock(
  "remend",
  () => ({ __esModule: true, default: (markdown: string) => markdown }),
  { virtual: true },
);
jest.mock("marked", () => {
  const actual = jest.requireActual("marked");
  return {
    ...actual,
    marked: {
      ...actual.marked,
      lexer: jest.fn((markdown: string) => actual.marked.lexer(markdown)),
    },
  };
});
jest.mock("react-native-enriched-markdown", () => ({
  EnrichedMarkdownText: ({ markdown }: { markdown: string }) => {
    const React = require("react");
    const ReactNative = require("react-native");
    return React.createElement(ReactNative.Text, null, markdown);
  },
}));
jest.mock("expo-image", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  return {
    Image: (props: Record<string, unknown>) =>
      React.createElement(ReactNative.Image, props),
  };
});
jest.mock("expo-video", () => ({
  useVideoPlayer: (source: string) => ({ source }),
  VideoView: ({ accessibilityLabel, ...props }: Record<string, unknown>) => {
    const React = require("react");
    const ReactNative = require("react-native");
    return React.createElement(ReactNative.View, {
      ...props,
      accessibilityLabel,
    });
  },
}));
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

function StreamingFixture({
  resolver,
}: {
  resolver: (path: string) => Promise<string>;
}) {
  const [markdown, setMarkdown] = React.useState(
    "![preview](/workspace/picture.png)",
  );
  return (
    <>
      <ChatMarkdown markdown={markdown} streaming resolveMedia={resolver} />
      <Pressable
        testID="append-stream-token"
        onPress={() => setMarkdown((current) => `${current} token`)}
      >
        <Text>Append token</Text>
      </Pressable>
    </>
  );
}

describe("ChatMarkdown media handling", () => {
  it("does not transform image-looking text in a fenced code block", () => {
    const source = "```md\n![not an image](/workspace/example.png)\n```";
    const resolver = jest.fn();
    render(
      <ThemeProvider>
        <ChatMarkdown markdown={source} resolveMedia={resolver} />
      </ThemeProvider>,
    );
    expect(screen.getByText(source)).toBeTruthy();
    expect(resolver).not.toHaveBeenCalled();
  });

  it("deduplicates a pending media read across streaming updates and emits no React warnings", async () => {
    let resolveImage!: (uri: string) => void;
    const resolver = jest.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveImage = resolve;
        }),
    );
    const consoleError = jest
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    render(
      <ThemeProvider>
        <StreamingFixture resolver={resolver} />
      </ThemeProvider>,
    );
    const update = screen.getByTestId("append-stream-token");

    for (let index = 0; index < 20; index += 1) fireEvent.press(update);
    await waitFor(() => expect(resolver).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveImage("file:///private/image.png");
    });
    expect(
      await screen.findByRole("button", { name: "Open image preview preview" }),
    ).toBeTruthy();
    expect(resolver).toHaveBeenCalledTimes(1);
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("holds visible files until the row leaves the viewport and reacquires on return", async () => {
    const release = jest.fn();
    const resolver = jest
      .fn()
      .mockResolvedValue({ uri: "file:///private/image.png", release });
    const tree = render(
      <ThemeProvider>
        <ChatMarkdown
          markdown="![preview](/workspace/picture.png)"
          resolveMedia={resolver}
          mediaVisible
        />
      </ThemeProvider>,
    );
    await screen.findByRole("button", { name: "Open image preview preview" });
    tree.rerender(
      <ThemeProvider>
        <ChatMarkdown
          markdown="![preview](/workspace/picture.png)"
          resolveMedia={resolver}
          mediaVisible={false}
        />
      </ThemeProvider>,
    );
    expect(release).toHaveBeenCalledTimes(1);
    tree.rerender(
      <ThemeProvider>
        <ChatMarkdown
          markdown="![preview](/workspace/picture.png)"
          resolveMedia={resolver}
          mediaVisible
        />
      </ThemeProvider>,
    );
    await waitFor(() => expect(resolver).toHaveBeenCalledTimes(2));
    tree.unmount();
    expect(release).toHaveBeenCalledTimes(2);
  });

  it("retries a failed read in the background until the image arrives", async () => {
    jest.useFakeTimers();
    try {
      const resolver = jest
        .fn()
        .mockRejectedValueOnce(new Error("Socket is not connected"))
        .mockRejectedValueOnce(new Error("File transfer timed out"))
        .mockResolvedValue("file:///private/shot.png");
      render(
        <ThemeProvider>
          <ChatMarkdown
            markdown={"![shot](/workspace/shot.png)"}
            resolveMedia={resolver}
          />
        </ThemeProvider>,
      );
      expect(screen.getByLabelText("Loading shot")).toBeTruthy();
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1_000);
      });
      await act(async () => {
        await jest.advanceTimersByTimeAsync(2_000);
      });
      expect(
        await screen.findByRole("button", { name: "Open image preview shot" }),
      ).toBeTruthy();
      expect(resolver).toHaveBeenCalledTimes(3);
    } finally {
      jest.useRealTimers();
    }
  });

  it("offers a manual retry after a permanent failure", async () => {
    const permanent = Object.assign(new Error("Too large"), {
      retryable: false,
    });
    const resolver = jest
      .fn()
      .mockRejectedValueOnce(permanent)
      .mockResolvedValue("file:///private/shot.png");
    render(
      <ThemeProvider>
        <ChatMarkdown
          markdown={"![shot](/workspace/shot.png)"}
          resolveMedia={resolver}
        />
      </ThemeProvider>,
    );
    fireEvent.press(
      await screen.findByRole("button", { name: "Retry loading shot" }),
    );
    expect(
      await screen.findByRole("button", { name: "Open image preview shot" }),
    ).toBeTruthy();
    expect(resolver).toHaveBeenCalledTimes(2);
  });

  it("reads the file again once when the image view cannot display it", async () => {
    const release = jest.fn();
    const resolver = jest
      .fn()
      .mockResolvedValue({ uri: "file:///private/shot.png", release });
    render(
      <ThemeProvider>
        <ChatMarkdown
          markdown={"![shot](/workspace/shot.png)"}
          resolveMedia={resolver}
        />
      </ThemeProvider>,
    );
    await screen.findByRole("button", { name: "Open image preview shot" });
    fireEvent(screen.getByLabelText("shot"), "error");
    await waitFor(() => expect(resolver).toHaveBeenCalledTimes(2));
    expect(release).toHaveBeenCalledTimes(1);
    fireEvent(await screen.findByLabelText("shot"), "error");
    expect(
      await screen.findByRole("button", { name: "Retry loading shot" }),
    ).toBeTruthy();
    expect(resolver).toHaveBeenCalledTimes(2);
    expect(release).toHaveBeenCalledTimes(2);
  });

  it("keeps the preview open with its own lease when the row scrolls away", async () => {
    const releases: jest.Mock[] = [];
    const resolver = jest.fn(async () => {
      const release = jest.fn();
      releases.push(release);
      return { uri: "file:///private/shot.png", release };
    });
    const markdown = "![shot](/workspace/shot.png)";
    const tree = render(
      <ThemeProvider>
        <ChatMarkdown
          markdown={markdown}
          resolveMedia={resolver}
          mediaVisible
        />
      </ThemeProvider>,
    );
    fireEvent.press(
      await screen.findByRole("button", { name: "Open image preview shot" }),
    );
    await waitFor(() => expect(resolver).toHaveBeenCalledTimes(2));
    tree.rerender(
      <ThemeProvider>
        <ChatMarkdown
          markdown={markdown}
          resolveMedia={resolver}
          mediaVisible={false}
        />
      </ThemeProvider>,
    );
    expect(releases[0]).toHaveBeenCalledTimes(1);
    expect(releases[1]).not.toHaveBeenCalled();
    expect(screen.getByTestId("image-preview")).toBeTruthy();
    tree.unmount();
    expect(releases[1]).toHaveBeenCalledTimes(1);
  });

  it("renders a workspace video reference as a native video card, not an image", async () => {
    const resolver = jest.fn().mockResolvedValue("file:///private/clip.mp4");
    render(
      <ThemeProvider>
        <ChatMarkdown
          markdown={"![demo clip](/workspace/demo.mp4)"}
          resolveMedia={resolver}
        />
      </ThemeProvider>,
    );
    expect(await screen.findByLabelText("Video demo clip")).toBeTruthy();
    expect(resolver).toHaveBeenCalledWith("/workspace/demo.mp4");
    expect(
      screen.queryByRole("button", { name: "Open image preview demo clip" }),
    ).toBeNull();
  });
});
