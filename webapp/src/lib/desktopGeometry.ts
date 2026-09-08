export const DEFAULT_DESKTOP_WIDTH = 1920
export const DEFAULT_DESKTOP_HEIGHT = 1080

export function workspaceDesktopSize(workspace?: {
  desktop_width?: number | null
  desktop_height?: number | null
} | null): { width: number; height: number } {
  return {
    width: workspace?.desktop_width || DEFAULT_DESKTOP_WIDTH,
    height: workspace?.desktop_height || DEFAULT_DESKTOP_HEIGHT,
  }
}

export function desktopIframeSrc(base: string, proxyUrl: string, token: string): string {
  const params = new URLSearchParams({
    token,
    resize: 'scale',
  })
  return `${base}${proxyUrl}?${params.toString()}`
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
