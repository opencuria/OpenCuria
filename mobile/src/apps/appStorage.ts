import { Directory, File, Paths } from "expo-file-system";

/**
 * The file operations the app installer needs, addressed by `file://` URI.
 * The Expo implementation below is the only one in the app; tests use an
 * in-memory double.
 */
export interface AppFileSystem {
  /** URI of the directory that holds every installed app. */
  readonly root: string;
  exists(uri: string): boolean;
  readText(uri: string): Promise<string>;
  /** Write a whole file, replacing it only once the new content is complete. */
  writeText(uri: string, text: string): void;
  makeDirectory(uri: string): void;
  /** Delete a file or a directory with its contents; missing is fine. */
  remove(uri: string): void;
  /** Names of the entries in a directory; empty when it is missing. */
  list(uri: string): string[];
  move(from: string, to: string): void;
  size(uri: string): number;
  md5(uri: string): string | null;
  download(url: string, headers: Record<string, string>, to: string): Promise<void>;
}

/** Strip the scheme for native code that expects a file system path. */
export function filePath(uri: string): string {
  return decodeURI(uri.replace(/^file:\/\//, ""));
}

export function joinUri(base: string, ...parts: string[]): string {
  return [base.replace(/\/+$/, ""), ...parts].join("/");
}

function isDirectory(uri: string): boolean {
  return new Directory(uri).exists;
}

/** Installed apps live in the app's documents, so the OS keeps them. */
export function createExpoAppFileSystem(): AppFileSystem {
  const root = joinUri(Paths.document.uri, "opencuria-apps");
  return {
    root,
    exists: (uri) => new File(uri).exists || isDirectory(uri),
    readText: (uri) => new File(uri).text(),
    writeText: (uri, text) => {
      const target = new File(uri);
      const temporary = new File(`${uri}.tmp`);
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
