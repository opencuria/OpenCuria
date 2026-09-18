export type WorkspaceFileKind = 'image' | 'video' | 'text' | 'binary'

export const WORKSPACE_ROOT = '/workspace'

const MARKDOWN_LINK_RE = /(!?)\[([^\]]*)\]\(([^)]+)\)/g

const REMOTE_DEST_RE = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i

const IMAGE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif', 'tiff', 'tif',
])

const VIDEO_EXTENSIONS = new Set([
  'mp4', 'webm', 'ogg', 'ogv', 'mov', 'm4v', 'avi', 'mkv', 'wmv', 'flv', 'mpeg',
  'mpg', '3gp', '3g2', 'ts', 'm2ts',
])

const TEXT_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'csv', 'tsv', 'log', 'json', 'yaml', 'yml', 'xml', 'html', 'htm',
  'css', 'scss', 'less', 'js', 'jsx', 'ts', 'tsx', 'mjs', 'cjs', 'vue', 'py', 'go', 'rs', 'java',
  'kt', 'kts', 'swift', 'rb', 'php', 'c', 'h', 'cpp', 'hpp', 'cs', 'sh', 'bash', 'zsh', 'fish',
  'sql', 'ini', 'conf', 'cfg', 'toml', 'env', 'gitignore', 'dockerfile', 'makefile',
])

const TEXT_MIME_PREFIXES = ['text/']
const TEXT_MIMES = new Set([
  'application/json',
  'application/ld+json',
  'application/xml',
  'application/x-yaml',
  'application/yaml',
  'application/javascript',
  'application/x-javascript',
  'application/typescript',
  'application/sql',
  'application/x-sh',
  'image/svg+xml',
])

export type WorkspacePathReference = {
  path: string
  label: string
  isMediaMarkdown: boolean
}

export function getWorkspaceFileExtension(nameOrPath: string): string {
  const filename = nameOrPath.split('/').pop() ?? nameOrPath
  const dot = filename.lastIndexOf('.')
  if (dot < 0) {
    const special = filename.toLowerCase()
    if (special === 'dockerfile' || special === 'makefile') return special
    return ''
  }
  return filename.slice(dot + 1).toLowerCase()
}

export function classifyWorkspaceFile(
  nameOrPath: string,
  mimeType?: string,
): WorkspaceFileKind {
  const ext = getWorkspaceFileExtension(nameOrPath)
  const normalizedMime = (mimeType ?? '').toLowerCase()

  if (normalizedMime.startsWith('image/')) return 'image'
  if (normalizedMime.startsWith('video/')) return 'video'

  if (IMAGE_EXTENSIONS.has(ext)) return 'image'
  if (VIDEO_EXTENSIONS.has(ext)) return 'video'

  if (normalizedMime) {
    if (TEXT_MIME_PREFIXES.some((prefix) => normalizedMime.startsWith(prefix))) return 'text'
    if (TEXT_MIMES.has(normalizedMime)) return 'text'
  }

  if (TEXT_EXTENSIONS.has(ext)) return 'text'
  return 'binary'
}

export function buildWorkspaceReferenceMarkdown(
  filename: string,
  path: string,
  kind: WorkspaceFileKind,
): string {
  return kind === 'image' || kind === 'video'
    ? `![${filename}](${path})`
    : `[${filename}](${path})`
}

/** Strip angle-bracket wrapping and an optional markdown title from a dest. */
export function parseMarkdownLinkDest(raw: string): string {
  let dest = raw.trim()
  if (dest.startsWith('<')) {
    const close = dest.indexOf('>')
    if (close >= 0) return dest.slice(1, close).trim()
    dest = dest.slice(1).trimStart()
  }
  const space = dest.search(/\s/)
  if (space >= 0) return dest.slice(0, space)
  return dest
}

function posixNormalize(path: string): string {
  const absolute = path.startsWith('/')
  const out: string[] = []
  for (const part of path.split('/')) {
    if (!part || part === '.') continue
    if (part === '..') {
      if (out.length > 0) out.pop()
      continue
    }
    out.push(part)
  }
  const joined = out.join('/')
  if (absolute) return `/${joined}`
  return joined
}

/**
 * Resolve a markdown image/link dest to a sandboxed `/workspace/...` path.
 * Remote URLs and sandbox escapes return null.
 */
export function resolveWorkspaceMediaPath(rawDest: string): string | null {
  const dest = parseMarkdownLinkDest(rawDest)
  if (!dest || REMOTE_DEST_RE.test(dest)) return null
  const candidate = dest.startsWith('/') ? dest : `${WORKSPACE_ROOT}/${dest}`
  const normalized = posixNormalize(candidate)
  if (normalized !== WORKSPACE_ROOT && !normalized.startsWith(`${WORKSPACE_ROOT}/`)) {
    return null
  }
  return normalized
}

export function extractWorkspacePathReferences(markdown: string): WorkspacePathReference[] {
  const refs: WorkspacePathReference[] = []
  MARKDOWN_LINK_RE.lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = MARKDOWN_LINK_RE.exec(markdown)) !== null) {
    const path = resolveWorkspaceMediaPath(match[3] ?? '')
    if (!path) continue
    refs.push({
      path,
      label: (match[2] ?? '').trim(),
      isMediaMarkdown: match[1] === '!',
    })
  }

  return refs
}

