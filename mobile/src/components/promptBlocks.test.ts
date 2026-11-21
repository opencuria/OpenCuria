import { describe, expect, it } from "vitest";
import { promptLayout } from "./promptBlocks";

describe("prompt layout", () => {
  it("keeps prompts without mentions as one Markdown block", () => {
    expect(promptLayout("Plain **text**\n\n- a list")).toEqual({
      blocks: [{ kind: "markdown", markdown: "Plain **text**\n\n- a list" }],
      images: [],
    });
    expect(promptLayout("   ").blocks).toEqual([]);
  });

  it("lays out paragraphs with mentions as styled runs and chips", () => {
    const { blocks, images } = promptLayout(
      "Read @file:/workspace/src/a.ts and **fix** `@file:/workspace/x.ts`\nnow @agent:build",
    );
    expect(images).toEqual([]);
    expect(blocks).toHaveLength(1);
    const [paragraph] = blocks;
    expect(paragraph?.kind).toBe("paragraph");
    if (paragraph?.kind !== "paragraph") return;
    expect(
      paragraph.runs.map((run) =>
        run.kind === "mention"
          ? `<${run.token.kind}:${run.token.kind === "file" ? run.token.name : run.token.name}>`
          : `${run.strong ? "**" : ""}${run.code ? "`" : ""}${run.text}`,
      ),
    ).toEqual([
      "Read ",
      "<file:a.ts>",
      " and ",
      "**fix",
      " ",
      "`@file:/workspace/x.ts",
      " now ",
      "<agent:build>",
    ]);
  });

  it("collects mentioned pictures once and links mentions in other blocks", () => {
    const { blocks, images } = promptLayout(
      "Compare @file:/workspace/a.png with @file:/workspace/a.png\n\n" +
        "- also @file:/workspace/b.jpg\n\n" +
        "```\n@file:/workspace/c.png\n```",
    );
    expect(images.map((image) => image.path)).toEqual([
      "/workspace/a.png",
      "/workspace/b.jpg",
    ]);
    expect(blocks.map((block) => block.kind)).toEqual([
      "paragraph",
      "markdown",
    ]);
    const markdown = blocks[1]?.kind === "markdown" ? blocks[1].markdown : "";
    expect(markdown).toContain("- also [b.jpg](</workspace/b.jpg>)");
    // Code keeps the mention as typed and loads nothing.
    expect(markdown).toContain("```\n@file:/workspace/c.png\n```");
  });

  it("keeps inline code of other blocks untouched", () => {
    const { blocks } = promptLayout(
      "# Title `@file:/workspace/a.ts` @file:/workspace/b.ts",
    );
    expect(blocks).toEqual([
      {
        kind: "markdown",
        markdown: "# Title `@file:/workspace/a.ts` [b.ts](</workspace/b.ts>)",
      },
    ]);
  });
});
