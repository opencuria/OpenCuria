import { describe, expect, it } from "vitest";
import { splitMarkdownMedia, type MarkdownPiece } from "./markdownMedia";

/** Compact view: markdown text, or `[media:<path|uri>]`. */
function shape(markdown: string): string[] {
  return splitMarkdownMedia(markdown).map((piece: MarkdownPiece) =>
    piece.kind === "markdown"
      ? piece.text
      : `[${piece.media.kind}:${piece.media.path ?? piece.media.uri}]`,
  );
}

function mediaPaths(markdown: string): string[] {
  return splitMarkdownMedia(markdown).flatMap((piece) =>
    piece.kind === "media" ? [piece.media.path ?? piece.media.uri ?? ""] : [],
  );
}

describe("splitMarkdownMedia", () => {
  it("keeps Markdown without media as a single piece", () => {
    expect(shape("Hello **world**")).toEqual(["Hello **world**"]);
    expect(shape("")).toEqual([]);
  });

  it("splits paragraphs at workspace images and keeps the prose", () => {
    expect(
      shape(
        "Hier ist der Screenshot:\n\n![Ergebnis](/workspace/screenshots/result.png)\n\nFertig.",
      ),
    ).toEqual([
      "Hier ist der Screenshot:\n\n",
      "[image:/workspace/screenshots/result.png]",
      "\n\nFertig.",
    ]);
    expect(
      shape("Vorher ![a](/workspace/a.png) und ![b](/workspace/b.png)"),
    ).toEqual([
      "Vorher ",
      "[image:/workspace/a.png]",
      "und ",
      "[image:/workspace/b.png]",
    ]);
  });

  it("finds images on continuation lines of ordered and nested list items", () => {
    expect(
      shape(
        "Ergebnisse:\n\n1. Startseite\n   ![Start](/workspace/screenshots/start.png)\n2. Login\n   ![Login](/workspace/screenshots/login.png)\n",
      ),
    ).toEqual([
      "Ergebnisse:\n\n1. Startseite\n   ",
      "[image:/workspace/screenshots/start.png]",
      "\n2. Login\n   ",
      "[image:/workspace/screenshots/login.png]",
    ]);
    expect(
      shape(
        "- Item\n  - Sub\n    ![n](/workspace/n.png)\n    Beschreibung\n- Next",
      ),
    ).toEqual([
      "- Item\n  - Sub\n    ",
      "[image:/workspace/n.png]",
      "\nBeschreibung\n- Next",
    ]);
  });

  it("drops list markers that only held an image", () => {
    expect(
      shape("- ![Desktop](/workspace/d.png)\n- ![Mobile](/workspace/m.png)\n"),
    ).toEqual(["[image:/workspace/d.png]", "[image:/workspace/m.png]"]);
  });

  it("finds images in multi-line blockquotes", () => {
    expect(
      mediaPaths(
        "> ![q](/workspace/q.png)\n> mehr Text\n> ![q2](/workspace/q2.png)",
      ),
    ).toEqual(["/workspace/q.png", "/workspace/q2.png"]);
  });

  it("keeps tables intact and shows table images below them", () => {
    expect(
      shape(
        "| Vorher | Nachher |\n|---|---|\n| ![alt](/workspace/a.png) | ![neu](/workspace/b.png) |\n",
      ),
    ).toEqual([
      "| Vorher | Nachher |\n|---|---|\n| alt | neu |\n",
      "[image:/workspace/a.png]",
      "[image:/workspace/b.png]",
    ]);
  });

  it("replaces linked images and emphasised images as a whole", () => {
    expect(shape("[![a](/workspace/a.png)](/workspace/a.png)")).toEqual([
      "[image:/workspace/a.png]",
    ]);
    expect(shape("**![a](/workspace/a.png)**")).toEqual([
      "[image:/workspace/a.png]",
    ]);
  });

  it("keeps link text and shows an embedded image after it", () => {
    expect(
      shape("Siehe [hier ![a](/workspace/a.png)](https://example.com) bitte"),
    ).toEqual([
      "Siehe [hier a](https://example.com)",
      "[image:/workspace/a.png]",
      "bitte",
    ]);
  });

  it("renders HTML img tags that point into the workspace", () => {
    expect(
      shape(
        '<p align="center"><img src="/workspace/shot.png" width="400"></p>\n\nText',
      ),
    ).toEqual(["[image:/workspace/shot.png]", "Text"]);
    expect(shape("Inline <img alt='x' src=\"/workspace/i.png\"> text")).toEqual(
      ["Inline ", "[image:/workspace/i.png]", "text"],
    );
  });

  it("supports spaces, umlauts, relative and reference-style destinations", () => {
    // Paths stay URL-encoded like the webapp's `src`; the media cache decodes once.
    expect(
      mediaPaths(
        "![s](</workspace/screenshots/Bild 1.png>)\n\n![u](/workspace/übersicht.png)\n\n![r](screenshots/rel.png)\n\n![ref][shot]\n\n![e](/workspace/a%20b.png)\n\n[shot]: /workspace/ref.png",
      ),
    ).toEqual([
      "/workspace/screenshots/Bild%201.png",
      "/workspace/%C3%BCbersicht.png",
      "/workspace/screenshots/rel.png",
      "/workspace/ref.png",
      "/workspace/a%20b.png",
    ]);
    const [piece] = splitMarkdownMedia("![](</workspace/Bild 1.png>)");
    expect(piece).toMatchObject({ media: { label: "Bild 1.png" } });
  });

  it("keeps the same image twice and classifies videos", () => {
    expect(
      shape(
        "![a](/workspace/a.png)\n\n![a](/workspace/a.png)\n\n![v](/workspace/demo.mp4)",
      ),
    ).toEqual([
      "[image:/workspace/a.png]",
      "[image:/workspace/a.png]",
      "[video:/workspace/demo.mp4]",
    ]);
  });

  it("never treats code as media, even when it repeats a later image", () => {
    expect(shape("```md\n![x](/workspace/x.png)\n```")).toEqual([
      "```md\n![x](/workspace/x.png)\n```",
    ]);
    expect(
      shape("`![a](/workspace/a.png)` ergibt ![a](/workspace/a.png)"),
    ).toEqual(["`![a](/workspace/a.png)` ergibt ", "[image:/workspace/a.png]"]);
  });

  it("shows alt text for unsupported or unsafe destinations", () => {
    expect(shape("![Bericht](/workspace/report.pdf)")).toEqual(["Bericht"]);
    expect(shape("![x](/etc/passwd)")).toEqual(["x"]);
    expect(shape("![x](javascript:alert(1))")).toEqual(["x"]);
    expect(shape("![remote](https://example.com/a.png)")).toEqual([
      "[image:https://example.com/a.png]",
    ]);
  });

  it("handles CRLF line endings", () => {
    expect(mediaPaths("1. A\r\n   ![a](/workspace/a.png)\r\n2. B\r\n")).toEqual(
      ["/workspace/a.png"],
    );
  });
});
