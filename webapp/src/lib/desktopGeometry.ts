export {
  DEFAULT_DESKTOP_HEIGHT,
  DEFAULT_DESKTOP_WIDTH,
  buildDesktopViewerUrl as desktopIframeSrc,
  workspaceDesktopSize,
} from '@opencuria/chat-core/desktopSurface'

/**
 * CSS width for the desktop modal content: the largest width that keeps
 * the aspect-ratio viewport plus header inside the window with a minimal
 * margin.
 */
export function desktopModalWidthCss(desktopWidth: number, desktopHeight: number): string {
  const aspect = desktopWidth / Math.max(desktopHeight, 1)
  return `min(calc(100vw - 2rem), calc((100dvh - 4.5rem) * ${aspect}))`
}
