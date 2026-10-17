/** Browser File and drag/drop transport helpers for workspace chat uploads. */

export {
  appendUploadMentions,
  buildUploadMentionToken,
  CHAT_UPLOAD_DIR,
  collectUploadDirFilenames,
  findWorkspaceTreeNode,
  hasTreePath,
  hasUploadMention,
  nextChatUploadRequestId,
  resolveUniqueFilename,
  sanitizeUploadFilename,
  uploadTargetPath,
} from './chatUploads'
export { UPLOAD_MAX_BYTES } from './fileChunks'

/** Encode a browser File as base64 (same arrayBuffer → btoa path as FileUploadZone). */
export async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]!)
  }
  return btoa(binary)
}

/** True when the drag carries files (used to ignore text selections). */
export function isFileDrag(event: DragEvent): boolean {
  const types = event.dataTransfer?.types
  if (!types) return false
  return Array.from(types).includes('Files')
}

/** Extract dropped files; empty for folder drops and non-file drags. */
export function getDroppedFiles(event: DragEvent): File[] {
  const files = event.dataTransfer?.files
  if (!files || files.length === 0) return []
  return Array.from(files)
}
