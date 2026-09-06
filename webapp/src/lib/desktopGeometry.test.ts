import { describe, expect, it } from 'vitest'

import {
  DEFAULT_DESKTOP_HEIGHT,
  DEFAULT_DESKTOP_WIDTH,
  desktopIframeSrc,
  workspaceDesktopSize,
} from './desktopGeometry'

describe('desktopGeometry', () => {
  it('defaults to 1920x1080', () => {
    expect(workspaceDesktopSize(null)).toEqual({
      width: DEFAULT_DESKTOP_WIDTH,
      height: DEFAULT_DESKTOP_HEIGHT,
    })
  })

  it('uses the workspace framebuffer size', () => {
    expect(
      workspaceDesktopSize({ desktop_width: 1280, desktop_height: 720 }),
    ).toEqual({ width: 1280, height: 720 })
  })

  it('asks KasmVNC to scale locally instead of resizing the desktop', () => {
    expect(desktopIframeSrc('http://ws.test', '/ws/desktop/ws-1/', 'tok')).toBe(
      'http://ws.test/ws/desktop/ws-1/?token=tok&resize=scale',
    )
  })
})
