import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const webappRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
const html = readFileSync(resolve(webappRoot, 'index.html'), 'utf8')

/** Hrefs of the `<link>` tags with the given rel. */
function linkHrefs(rel: string): string[] {
  return [...html.matchAll(/<link\b[^>]*>/g)]
    .map(([tag]) => tag)
    .filter((tag) => tag.includes(`rel="${rel}"`))
    .map((tag) => /href="([^"]+)"/.exec(tag)?.[1] ?? '')
}

describe('favicon assets', () => {
  it('draws the logo facets on a tile that follows the light and dark color scheme', () => {
    const svg = readFileSync(resolve(webappRoot, 'src/assets/brand/favicon.svg'), 'utf8')
    const [lightCss = '', darkCss = ''] = svg.split('@media (prefers-color-scheme: dark)')

    expect(svg).toContain('viewBox="0 0 64 64"')
    expect(svg).toMatch(/<rect class="oc-tile"[^>]*rx="[\d.]+"\/>/)
    expect(lightCss).toContain('.oc-tile { fill: #F7F7F8;')
    // Flat tiles in both schemes: the app icon has no background gradient.
    expect(darkCss).toContain('.oc-tile { fill: #0B0A14;')
    expect(svg).not.toContain('id="oc-bg"')
    for (const facet of ['18,23 32,15 32,32', '46,23 32,32 46,41', '32,32 32,49 18,41']) {
      expect(svg).toContain(`points="${facet}"`)
    }
  })

  it('links content-hashed icons so the immutable nginx cache never serves stale ones', () => {
    const svgIcon = linkHrefs('icon').find((href) => href.endsWith('.svg'))
    const [touchIcon] = linkHrefs('apple-touch-icon')

    expect(svgIcon).toBe('/src/assets/brand/favicon.svg')
    expect(touchIcon).toBe('/src/assets/brand/apple-touch-icon.png')
    for (const href of [svgIcon, touchIcon]) {
      expect(existsSync(resolve(webappRoot, `.${href}`))).toBe(true)
    }
  })

  it('keeps a 32px favicon.ico at the root for browsers without SVG favicons', () => {
    expect(html).toContain('<link rel="icon" href="/favicon.ico" sizes="32x32">')
    expect(existsSync(resolve(webappRoot, 'public/favicon.ico'))).toBe(true)
    expect(html).not.toContain('rel="mask-icon"')
  })
})
