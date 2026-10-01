import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

const repositoryRoot = path.resolve(process.cwd(), '..')
const bridge = `${repositoryRoot}/backend/apps/runners/assets/native_kasm_clipboard.js`
const harness = `${repositoryRoot}/backend/apps/runners/tests/native_clipboard_bridge_harness.cjs`

describe('native KasmVNC clipboard bridge', () => {
  it('preserves generation fences, paste event ordering, manual fallback and empty MIME safety', () => {
    const output = execFileSync(process.execPath, [harness, bridge], { encoding: 'utf8' })
    expect(output).toContain('native clipboard ordering, fallback, empty MIME and generation fences passed')
  })
})
