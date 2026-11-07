import { createLocalFiles, type LocalFiles } from "../platform/localFiles";

export { filePath, joinUri, temporaryUri } from "../platform/localFiles";

/**
 * The file operations the app installer needs, addressed by `file://` URI.
 * The Expo implementation below is the only one in the app; tests use an
 * in-memory double.
 */
export type AppFileSystem = Omit<LocalFiles, "copy">;

/** Installed apps live in the app's documents, so the OS keeps them. */
export function createExpoAppFileSystem(): AppFileSystem {
  return createLocalFiles("opencuria-apps");
}
