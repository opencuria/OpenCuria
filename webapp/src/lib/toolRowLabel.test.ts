import { describe, expect, it } from 'vitest'
import type { HarnessPart } from '@/types/harness'
import { toolRowLabel } from './toolRowLabel'

function part(overrides: Partial<HarnessPart>): HarnessPart {
  return {
    id: 'p1',
    session_id: 's1',
    type: 'tool',
    state: 'completed',
    title: '',
    output: '',
    ...overrides,
  }
}

describe('toolRowLabel', () => {
  it('keeps reasoning as a single verb', () => {
    expect(toolRowLabel(part({ type: 'reasoning' }))).toEqual({
      verb: 'Thought',
      object: '',
      hint: '',
    })
  })

  it('splits shell commands after the prompt marker', () => {
    expect(toolRowLabel(part({ tool: 'bash', title: '$ npm run test' }))).toEqual({
      verb: '$',
      object: 'npm run test',
      hint: '',
    })
  })

  it('splits file tools into base name and directory hint', () => {
    expect(toolRowLabel(part({ tool: 'read', title: 'Read src/components/App.vue' }))).toEqual({
      verb: 'Read',
      object: 'App.vue',
      hint: 'src/components',
    })
  })

  it('keeps bare file names without a hint', () => {
    expect(toolRowLabel(part({ tool: 'edit', title: 'Edit README.md' }))).toEqual({
      verb: 'Edit',
      object: 'README.md',
      hint: '',
    })
  })

  it('does not shorten non-path objects', () => {
    expect(toolRowLabel(part({ tool: 'glob', title: 'Glob src/**/*.ts' }))).toEqual({
      verb: 'Glob',
      object: 'src/**/*.ts',
      hint: '',
    })
    expect(toolRowLabel(part({ tool: 'webfetch', title: 'Fetch https://a.dev/x' }))).toEqual({
      verb: 'Fetch',
      object: 'https://a.dev/x',
      hint: '',
    })
  })

  it('falls back to the whole label when there is no verb', () => {
    expect(toolRowLabel(part({ tool: 'custom', title: 'bash' }))).toEqual({
      verb: 'bash',
      object: '',
      hint: '',
    })
  })
})
