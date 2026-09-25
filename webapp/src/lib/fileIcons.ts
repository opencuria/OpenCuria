/**
 * fileIcons — VS Code-like file/folder icon resolution backed by a small
 * curated subset of `material-icon-theme` (MIT, by Material Extensions).
 *
 * Only the SVGs under `src/assets/file-icons/` are bundled (~147 files).
 * This module is pure (no Vite glob, no DOM) so it is unit-testable; the
 * `WorkspaceFileIcon` component maps the returned keys to the bundled SVGs.
 *
 * Upstream: https://github.com/material-extensions/vscode-material-icon-theme
 * License: MIT (see THIRD_PARTY_NOTICES.md and webapp attribution).
 */

export const DEFAULT_FILE_ICON = 'file'
export const DEFAULT_FOLDER_ICON = 'folder'
export const DEFAULT_FOLDER_OPEN_ICON = 'folder-open'

/** Basename (lower-cased) → bundled SVG key (without `.svg`). */
const FILE_NAME_ICONS: Record<string, string> = {
  'package.json': 'nodejs',
  'package-lock.json': 'nodejs',
  '.nvmrc': 'nodejs',
  '.node-version': 'nodejs',
  'go.mod': 'go-mod',
  'go.sum': 'go-mod',
  'requirements.txt': 'python-misc',
  pipfile: 'python-misc',
  'pipfile.lock': 'python-misc',
  'pyproject.toml': 'python-misc',
  'poetry.lock': 'python-misc',
  gemfile: 'gemfile',
  'gemfile.lock': 'gemfile',
  podfile: 'ruby',
  'podfile.lock': 'ruby',
  'pom.xml': 'maven',
  'build.gradle': 'gradle',
  'settings.gradle': 'gradle',
  'pnpm-lock.yaml': 'pnpm',
  'yarn.lock': 'yarn',
  '.gitignore': 'git',
  '.gitattributes': 'git',
  '.gitmodules': 'git',
  '.gitconfig': 'git',
  '.editorconfig': 'editorconfig',
  'docker-compose.yml': 'docker',
  'docker-compose.yaml': 'docker',
  'compose.yml': 'docker',
  'compose.yaml': 'docker',
  '.dockerignore': 'docker',
  'nginx.conf': 'nginx',
  'tsconfig.json': 'tsconfig',
  '.env': 'tune',
  '.env.example': 'tune',
  '.env.local': 'tune',
}

/** Lower-cased extension (no dot) → bundled SVG key. */
const FILE_EXTENSION_ICONS: Record<string, string> = {
  ts: 'typescript',
  mts: 'typescript',
  cts: 'typescript',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'react',
  tsx: 'react_ts',
  vue: 'vue',
  svelte: 'svelte',
  astro: 'astro',
  mdx: 'mdx',
  md: 'markdown',
  markdown: 'markdown',
  mkd: 'markdown',
  py: 'python',
  pyw: 'python',
  pyi: 'python',
  go: 'go',
  rs: 'rust',
  java: 'java',
  c: 'c',
  h: 'h',
  hpp: 'hpp',
  hh: 'hpp',
  hxx: 'hpp',
  cpp: 'cpp',
  cc: 'cpp',
  cxx: 'cpp',
  'c++': 'cpp',
  cs: 'csharp',
  php: 'php',
  rb: 'ruby',
  swift: 'swift',
  kt: 'kotlin',
  kts: 'kotlin',
  dart: 'dart',
  lua: 'lua',
  r: 'r',
  scala: 'scala',
  sc: 'scala',
  groovy: 'groovy',
  pl: 'prolog',
  pm: 'perl',
  ex: 'elixir',
  exs: 'elixir',
  erl: 'erlang',
  clj: 'clojure',
  cljs: 'clojure',
  hs: 'haskell',
  pas: 'pascal',
  d: 'd',
  nim: 'nim',
  zig: 'zig',
  ps1: 'powershell',
  psm1: 'powershell',
  psd1: 'powershell',
  sh: 'console',
  bash: 'console',
  zsh: 'console',
  fish: 'console',
  ksh: 'console',
  csh: 'console',
  bat: 'console',
  cmd: 'console',
  html: 'html',
  htm: 'html',
  css: 'css',
  scss: 'sass',
  sass: 'sass',
  less: 'less',
  json: 'json',
  jsonc: 'json',
  json5: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  xml: 'xml',
  svg: 'svg',
  sql: 'database',
  sqlite: 'database',
  sqlite3: 'database',
  db: 'database',
  mdb: 'database',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  gif: 'image',
  bmp: 'image',
  webp: 'image',
  avif: 'image',
  tiff: 'image',
  tif: 'image',
  ico: 'image',
  heic: 'image',
  heif: 'image',
  pdf: 'pdf',
  zip: 'zip',
  tar: 'zip',
  gz: 'zip',
  bz2: 'zip',
  xz: 'zip',
  '7z': 'zip',
  rar: 'zip',
  jar: 'zip',
  war: 'zip',
  log: 'log',
  txt: 'document',
  text: 'document',
  csv: 'table',
  tsv: 'table',
  ttf: 'font',
  otf: 'font',
  woff: 'font',
  woff2: 'font',
  eot: 'font',
  mp3: 'audio',
  wav: 'audio',
  flac: 'audio',
  aac: 'audio',
  mp4: 'video',
  webm: 'video',
  mov: 'video',
  avi: 'video',
  mkv: 'video',
  ogv: 'video',
  ogg: 'video',
  lock: 'lock',
  properties: 'settings',
  ini: 'settings',
  cfg: 'settings',
  conf: 'settings',
  env: 'tune',
  graphql: 'graphql',
  gql: 'graphql',
  prisma: 'prisma',
  gradle: 'gradle',
}

/**
 * Lower-cased folder basename → bundled folder base (without `-open`).
 * Every value must exist as `folder-<base>.svg` + `folder-<base>-open.svg`
 * under `src/assets/file-icons/`.
 */
const FOLDER_NAME_ICONS: Record<string, string> = {
  src: 'folder-src',
  source: 'folder-src',
  sources: 'folder-src',
  docs: 'folder-docs',
  doc: 'folder-docs',
  documentation: 'folder-docs',
  help: 'folder-docs',
  test: 'folder-test',
  tests: 'folder-test',
  __tests__: 'folder-test',
  spec: 'folder-test',
  specs: 'folder-test',
  cypress: 'folder-cypress',
  coverage: 'folder-coverage',
  e2e: 'folder-coverage',
  '.nyc_output': 'folder-coverage',
  node_modules: 'folder-node',
  '.git': 'folder-git',
  '.github': 'folder-github',
  dist: 'folder-dist',
  out: 'folder-dist',
  build: 'folder-dist',
  release: 'folder-dist',
  target: 'folder-dist',
  public: 'folder-public',
  www: 'folder-public',
  wwwroot: 'folder-public',
  web: 'folder-public',
  config: 'folder-config',
  configs: 'folder-config',
  configuration: 'folder-config',
  settings: 'folder-config',
  '.config': 'folder-config',
  '.vscode': 'folder-vscode',
  components: 'folder-components',
  assets: 'folder-resource',
  static: 'folder-resource',
  resources: 'folder-resource',
  res: 'folder-resource',
  lib: 'folder-lib',
  libs: 'folder-lib',
  vendor: 'folder-lib',
  'third-party': 'folder-lib',
  third_party: 'folder-lib',
  deps: 'folder-lib',
  scripts: 'folder-scripts',
  server: 'folder-server',
  servers: 'folder-server',
  backend: 'folder-server',
  client: 'folder-client',
  clients: 'folder-client',
  frontend: 'folder-client',
  app: 'folder-app',
  apps: 'folder-app',
  api: 'folder-api',
  apis: 'folder-api',
  images: 'folder-images',
  image: 'folder-images',
  img: 'folder-images',
  icons: 'folder-images',
  icon: 'folder-images',
  pictures: 'folder-images',
  pics: 'folder-images',
  photos: 'folder-images',
  photo: 'folder-images',
  fonts: 'folder-font',
  font: 'folder-font',
  styles: 'folder-css',
  style: 'folder-css',
  css: 'folder-css',
  scss: 'folder-css',
  sass: 'folder-css',
  less: 'folder-css',
  videos: 'folder-video',
  video: 'folder-video',
  audio: 'folder-audio',
  audios: 'folder-audio',
  sounds: 'folder-audio',
  sound: 'folder-audio',
  music: 'folder-audio',
  temp: 'folder-temp',
  tmp: 'folder-temp',
  temporary: 'folder-temp',
  cache: 'folder-temp',
  '.cache': 'folder-temp',
  logs: 'folder-log',
  log: 'folder-log',
  packages: 'folder-packages',
  tools: 'folder-tools',
}

export interface FileIconTarget {
  path?: string | null
  name?: string | null
  directory?: boolean
  expanded?: boolean
}

/** Last path segment, ignoring trailing slashes. */
export function basenameOf(path: string): string {
  const trimmed = path.replace(/\/+$/, '')
  if (!trimmed) return ''
  const slash = trimmed.lastIndexOf('/')
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}

function displayName(target: FileIconTarget): string {
  if (target.name) return target.name
  if (target.path) return basenameOf(target.path)
  return ''
}

/** Exact-name + prefix rules (case-insensitive) → icon key, if any. */
function matchFileName(lower: string): string | null {
  const exact = FILE_NAME_ICONS[lower]
  if (exact) return exact

  if (lower === 'dockerfile' || lower.startsWith('dockerfile.')) return 'docker'
  if (lower === 'containerfile' || lower.startsWith('containerfile.')) return 'docker'
  if (lower === 'makefile' || lower === 'gnumakefile') return 'makefile'
  if (lower === 'justfile' || lower === '.justfile') return 'just'
  if (lower === 'readme' || lower.startsWith('readme.')) return 'readme'
  if (
    lower === 'changelog' ||
    lower.startsWith('changelog.') ||
    lower === 'changes' ||
    lower.startsWith('changes.')
  ) {
    return 'changelog'
  }
  for (const base of ['license', 'licence', 'copying', 'copyright']) {
    if (lower === base || lower.startsWith(`${base}.`)) return 'license'
  }
  if (lower === 'tsconfig.json' || lower.startsWith('tsconfig.')) return 'tsconfig'
  if (lower.startsWith('vite.config.')) return 'vite'
  if (lower.startsWith('vitest.')) return 'vitest'
  if (lower.startsWith('vue.config.')) return 'vue-config'
  if (lower.startsWith('docker-compose.') || lower.startsWith('compose.')) return 'docker'
  if (lower === '.env' || lower.startsWith('.env.')) return 'tune'
  if (lower === '.eslintrc' || lower.startsWith('.eslintrc.') || lower.startsWith('eslint.config.')) {
    return 'eslint'
  }
  if (
    lower === '.prettierrc' ||
    lower.startsWith('.prettierrc.') ||
    lower.startsWith('prettier.config.')
  ) {
    return 'prettier'
  }
  return null
}

/** Resolve a file basename to its bundled SVG key (no `.svg`). */
export function resolveFileIconKey(target: FileIconTarget): string {
  const base = displayName(target)
  if (!base) return DEFAULT_FILE_ICON
  const lower = base.toLowerCase()

  const byName = matchFileName(lower)
  if (byName) return byName

  // Dotfiles like `.gitignore` have no extension; exact names are handled
  // above, so only look for an extension after the leading dot.
  const dot = lower.lastIndexOf('.')
  if (dot > 0 && dot < lower.length - 1) {
    const ext = lower.slice(dot + 1)
    const byExt = FILE_EXTENSION_ICONS[ext]
    if (byExt) return byExt
  }
  return DEFAULT_FILE_ICON
}

/** Resolve a folder to its bundled SVG key (includes `-open` when expanded). */
export function resolveFolderIconKey(target: FileIconTarget): string {
  const base = displayName(target)
  const lower = base.toLowerCase()
  const folderBase = (lower && FOLDER_NAME_ICONS[lower]) || DEFAULT_FOLDER_ICON
  if (target.expanded) {
    return folderBase === DEFAULT_FOLDER_ICON ? DEFAULT_FOLDER_OPEN_ICON : `${folderBase}-open`
  }
  return folderBase
}

/** Unified entry point used by `WorkspaceFileIcon`. */
export function resolveWorkspaceIconKey(target: FileIconTarget): string {
  if (target.directory) return resolveFolderIconKey(target)
  return resolveFileIconKey(target)
}
