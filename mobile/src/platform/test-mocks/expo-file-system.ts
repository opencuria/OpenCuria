const files = new Map<string, { data: string; encoding: string }>();
const directories = new Set<string>();

function uriOf(...parts: Array<string | Directory>): string {
  return parts
    .map((part) =>
      part instanceof Directory ? part.uri.replace(/\/$/, "") : part,
    )
    .join("/");
}

export class Directory {
  readonly uri: string;
  constructor(...parts: Array<string | Directory>) {
    this.uri = uriOf(...parts);
  }
  get exists(): boolean {
    return directories.has(this.uri);
  }
  create(_options?: { intermediates?: boolean; idempotent?: boolean }): void {
    directories.add(this.uri);
  }
  delete(): void {
    directories.delete(this.uri);
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
      directories.add(this.uri.slice(0, this.uri.lastIndexOf("/")));
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
}

export const Paths = { cache: new Directory("file:///cache") };
export const __files = files;
export const __directories = directories;
