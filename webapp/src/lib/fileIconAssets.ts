import { resolveFileIconKey } from '@/lib/fileIcons'

const iconUrlModules = import.meta.glob<string>('../assets/file-icons/*.svg', {
  eager: true,
  query: '?url',
  import: 'default',
})

const iconUrls: Record<string, string> = {}
for (const [modulePath, url] of Object.entries(iconUrlModules)) {
  const match = modulePath.match(/([^/]+)\.svg$/)
  if (match?.[1]) iconUrls[match[1]] = url
}

/**
 * Bundled file-icon URL for a workspace path (decorative badge icons).
 *
 * Shared helper so badge/chip renderers don't each duplicate the
 * `src/assets/file-icons/*.svg` glob. Falls back to the generic `file`
 * icon; returns empty string when no bundle is available.
 */
export function workspaceIconUrl(key: string, directory = false, expanded = false): string {
  return iconUrls[key] ?? iconUrls[directory ? (expanded ? 'folder-open' : 'folder') : 'file'] ?? ''
}

export function workspaceFileIconUrl(path: string): string {
  return workspaceIconUrl(resolveFileIconKey({ path }))
}
