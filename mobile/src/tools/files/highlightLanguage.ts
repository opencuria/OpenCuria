import { HIGHLIGHTS_LANGUAGE_IDS } from "./highlights.generated";

/** Extensions whose lexer name differs from the extension. */
const EXTENSION_LANGUAGES: Readonly<Record<string, string>> = {
  luau: "lua",
  xsl: "xml",
  xslt: "xml",
  plist: "xml",
  csproj: "xml",
  bash: "bash",
  fish: "bash",
  ksh: "bash",
  env: "bash",
  ipynb: "json",
  lock: "text",
  rst: "text",
  cfg: "toml",
  ini: "toml",
  conf: "text",
  yarnrc: "yaml",
  mkd: "markdown",
  markdown: "markdown",
  pyi: "python",
  pyw: "python",
  rake: "ruby",
  gemspec: "ruby",
  h: "c",
  hh: "cpp",
};

/** Exact file names (lower-cased) with a known lexer. */
const NAME_LANGUAGES: Readonly<Record<string, string>> = {
  dockerfile: "dockerfile",
  containerfile: "dockerfile",
  makefile: "makefile",
  gnumakefile: "makefile",
  "cmakelists.txt": "cmake",
  gemfile: "ruby",
  rakefile: "ruby",
  podfile: "ruby",
  ".bashrc": "bash",
  ".bash_profile": "bash",
  ".zshrc": "bash",
  ".profile": "bash",
  ".env": "bash",
  "go.mod": "go",
  ".gitignore": "text",
};

/** The native lexer ID for a @pierre/highlights language name, or 0 (plain text). */
export function highlightLanguageId(language: string): number {
  return Object.hasOwn(HIGHLIGHTS_LANGUAGE_IDS, language)
    ? HIGHLIGHTS_LANGUAGE_IDS[language]!
    : 0;
}

/** A @pierre/highlights language for a file path, or `text`. */
export function resolveHighlightLanguage(path: string): string {
  const name = (path.split("/").pop() ?? path).toLowerCase();
  const byName = NAME_LANGUAGES[name];
  if (byName) return byName;
  if (name.startsWith("dockerfile.")) return "dockerfile";
  if (name.startsWith(".env.")) return "bash";
  const dot = name.lastIndexOf(".");
  if (dot <= 0 && !name.startsWith(".")) return "text";
  const extension = name.slice(dot + 1);
  const mapped = EXTENSION_LANGUAGES[extension];
  if (mapped) return mapped;
  return highlightLanguageId(extension) !== 0 ? extension : "text";
}
