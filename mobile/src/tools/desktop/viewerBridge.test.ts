// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DESKTOP_VIEWER_BRIDGE,
  keyCommand,
  parseDesktopViewerMessage,
  pointerCommand,
  scrollCommand,
  textCommand,
} from "./viewerBridge";
import { DESKTOP_KEYS } from "./desktopKeys";

type Recorded = {
  type: string;
  key?: string;
  code?: string;
  clientX?: number;
  clientY?: number;
  button?: number;
  buttons?: number;
  ctrlKey?: boolean;
  deltaY?: number;
};

const posted: unknown[] = [];
let events: Recorded[] = [];

/** The parts of KasmVNC's page the bridge relies on. */
function kasmPage(): HTMLCanvasElement {
  document.documentElement.className = "noVNC_connecting";
  document.body.innerHTML =
    '<div id="noVNC_control_bar_anchor"></div>' +
    '<div id="noVNC_container"><div><canvas width="1920" height="1080"></canvas></div></div>';
  const canvas = document.querySelector("canvas")!;
  canvas.getBoundingClientRect = () =>
    ({
      left: 0,
      top: 0,
      width: 384,
      height: 216,
      right: 384,
      bottom: 216,
    }) as DOMRect;
  for (const type of [
    "mousemove",
    "mousedown",
    "mouseup",
    "wheel",
    "keydown",
    "keyup",
  ])
    canvas.addEventListener(type, (event) => {
      const value = event as MouseEvent & KeyboardEvent & WheelEvent;
      events.push({
        type,
        key: value.key,
        code: value.code,
        clientX: value.clientX,
        clientY: value.clientY,
        button: value.button,
        buttons: value.buttons,
        ctrlKey: value.ctrlKey,
        deltaY: value.deltaY,
      });
    });
  return canvas;
}

function run(script: string): void {
  // eslint-disable-next-line no-new-func
  new Function(script)();
}

beforeEach(() => {
  // The bridge's state heartbeat must not outlive the test document.
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  posted.length = 0;
  events = [];
  delete (window as { __opencuriaDesktop?: unknown }).__opencuriaDesktop;
  (window as unknown as { ReactNativeWebView: unknown }).ReactNativeWebView = {
    postMessage: (data: string) => posted.push(JSON.parse(data)),
  };
  kasmPage();
  run(DESKTOP_VIEWER_BRIDGE);
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  // Detach the previous page's observers by replacing the whole document.
  const html = document.createElement("html");
  html.append(document.createElement("head"), document.createElement("body"));
  document.replaceChild(html, document.documentElement);
});

describe("KasmVNC page bridge", () => {
  it("reduces the page to the framebuffer and reports state and size", async () => {
    expect(
      document.querySelector("style[data-opencuria-mobile]")?.textContent,
    ).toContain("body>*:not(#noVNC_container){display:none!important}");
    expect(navigator.platform).toBe("Linux x86_64");
    document.documentElement.className = "noVNC_connected";
    await vi.waitFor(() =>
      expect(posted).toContainEqual({ type: "state", state: "connected" }),
    );
    expect(posted).toContainEqual({
      type: "framebuffer",
      width: 1920,
      height: 1080,
    });
  });

  it("replays a click as canvas mouse events at the framebuffer pixel", () => {
    run(pointerCommand(960, 540, 1));
    run(pointerCommand(960, 540, 0));
    expect(events.map((event) => event.type)).toEqual([
      "mousemove",
      "mousedown",
      "mousemove",
      "mouseup",
    ]);
    // 384 CSS px show 1920 framebuffer px: pixel 960 lies at x = 192.
    expect(events[1]).toMatchObject({
      clientX: 192,
      clientY: 108,
      button: 0,
      buttons: 1,
    });
    expect(events[3]).toMatchObject({ button: 0, buttons: 0 });
  });

  it("right-clicks and scrolls with the X11 button mapping", () => {
    run(pointerCommand(10, 10, 2));
    expect(events.find((event) => event.type === "mousedown")).toMatchObject({
      button: 2,
      buttons: 2,
    });
    run(scrollCommand(10, 10, 0, 120));
    expect(events.at(-1)).toMatchObject({ type: "wheel", deltaY: 120 });
  });

  it("holds modifiers around a key and releases them afterwards", () => {
    run(
      keyCommand(DESKTOP_KEYS.v, {
        ctrl: true,
        alt: false,
        shift: false,
        meta: false,
      }),
    );
    expect(events.map((event) => `${event.type}:${event.key}`)).toEqual([
      "keydown:Control",
      "keydown:v",
      "keyup:v",
      "keyup:Control",
    ]);
    expect(events[1]?.ctrlKey).toBe(true);
    expect(events[3]?.ctrlKey).toBe(false);
  });

  it("types text through KasmVNC's virtual-keyboard path", () => {
    run(textCommand("aÄ"));
    expect(events).toEqual([
      expect.objectContaining({
        type: "keydown",
        key: "a",
        code: "Unidentified",
      }),
      expect.objectContaining({ type: "keyup", key: "a" }),
      expect.objectContaining({
        type: "keydown",
        key: "Ä",
        code: "Unidentified",
      }),
      expect.objectContaining({ type: "keyup", key: "Ä" }),
    ]);
  });
});

describe("page messages", () => {
  it("accepts only well-formed messages", () => {
    expect(
      parseDesktopViewerMessage('{"type":"state","state":"connected"}'),
    ).toEqual({
      type: "state",
      state: "connected",
    });
    expect(
      parseDesktopViewerMessage('{"type":"state","state":"evil"}'),
    ).toBeNull();
    expect(parseDesktopViewerMessage("not json")).toBeNull();
    expect(
      parseDesktopViewerMessage(
        '{"type":"cursor","image":"https://example.com/x.png","width":1,"height":1}',
      ),
    ).toBeNull();
    expect(
      parseDesktopViewerMessage(
        '{"type":"cursor","image":"data:image/png;base64,AA==","width":24,"height":24,"hotX":-3,"hotY":4}',
      ),
    ).toEqual({
      type: "cursor",
      image: "data:image/png;base64,AA==",
      width: 24,
      height: 24,
      hotX: 0,
      hotY: 4,
    });
  });
});
