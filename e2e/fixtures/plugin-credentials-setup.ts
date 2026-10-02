import { execFileSync } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import { request, type FullConfig } from '@playwright/test'
import path from 'node:path'

const apiUrl = process.env.E2E_API_URL || 'http://127.0.0.1:8000/api/v1'
const statePath = process.env.E2E_FIXTURE_STATE || './test-results/focused.json'
const repo = path.resolve(__dirname, '../..')

async function setup(_config: FullConfig): Promise<void> {
  const dbPath = path.resolve(process.env.SQLITE_PATH || '')
  const isolatedOutput =
    dbPath.endsWith('/e2e/test-results/focused-db/plugin-e2e.sqlite3') ||
    dbPath.startsWith('/workspace/.opencuria/')
  if (!isolatedOutput) {
    throw new Error('Focused E2E requires its isolated disposable SQLite database')
  }
  const seedPath = path.join(repo, 'e2e/fixtures/seed_plugin_credentials.py')
  execFileSync(
    path.join(repo, 'backend/.venv/bin/python'),
    ['manage.py', 'shell', '-c', `exec(open(${JSON.stringify(seedPath)}).read())`],
    { cwd: path.join(repo, 'backend'), env: process.env, stdio: 'inherit' },
  )

  const state = JSON.parse(await readFile(statePath, 'utf8'))
  const client = await request.newContext({ baseURL: apiUrl.replace(/\/api\/v1\/?$/, '') })
  try {
    const login = await client.post('/api/v1/auth/login/', {
      data: { email: state.adminEmail, password: state.password },
    })
    if (!login.ok())
      throw new Error(`Focused E2E login failed (${login.status()}): ${await login.text()}`)
    const token = (await login.json()).access_token
    const headers = {
      Authorization: `Bearer ${token}`,
      'X-Organization-Id': state.organizationId,
    }
    const plugins = await client.get('/api/v1/plugins/', { headers }).then((res) => res.json())
    const notion = plugins.find((item: { slug: string }) => item.slug === 'notion')
    if (!notion) throw new Error('Focused Notion plugin seed is unavailable')
    state.notionPluginId = notion.id
    state.notionServiceId = notion.credential_requirements.find(
      (item: { service_slug: string }) => item.service_slug === 'notion-oauth',
    )?.service_id
    if (!state.notionServiceId) throw new Error('Focused Notion OAuth service is unavailable')
    const plugin = await client
      .get(`/api/v1/plugins/${state.notionPluginId}/`, { headers })
      .then((res) => res.json())
    state.notionOriginalEnabled = plugin.org_enabled
    await writeFile(statePath, JSON.stringify(state, null, 2), { mode: 0o600 })
  } finally {
    await client.dispose()
  }
}

export default setup
