export const DEFAULT_DESKTOP_WIDTH = 1920
export const DEFAULT_DESKTOP_HEIGHT = 1080

export function workspaceDesktopSize(
  workspace?: {
    desktop_width?: number | null
    desktop_height?: number | null
  } | null,
): { width: number; height: number } {
  return {
    width: workspace?.desktop_width || DEFAULT_DESKTOP_WIDTH,
    height: workspace?.desktop_height || DEFAULT_DESKTOP_HEIGHT,
  }
}

export function desktopIframeSrc(
  base: string,
  proxyUrl: string,
  token: string,
  intent?: { viewer_client_id: string; intent_revision: number },
): string {
  const params = new URLSearchParams({
    token,
    resize: 'scale',
  })
  if (intent) {
    params.set('viewer_client_id', intent.viewer_client_id)
    params.set('intent_revision', String(intent.intent_revision))
  }
  const [path, query = ''] = proxyUrl.split('?')
  const merged = new URLSearchParams(query)
  params.forEach((value, key) => merged.set(key, value))
  // KasmVNC reads `path` for its websocket URL. Associate both requests.
  if (intent) {
    const socketPath =
      merged.get('path') || `${path!.replace(/^\//, '')}?token=${encodeURIComponent(token)}`
    const [socketBase, socketQuery = ''] = socketPath.split('?')
    const socketParams = new URLSearchParams(socketQuery)
    socketParams.set('viewer_client_id', intent.viewer_client_id)
    socketParams.set('intent_revision', String(intent.intent_revision))
    merged.set('path', `${socketBase}?${socketParams}`)
  }
  return `${base}${path}?${merged}`
}

/**
 * CSS width for the desktop modal content: the largest width that keeps
 * the aspect-ratio viewport plus header inside the window with a minimal
 * margin.
 */
export function desktopModalWidthCss(desktopWidth: number, desktopHeight: number): string {
  const aspect = desktopWidth / Math.max(desktopHeight, 1)
  return `min(calc(100vw - 2rem), calc((100dvh - 4.5rem) * ${aspect}))`
}
