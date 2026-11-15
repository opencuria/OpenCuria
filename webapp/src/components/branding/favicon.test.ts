import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const webappRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')

function readPublic(name: string): string {
  return readFileSync(resolve(webappRoot, 'public', name), 'utf8')
}

describe('favicon assets', () => {
  it('fills the canvas so Safari does not pad a tiny mark onto white', () => {
    const svg = readPublic('favicon.svg')

    expect(svg).toContain('viewBox="0 0 64 64"')
    expect(svg).toContain('<rect width="64" height="64" fill="#0A0A0A"/>')
    expect(svg).toContain('scale(1.7)')
    expect(svg).not.toMatch(/<rect[^>]*rx=/)
  })

  it('exposes PNG and Safari fallbacks from index.html', () => {
    const html = readFileSync(resolve(webappRoot, 'index.html'), 'utf8')

    expect(html).toContain('href="/favicon.svg"')
    expect(html).toContain('href="/favicon-32.png"')
    expect(html).toContain('href="/apple-touch-icon.png"')
    expect(html).toContain('href="/safari-pinned-tab.svg"')
    expect(existsSync(resolve(webappRoot, 'public/favicon-32.png'))).toBe(true)
    expect(existsSync(resolve(webappRoot, 'public/apple-touch-icon.png'))).toBe(true)
    expect(existsSync(resolve(webappRoot, 'public/favicon.ico'))).toBe(true)
  })
})
