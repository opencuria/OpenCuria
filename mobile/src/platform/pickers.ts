import { File } from "expo-file-system";
import * as DocumentPicker from "expo-document-picker";
import * as ImageManipulator from "expo-image-manipulator";
import * as ImagePicker from "expo-image-picker";
import {
  CHAT_UPLOAD_DIR,
  resolveUniqueFilename,
  sanitizeUploadFilename,
  uploadTargetPath,
} from "@opencuria/chat-core/chatUploads";
import {
  decodedBase64Size,
  UPLOAD_MAX_BYTES,
} from "@opencuria/chat-core/fileChunks";
import type { OpenCuriaClient } from "@opencuria/client";
import type { ComposerAttachment } from "../components/ChatComposer";

const MAX_IMAGE_WIDTH = 2048;
const pickedTemporaryUris = new Set<string>();

export class NativePickerError extends Error {
  readonly code: string;

  constructor(message: string, code = "picker_error") {
    super(message);
    this.name = "NativePickerError";
    this.code = code;
  }
}

/** Open the camera or photo library and return native composer attachments. */
export async function pickPhoto(
  source: "camera" | "library",
): Promise<ComposerAttachment[]> {
  if (source === "camera") {
    const permission = await ImagePicker.requestCameraPermissionsAsync();
    if (!permission.granted) {
      throw new NativePickerError(
        permission.canAskAgain
          ? "Camera permission is required to take a photo."
          : "Camera access is disabled. Enable it in your device settings to take a photo.",
        "camera_permission_denied",
      );
    }
  } else {
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      throw new NativePickerError(
        permission.canAskAgain
          ? "Photo library permission is required to choose a photo."
          : "Photo library access is disabled. Enable it in your device settings to choose a photo.",
        "photo_permission_denied",
      );
    }
  }

  const result =
    source === "camera"
      ? await ImagePicker.launchCameraAsync({
          mediaTypes: ["images"],
          quality: 1,
        })
      : await ImagePicker.launchImageLibraryAsync({
          mediaTypes: ["images"],
          allowsMultipleSelection: true,
          quality: 1,
        });
  if (result.canceled || !result.assets?.length) return [];

  const attachments: ComposerAttachment[] = [];
  for (const asset of result.assets)
    attachments.push(await photoAttachment(asset));
  return attachments;
}

/** Pick multiple documents, copied into app cache for immediate content:// access. */
export async function pickDocuments(): Promise<ComposerAttachment[]> {
  const result = await DocumentPicker.getDocumentAsync({
    type: "*/*",
    multiple: true,
    copyToCacheDirectory: true,
  });
  if (result.canceled || !result.assets?.length) return [];

  return result.assets.map((asset) => ({
    id: randomId(),
    uri: asset.uri,
    name: asset.name || "attachment",
    mimeType: asset.mimeType,
    size: asset.size,
    kind: asset.mimeType?.startsWith("image/")
      ? "image"
      : asset.mimeType?.startsWith("video/")
        ? "video"
        : "file",
  }));
}

/** Read and upload picked composer files via the authenticated workspace socket. */
export async function uploadAttachments(
  client: Pick<OpenCuriaClient, "files">,
  workspaceId: string,
  attachments: ComposerAttachment[],
): Promise<string[]> {
  if (!workspaceId)
    throw new TypeError("A workspace is required to upload attachments.");
  if (attachments.length === 0) return [];

  const prepared = attachments.map((attachment) => {
    if (!attachment.uri)
      throw new NativePickerError(
        "An attachment has no local file URI.",
        "invalid_file",
      );
    const file = new File(attachment.uri);
    const size = file.size;
    if (!file.exists || !Number.isFinite(size) || size < 0) {
      throw new NativePickerError(
        `Could not read ${attachment.name || "the selected file"}.`,
        "file_unavailable",
      );
    }
    // Inspect every size before any base64 representation is allocated.
    if (size > UPLOAD_MAX_BYTES) {
      throw new NativePickerError(
        `${attachment.name || "This file"} exceeds the 10 MiB upload limit.`,
        "size_limit",
      );
    }
    return { attachment, file, size };
  });

  // Do not retry after arbitrary listing failures: otherwise a listing outage
  // could make us overwrite a user's prior upload with the same name.
  const existing = await existingUploadNames(client, workspaceId);
  const paths: string[] = [];
  for (const { attachment, file, size } of prepared) {
    const sourceName = sanitizeUploadFilename(
      attachment.name || file.name || "upload",
    );
    // UUID prefix prevents races between concurrent uploads whose directory
    // listings began at the same time while retaining the source basename.
    const uniqueName = resolveUniqueFilename(
      existing,
      `${randomId().slice(0, 8)}_${sourceName}`,
    );
    const base64 = await file.base64();
    const actualBytes = decodedBase64SizeSafe(base64);
    if (actualBytes !== size || actualBytes > UPLOAD_MAX_BYTES) {
      throw new NativePickerError(
        `${attachment.name || "This file"} changed or exceeds the 10 MiB upload limit.`,
        "size_limit",
      );
    }
    const result = await client.files.uploadFile(workspaceId, {
      directory: CHAT_UPLOAD_DIR,
      filename: uniqueName,
      base64,
    });
    if (result.status !== "success") {
      throw new NativePickerError(
        result.error || "The file upload failed.",
        "upload_failed",
      );
    }
    paths.push(uploadTargetPath(uniqueName));
  }
  return paths;
}

interface ImageAsset {
  uri: string;
  fileName?: string | null;
  fileSize?: number;
  mimeType?: string | null;
  type?: string | null;
  width?: number;
}

async function photoAttachment(asset: ImageAsset): Promise<ComposerAttachment> {
  if (!asset.uri)
    throw new NativePickerError(
      "The photo picker returned no file.",
      "file_unavailable",
    );
  let uri = asset.uri;
  let name = asset.fileName || `photo-${randomId().slice(0, 8)}`;
  let mimeType = asset.mimeType || mimeFromName(name) || "image/jpeg";
  const pickedFile = new File(uri);
  const actualSize = pickedFile.size || asset.fileSize || 0;
  if (actualSize <= 0)
    throw new NativePickerError(
      "The selected photo could not be read.",
      "file_unavailable",
    );

  if (
    mimeType.toLowerCase() === "image/heic" ||
    mimeType.toLowerCase() === "image/heif" ||
    /\.(?:heic|heif)$/i.test(name)
  ) {
    const converted = await convertImageToJpeg(uri, asset.width);
    uri = converted.uri;
    name = `${withoutExtension(name)}.jpg`;
    mimeType = "image/jpeg";
  } else if (actualSize > UPLOAD_MAX_BYTES) {
    if (!mimeType.toLowerCase().startsWith("image/")) {
      throw new NativePickerError(
        "This photo is larger than the 10 MiB upload limit.",
        "size_limit",
      );
    }
    const converted = await convertImageToJpeg(uri, asset.width);
    uri = converted.uri;
    name = `${withoutExtension(name)}.jpg`;
    mimeType = "image/jpeg";
  }

  const file = new File(uri);
  const size = file.size || actualSize;
  return {
    id: randomId(),
    uri,
    name,
    mimeType,
    size,
    kind: asset.type === "video" ? "video" : "image",
  };
}

async function convertImageToJpeg(
  sourceUri: string,
  sourceWidth?: number,
): Promise<{ uri: string; size: number }> {
  const initialWidth =
    sourceWidth && sourceWidth > 0
      ? Math.min(MAX_IMAGE_WIDTH, sourceWidth)
      : MAX_IMAGE_WIDTH;
  const attempts = [
    { width: initialWidth, quality: 0.82 },
    { width: initialWidth, quality: 0.65 },
    { width: Math.min(initialWidth, 1600), quality: 0.72 },
    { width: Math.min(initialWidth, 1280), quality: 0.68 },
    { width: Math.min(initialWidth, 1024), quality: 0.64 },
    { width: Math.min(initialWidth, 768), quality: 0.6 },
    { width: Math.min(initialWidth, 512), quality: 0.55 },
    { width: Math.min(initialWidth, 384), quality: 0.5 },
    { width: Math.min(initialWidth, 256), quality: 0.45 },
  ];
  for (const attempt of attempts) {
    const context = ImageManipulator.ImageManipulator.manipulate(sourceUri);
    context.resize({ width: Math.min(MAX_IMAGE_WIDTH, attempt.width) });
    const image = await context.renderAsync();
    const result = await image.saveAsync({
      format: ImageManipulator.SaveFormat.JPEG,
      compress: attempt.quality,
    });
    const converted = new File(result.uri);
    const size = converted.size;
    if (size > 0 && size <= UPLOAD_MAX_BYTES) {
      pickedTemporaryUris.add(result.uri);
      return { uri: result.uri, size };
    }
    if (converted.exists) converted.delete();
  }
  throw new NativePickerError(
    "The photo could not be converted below the 10 MiB upload limit.",
    "size_limit",
  );
}

/** Delete temporary converted image outputs created by this adapter, not picker originals. */
export function clearPickedPhotoCache(): void {
  for (const uri of pickedTemporaryUris) {
    const file = new File(uri);
    if (file.exists) file.delete();
  }
  pickedTemporaryUris.clear();
}

export async function existingUploadNames(
  client: Pick<OpenCuriaClient, "files">,
  workspaceId: string,
): Promise<Set<string>> {
  try {
    const result = await client.files.listFiles(workspaceId, CHAT_UPLOAD_DIR);
    if (result.error) {
      if (isExplicitMissingUploadDirectory(result.error)) return new Set();
      throw new NativePickerError(result.error, "listing_failed");
    }
    if (!Array.isArray(result.entries)) {
      throw new NativePickerError(
        "The upload directory could not be listed safely.",
        "listing_failed",
      );
    }
    return new Set(
      (result.entries ?? []).map((entry) => entry.name).filter(Boolean),
    );
  } catch (error) {
    if (isExplicitMissingUploadDirectory(error)) return new Set();
    throw error;
  }
}

function isExplicitMissingUploadDirectory(error: unknown): boolean {
  // Runner errors carry raw `find` stderr including its trailing newline.
  const message = (
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : ""
  ).trim();
  return (
    /^failed to list files:.*(?:no such file or directory|enoent).*$/i.test(
      message,
    ) && message.includes(CHAT_UPLOAD_DIR)
  );
}

function decodedBase64SizeSafe(base64: string): number {
  try {
    return decodedBase64Size(base64.replace(/\s+/g, ""));
  } catch {
    throw new NativePickerError(
      "The selected file contains invalid data.",
      "invalid_payload",
    );
  }
}

function mimeFromName(name: string): string | null {
  const ext = name.split(".").pop()?.toLowerCase();
  const types: Record<string, string> = {
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    png: "image/png",
    gif: "image/gif",
    webp: "image/webp",
    avif: "image/avif",
    heic: "image/heic",
    heif: "image/heif",
  };
  return ext ? (types[ext] ?? null) : null;
}

function withoutExtension(name: string): string {
  const index = name.lastIndexOf(".");
  return index > 0 ? name.slice(0, index) : name;
}

function randomId(): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid) return uuid;
  } catch {
    // Use a non-identifying fallback on older native runtimes.
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
