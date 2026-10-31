import { describe, expect, it } from "vitest";
import {
  highlightLanguageId,
  resolveHighlightLanguage,
} from "./highlightLanguage";

describe("resolveHighlightLanguage", () => {
  it.each([
    ["src/app.tsx", "tsx"],
    ["backend/apps/api.py", "py"],
    ["Dockerfile", "dockerfile"],
    ["docker/Dockerfile.dev", "dockerfile"],
    ["CMakeLists.txt", "cmake"],
    ["src/init.luau", "lua"],
    ["styles/page.xslt", "xml"],
    ["README.md", "md"],
    ["notes.unknown", "text"],
    ["LICENSE", "text"],
  ])("%s highlights as %s", (path, language) => {
    expect(resolveHighlightLanguage(path)).toBe(language);
  });

  it("maps every resolved language to a lexer", () => {
    for (const path of ["a.ts", "b.rs", "c.go", "d.yaml", "Makefile", "x.h"])
      expect(
        highlightLanguageId(resolveHighlightLanguage(path)),
      ).toBeGreaterThan(0);
  });
});
