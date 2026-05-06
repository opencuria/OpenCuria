export type WorkspaceFileKind = 'image' | 'video' | 'text' | 'binary'

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

export function extractWorkspacePathReferences(markdown: string): WorkspacePathReference[] {
  const refs: WorkspacePathReference[] = []
  const re = /(!?)\[([^\]]*)\]\((\/workspace\/[^)\s]+(?: [^)]+)?)\)/g
  let match: RegExpExecArray | null

  while ((match = re.exec(markdown)) !== null) {
    const isMediaMarkdown = match[1] === '!'
    const label = (match[2] ?? '').trim()
    const rawPath = (match[3] ?? '').trim()
    const path = rawPath.replace(/^<|>$/g, '')
    if (!path.startsWith('/workspace/')) continue
    refs.push({ path, label, isMediaMarkdown })
  }

  return refs
}

