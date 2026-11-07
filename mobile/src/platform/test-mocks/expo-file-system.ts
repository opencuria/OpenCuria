const files = new Map<string, { data: string; encoding: string }>();
const directories = new Set<string>();

function uriOf(...parts: Array<string | Directory>): string {
  return parts
    .map((part) =>
      part instanceof Directory ? part.uri.replace(/\/$/, "") : part,
    )
    .join("/");
}

/** Directories from the scheme down to `uri`, as `intermediates` creates them. */
function addWithAncestors(uri: string): void {
  const scheme = /^[a-z]+:\/\/\/?/.exec(uri)?.[0] ?? "";
  const segments = uri.slice(scheme.length).split("/").filter(Boolean);
  for (let index = 1; index <= segments.length; index += 1)
    directories.add(`${scheme}${segments.slice(0, index).join("/")}`);
}

export class Directory {
  readonly uri: string;
  constructor(...parts: Array<string | Directory>) {
    this.uri = uriOf(...parts);
  }
  get exists(): boolean {
    return directories.has(this.uri);
  }
  get name(): string {
    return this.uri.split("/").pop() ?? "";
  }
  create(options?: { intermediates?: boolean; idempotent?: boolean }): void {
    if (options?.intermediates) addWithAncestors(this.uri);
    else directories.add(this.uri);
  }
  delete(): void {
    directories.delete(this.uri);
    for (const key of [...directories])
      if (key.startsWith(`${this.uri}/`)) directories.delete(key);
    for (const key of files.keys())
      if (key.startsWith(`${this.uri}/`)) files.delete(key);
  }
  /** Files and directories directly inside this one. */
  list(): Array<File | Directory> {
    const inside = (key: string) =>
      key.startsWith(`${this.uri}/`) &&
      !key.slice(this.uri.length + 1).includes("/");
    return [
      ...[...directories].filter(inside).map((key) => new Directory(key)),
      ...[...files.keys()].filter(inside).map((key) => new File(key)),
    ];
  }
}

export class File {
  readonly uri: string;
  constructor(...parts: Array<string | Directory>) {
    this.uri = uriOf(...parts);
  }
  get exists(): boolean {
    return files.has(this.uri);
  }
  get size(): number {
    const item = files.get(this.uri);
    if (!item) return 0;
    return item.encoding === "base64"
      ? Math.floor((item.data.length * 3) / 4) -
          (item.data.endsWith("==") ? 2 : item.data.endsWith("=") ? 1 : 0)
      : item.data.length;
  }
  get name(): string {
    return this.uri.split("/").pop() ?? "";
  }
  create(options?: { intermediates?: boolean }): void {
    if (options?.intermediates)
      addWithAncestors(this.uri.slice(0, this.uri.lastIndexOf("/")));
    files.set(this.uri, { data: "", encoding: "utf8" });
  }
  write(data: string, options?: { encoding?: string }): void {
    files.set(this.uri, { data, encoding: options?.encoding ?? "utf8" });
  }
  async base64(): Promise<string> {
    const item = files.get(this.uri);
    if (!item) throw new Error("missing file");
    return item.encoding === "base64" ? item.data : btoa(item.data);
  }
  async text(): Promise<string> {
    const item = files.get(this.uri);
    if (!item) throw new Error("missing file");
    return item.encoding === "base64" ? atob(item.data) : item.data;
  }
  delete(): void {
    files.delete(this.uri);
  }
  moveSync(target: File, options?: { overwrite?: boolean }): void {
    const item = files.get(this.uri);
    if (!item) throw new Error("missing file");
    if (files.has(target.uri) && !options?.overwrite)
      throw new Error("target exists");
    files.set(target.uri, item);
    files.delete(this.uri);
  }
  copySync(target: File, options?: { overwrite?: boolean }): void {
    const item = files.get(this.uri);
    if (!item) throw new Error("missing file");
    if (files.has(target.uri) && !options?.overwrite)
      throw new Error("target exists");
    files.set(target.uri, { ...item });
  }
}

export const Paths = {
  cache: new Directory("file:///cache"),
  document: new Directory("file:///document"),
};
export const __files = files;
export const __directories = directories;
