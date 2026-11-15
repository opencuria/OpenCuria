import { createRequire } from 'node:module'
import { build } from 'esbuild'
import type { Plugin } from 'vite'

const workerImports = new Map([
  ['editor', 'monaco-editor/editor/editor.worker.js'],
  ['json', 'monaco-editor/languages/features/json/json.worker.js'],
  ['css', 'monaco-editor/languages/features/css/css.worker.js'],
  ['html', 'monaco-editor/languages/features/html/html.worker.js'],
  ['typescript', 'monaco-editor/languages/features/typescript/ts.worker.js'],
])
const virtualPrefix = '\0opencuria:monaco-worker:'

/**
 * Monaco workers duplicate part of the editor graph and include a large
 * TypeScript compiler. Bundle them directly with esbuild so Rollup does not
 * retain those ASTs alongside the application. Workers remain self-contained.
 */
export function monacoWorkers(): Plugin {
  let production = false
  const compiledWorkers = new Map<string, Promise<Uint8Array>>()
  return {
    name: 'opencuria-monaco-workers',
    enforce: 'pre',
    configResolved(config) {
      production = config.command === 'build'
    },
    resolveId(source) {
      for (const [name, workerImport] of workerImports) {
        if (source === `${workerImport}?monaco-worker`) return `${virtualPrefix}${name}`
      }
    },
    transform(code, id) {
      if (!id.includes('/monaco-editor/esm/vs/languages/features/') || !id.endsWith('/workerManager.js')) return
      // Monaco 0.57 also embeds fallback worker constructors. Our environment
      // supplies every worker, so remove those unused fallbacks before Vite
      // discovers and bundles a second copy of the compiler worker.
      const transformed = code.replace(
        /createWorker:\s*\(\)\s*=>\s*new Worker\(new URL\((['"])[^'"]+\.worker\.js\1,\s*import\.meta\.url\),\s*\{\s*type:\s*['"]module['"]\s*\}\),?/g,
        '',
      )
      if (transformed !== code) return { code: transformed, map: null }
    },
    async load(id) {
      if (!id.startsWith(virtualPrefix)) return
      const name = id.slice(virtualPrefix.length)
      const workerImport = workerImports.get(name)
      if (!workerImport) return
      if (!production) return `export { default } from ${JSON.stringify(`${workerImport}?worker`)};`

      if (!compiledWorkers.has(name)) {
        compiledWorkers.set(name, build({
          entryPoints: [createRequire(import.meta.url).resolve(workerImport)],
          bundle: true,
          write: false,
          format: 'esm',
          platform: 'browser',
          target: 'es2020',
          minify: true,
          legalComments: 'eof',
        }).then((result) => result.outputFiles[0]!.contents))
      }
      const referenceId = this.emitFile({
        type: 'asset',
        name: `monaco-${name}.worker.js`,
        source: await compiledWorkers.get(name)!,
      })
      return `export default class MonacoWorker extends Worker {
        constructor(options = {}) {
          super(import.meta.ROLLUP_FILE_URL_${referenceId}, { ...options, type: 'module' });
        }
      }`
    },
  }
}
