import { execFileSync } from 'node:child_process'
import { readFile, unlink } from 'node:fs/promises'
import path from 'node:path'

const repo = path.resolve(__dirname, '../..')
const statePath = process.env.E2E_FIXTURE_STATE || '/workspace/.opencuria/plugin-credentials.json'

async function teardown(): Promise<void> {
  let state: any
  try {
    state = JSON.parse(await readFile(statePath, 'utf8'))
  } catch {
    return
  }
  const dbPath = path.resolve(process.env.SQLITE_PATH || '')
  const isolatedOutput =
    dbPath.endsWith('/e2e/test-results/focused-db/plugin-e2e.sqlite3') ||
    dbPath.startsWith('/workspace/.opencuria/')
  if (!isolatedOutput) {
    throw new Error('Focused E2E teardown refuses to modify a non-test database')
  }
  const cleanupPath = path.join(repo, 'e2e/fixtures/cleanup_plugin_credentials.py')
  execFileSync(path.join(repo, 'backend/.venv/bin/python'), [
    'manage.py', 'shell', '-c', `exec(open(${JSON.stringify(cleanupPath)}).read())`,
  ], { cwd: path.join(repo, 'backend'), env: process.env, stdio: 'inherit' })
  await unlink(statePath).catch(() => undefined)
}

export default teardown
