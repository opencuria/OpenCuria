import { File, UploadType } from "expo-file-system";
import * as DocumentPicker from "expo-document-picker";
import * as ImageManipulator from "expo-image-manipulator";
import * as ImagePicker from "expo-image-picker";
import {
  CHAT_UPLOAD_DIR,
  sanitizeUploadFilename,
} from "@opencuria/chat-core/chatUploads";
import {
  canTransferFile,
  fileTransferLimitMessage,
} from "@opencuria/chat-core/workspaceFiles";
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

/**
 * Upload picked composer files from disk into the chat upload folder and
 * return their workspace paths. Names that exist get a suffix (`name_1.ext`)
 * from the server.
 */
export async function uploadAttachments(
  client: Pick<OpenCuriaClient, "files">,
  workspaceId: string,
  attachments: ComposerAttachment[],
): Promise<string[]> {
  if (!workspaceId)
    throw new TypeError("A workspace is required to upload attachments.");
  if (attachments.length === 0) return [];

  // Check every file before the first upload starts.
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
    if (!canTransferFile(size)) {
      throw new NativePickerError(
        `${attachment.name || "This file"} is too large. ${fileTransferLimitMessage()}`,
        "size_limit",
      );
    }
    return { attachment, file, size };
  });

  const paths: string[] = [];
  for (const { attachment, file, size } of prepared) {
    const target = await client.files.createUpload(workspaceId, {
      directory: CHAT_UPLOAD_DIR,
      filename: sanitizeUploadFilename(
        attachment.name || file.name || "upload",
      ),
      size,
      conflict: "rename",
    });
    const response = await file.upload(target.uploadUrl, {
      httpMethod: "PUT",
      uploadType: UploadType.BINARY_CONTENT,
      headers: { "Content-Type": "application/octet-stream" },
    });
    const path = uploadedPath(response);
    client.files.forget(workspaceId, path);
    paths.push(path);
  }
  return paths;
}

/** The workspace path of a finished upload (HTTP 201 `{path, size}`). */
function uploadedPath(response: { status: number; body: string }): string {
  let payload: { path?: unknown; detail?: unknown; code?: unknown } = {};
  try {
    payload = JSON.parse(response.body) as typeof payload;
  } catch {
    // Not JSON: report the status below.
  }
  if (response.status === 201 && typeof payload.path === "string")
    return payload.path;
  throw new NativePickerError(
    typeof payload.detail === "string" && payload.detail
      ? payload.detail
      : `The upload failed (${response.status}).`,
    typeof payload.code === "string" && payload.code
      ? payload.code
      : "upload_failed",
  );
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

  // Models cannot read HEIC.
  if (
    mimeType.toLowerCase() === "image/heic" ||
    mimeType.toLowerCase() === "image/heif" ||
    /\.(?:heic|heif)$/i.test(name)
  ) {
    uri = await convertImageToJpeg(uri, asset.width);
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
): Promise<string> {
  const context = ImageManipulator.ImageManipulator.manipulate(sourceUri);
  context.resize({
    width:
      sourceWidth && sourceWidth > 0
        ? Math.min(MAX_IMAGE_WIDTH, sourceWidth)
        : MAX_IMAGE_WIDTH,
  });
  const image = await context.renderAsync();
  const result = await image.saveAsync({
    format: ImageManipulator.SaveFormat.JPEG,
    compress: 0.82,
  });
  pickedTemporaryUris.add(result.uri);
  if (new File(result.uri).size <= 0)
    throw new NativePickerError(
      "The photo could not be converted.",
      "file_unavailable",
    );
  return result.uri;
}

/** Delete temporary converted image outputs created by this adapter, not picker originals. */
export function clearPickedPhotoCache(): void {
  for (const uri of pickedTemporaryUris) {
    const file = new File(uri);
    if (file.exists) file.delete();
  }
  pickedTemporaryUris.clear();
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
