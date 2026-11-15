import { describe, expect, it } from "vitest";
import type { HarnessMessage, HarnessPart } from "@opencuria/chat-core/harness";
import { chatImages, galleryFor, mediaKey } from "./chatGallery";

const part = (
  id: string,
  type: HarnessPart["type"],
  output: string,
  position: number,
): HarnessPart => ({
  id,
  session_id: "s1",
  type,
  title: "",
  state: "completed",
  output,
  position,
});
const user = (id: string, content: string): HarnessMessage => ({
  id,
  session_id: "s1",
  role: "user",
  content,
  parts: [],
});
const assistant = (id: string, parts: HarnessPart[]): HarnessMessage => ({
  id,
  session_id: "s1",
  role: "assistant",
  content: "",
  parts,
});

const keys = (messages: HarnessMessage[]) =>
  chatImages(messages).map((media) => mediaKey(media));

describe("chatImages", () => {
  it("lists the pictures of prompts and answers in reading order", () => {
    expect(
      keys([
        user(
          "u1",
          "Compare @file:/workspace/before.png with ![mock](/workspace/mock.jpg)",
        ),
        assistant("a1", [
          part("t2", "text", "![after](/workspace/after.png)", 2),
          part("t1", "text", "First ![plan](/workspace/plan.png)", 0),
          part("s1", "steer", "Also @file:/workspace/steer.webp", 1),
        ]),
      ]),
    ).toEqual([
      "/workspace/before.png",
      "/workspace/mock.jpg",
      "/workspace/plan.png",
      "/workspace/steer.webp",
      "/workspace/after.png",
    ]);
  });

  it("keeps each file once, remote pictures included, and leaves out videos and tool output", () => {
    const images = chatImages([
      user("u1", "![shot](/workspace/shot.png)"),
      assistant("a1", [
        part("t1", "text", "Again ![shot](/workspace/shot.png)", 0),
        part("tool", "tool", "![tool](/workspace/tool.png)", 1),
        part("t2", "text", "![clip](/workspace/clip.mp4)", 2),
        part("t3", "text", "![logo](https://example.com/logo.png)", 3),
      ]),
      user("u2", "@file:/workspace/notes.md"),
    ]);
    expect(images.map((media) => mediaKey(media))).toEqual([
      "/workspace/shot.png",
      "https://example.com/logo.png",
    ]);
    expect(images.map((media) => media.label)).toEqual(["shot", "logo"]);
  });

  it("reads an unchanged message snapshot once", () => {
    const message = assistant("a1", [
      part("t1", "text", "![a](/workspace/a.png)", 0),
    ]);
    expect(chatImages([message])[0]).toBe(chatImages([message])[0]);
  });
});

describe("galleryFor", () => {
  const a = { kind: "image" as const, path: "/workspace/a.png", label: "a" };
  const b = { kind: "image" as const, path: "/workspace/b.png", label: "b" };
  const c = { kind: "image" as const, path: "/workspace/c.png", label: "c" };

  it("opens a conversation picture within the whole conversation", () => {
    expect(galleryFor({ ...b }, [a, b], [b])).toEqual({
      items: [a, b],
      index: 1,
    });
  });

  it("pages through the pictures beside a picture the conversation does not list", () => {
    expect(galleryFor(c, [a, b], [b, c])).toEqual({ items: [b, c], index: 1 });
    expect(galleryFor(c, [], [])).toEqual({ items: [c], index: 0 });
  });
});
