// @vitest-environment node
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { describe, expect, it } from 'vitest'
import { monacoWorkers } from '../../build/monacoWorkers'

const require = createRequire(import.meta.url)
const transform = monacoWorkers().transform as (
  code: string, id: string,
) => { code: string; map: null } | undefined

describe('Monaco worker fallback bundling', () => {
  it.each(['typescript', 'json', 'css', 'html'])('removes the unused %s worker fallback while preserving language-service configuration', (language) => {
    const path = require.resolve(`monaco-editor/languages/features/${language}/workerManager.js`)
    const source = readFileSync(path, 'utf8')
    expect(source).toContain('createWorker: () => new Worker(new URL(')
    const result = transform(source, path)
    expect(result?.code).not.toContain('createWorker: () => new Worker(new URL(')
    expect(result?.code).toContain('moduleId:')
    expect(result?.code).toContain('createData:')
    expect(result?.code).toContain('label:')
  })

  it('leaves unrelated application and Monaco source unchanged', () => {
    const source = 'const worker = new Worker(new URL("app.worker.js", import.meta.url))'
    expect(transform(source, '/workspace/app/src/workerManager.js')).toBeUndefined()
    expect(transform(source, '/workspace/node_modules/monaco-editor/esm/vs/editor/editor.api.js')).toBeUndefined()
  })
})
