import { Directory, File, Paths } from "expo-file-system";

/**
 * File operations below one directory of the app's own storage, addressed by
 * `file://` URI. The Expo implementation below is the only one in the app;
 * tests may use in-memory doubles.
 */
export interface LocalFiles {
  /** URI of the directory that holds every file of this store. */
  readonly root: string;
  exists(uri: string): boolean;
  readText(uri: string): Promise<string>;
  /**
   * Write a whole file, replacing it only once the new content is complete.
   * Until the replacement finished, the new content is at `temporaryUri`.
   */
  writeText(uri: string, text: string): void;
  makeDirectory(uri: string): void;
  /** Delete a file or a directory with its contents; missing is fine. */
  remove(uri: string): void;
  /** Names of the entries in a directory; empty when it is missing. */
  list(uri: string): string[];
  move(from: string, to: string): void;
  /** Copy a file, replacing the target. */
  copy(from: string, to: string): void;
  size(uri: string): number;
  md5(uri: string): string | null;
  download(
    url: string,
    headers: Record<string, string>,
    to: string,
  ): Promise<void>;
}

/** Strip the scheme for native code that expects a file system path. */
export function filePath(uri: string): string {
  return decodeURI(uri.replace(/^file:\/\//, ""));
}

/** Where `writeText` stages a file's new content. */
export function temporaryUri(uri: string): string {
  return `${uri}.tmp`;
}

export function joinUri(base: string, ...parts: string[]): string {
  return [base.replace(/\/+$/, ""), ...parts].join("/");
}

function isDirectory(uri: string): boolean {
  return new Directory(uri).exists;
}

/**
 * Files in `name` below the app's documents, which the OS keeps, or below its
 * cache, which is never backed up and may be cleared when storage runs low:
 * the place for data that can be loaded again.
 */
export function createLocalFiles(
  name: string,
  location: "documents" | "cache" = "documents",
): LocalFiles {
  const base = location === "cache" ? Paths.cache : Paths.document;
  const root = joinUri(base.uri, name);
  return {
    root,
    exists: (uri) => new File(uri).exists || isDirectory(uri),
    readText: (uri) => new File(uri).text(),
    writeText: (uri, text) => {
      const target = new File(uri);
      const temporary = new File(temporaryUri(uri));
      if (temporary.exists) temporary.delete();
      temporary.create({ intermediates: true });
      temporary.write(text);
      temporary.moveSync(target, { overwrite: true });
    },
    makeDirectory: (uri) => {
      const directory = new Directory(uri);
      if (!directory.exists)
        directory.create({ intermediates: true, idempotent: true });
    },
    remove: (uri) => {
      const directory = new Directory(uri);
      if (directory.exists) {
        directory.delete();
        return;
      }
      const file = new File(uri);
      if (file.exists) file.delete();
    },
    list: (uri) => {
      const directory = new Directory(uri);
      return directory.exists
        ? directory.list().map((entry) => entry.name)
        : [];
    },
    move: (from, to) => {
      new File(from).moveSync(new File(to), { overwrite: true });
    },
    copy: (from, to) => {
      new File(from).copySync(new File(to), { overwrite: true });
    },
    size: (uri) => new File(uri).size,
    md5: (uri) => new File(uri).md5,
    download: async (url, headers, to) => {
      await File.downloadFileAsync(url, new File(to), {
        headers,
        idempotent: true,
      });
    },
  };
}
