import { mkdirSync } from 'node:fs'
import { expect, test, type Page, type Request } from '@playwright/test'

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:5173'
const ARTIFACTS = '/workspace/.opencuria/playwright'
const CACHE_PREFIX = 'question-input:'
const FIXTURE_INIT_KEY = 'question-input-cache-fixtures-installed'
mkdirSync(ARTIFACTS, { recursive: true })

test.use({
  baseURL: BASE_URL,
  launchOptions: {
    executablePath: process.env.E2E_CHROME_PATH || '/usr/bin/google-chrome',
  },
  viewport: { width: 1440, height: 960 },
  video: 'off',
})

type QuestionRequest = {
  request_id: string
  session_id: string
  workspace_id: string
  questions: Array<{
    header?: string
    question: string
    multiple?: boolean
    options?: Array<{ label: string; description?: string }>
  }>
  status: 'pending'
  agent_name: string
}

type DraftPayload = { answers: string[]; custom: string[] }
type WorkspaceId = 'alpha' | 'beta'
type SessionId = 'alpha-1' | 'alpha-2' | 'beta-1'

const WORKSPACE_IDS: WorkspaceId[] = ['alpha', 'beta']
const SESSION_WORKSPACE: Record<SessionId, WorkspaceId> = {
  'alpha-1': 'alpha',
  'alpha-2': 'alpha',
  'beta-1': 'beta',
}

const baseQuestion = (prefix: string) => [
  {
    header: 'Color',
    question: `${prefix}: choose a color`,
    options: [{ label: 'Red' }, { label: 'Blue' }],
  },
  {
    header: 'Formats',
    question: `${prefix}: choose formats`,
    multiple: true,
    options: [{ label: 'Audio' }, { label: 'Video' }, { label: 'Text' }],
  },
  { header: 'Notes', question: `${prefix}: add notes` },
]

function questionRequest(
  requestId: string,
  sessionId: SessionId,
  workspaceId: WorkspaceId,
  prefix = requestId,
): QuestionRequest {
  return {
    request_id: requestId,
    session_id: sessionId,
    workspace_id: workspaceId,
    questions: baseQuestion(prefix),
    status: 'pending',
    agent_name: 'plan',
  }
}

function makeSession(id: SessionId) {
  const workspaceId = SESSION_WORKSPACE[id]
  return {
    id,
    workspace_id: workspaceId,
    parent_id: null,
    harness_id: 'native',
    title: `${workspaceId.toUpperCase()} chat ${id}`,
    mode: 'plan',
    agent_name: 'plan',
    model: '',
    reasoning_effort: '',
    status: 'busy',
    skill_ids: [],
    unread: false,
    manual_unread: false,
    needs_attention: true,
    attention_kind: 'question',
    cost: 0,
    tokens: {},
    created_at: '2026-10-15T10:00:00Z',
    updated_at: '2026-10-15T10:00:00Z',
  }
}

function makeWorkspace(id: WorkspaceId) {
  const name = id === 'alpha' ? 'Alpha' : 'Beta'
  return {
    id,
    name,
    status: 'running',
    runner_online: true,
    runner_id: 'fixture-runner',
    active_operation: null,
    has_active_session: true,
    runtime_type: 'docker',
    credential_ids: [],
    plugin_ids: [],
    credentials_present: false,
    created_by_id: 1,
    created_at: '2026-10-15T10:00:00Z',
    updated_at: '2026-10-15T10:00:00Z',
    last_activity_at: '2026-10-15T10:00:00Z',
    auto_stop_at: null,
    auto_stop_timeout_minutes: null,
    qemu_vcpus: null,
    qemu_memory_mb: null,
    qemu_disk_size_gb: null,
    desktop_width: 1920,
    desktop_height: 1080,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
  }
}

function makeConversation(sessionId: SessionId) {
  const workspaceId = SESSION_WORKSPACE[sessionId]
  return {
    session_id: sessionId,
    workspace_id: workspaceId,
    workspace_name: workspaceId === 'alpha' ? 'Alpha' : 'Beta',
    title: `${workspaceId.toUpperCase()} chat ${sessionId}`,
    status: 'busy',
    mode: 'plan',
    agent_name: 'plan',
    model: '',
    unread: false,
    manual_unread: false,
    needs_attention: true,
    attention_kind: 'question',
    updated_at: '2026-10-15T10:00:00Z',
    last_message_at: '2026-10-15T10:00:00Z',
  }
}

function tokenFor(userId: number): string {
  const payload = Buffer.from(
    JSON.stringify({ exp: 4_102_444_800, sub: userId }),
  ).toString('base64url')
  return `fixture-user-${userId}.${payload}.signature`
}

function tokenUserId(request: Request): number {
  const token = request.headers().authorization?.replace(/^Bearer\s+/i, '') ?? ''
  const match = token.match(/^fixture-user-(\d+)\./)
  return Number(match?.[1] ?? 1)
}

async function installFixtures(page: Page) {
  const sessions = [makeSession('alpha-1'), makeSession('alpha-2'), makeSession('beta-1')]
  const createRequests = () =>
    new Map<string, QuestionRequest[]>([
      [
        'alpha-1',
        [questionRequest('request-alpha-1', 'alpha-1', 'alpha', 'Main draft')],
      ],
      ['alpha-2', [questionRequest('request-alpha-1', 'alpha-2', 'alpha', 'Other chat')]],
      ['beta-1', [questionRequest('request-alpha-1', 'beta-1', 'beta', 'Other workspace')]],
    ])
  // Keep separate org snapshots while intentionally reusing identical workspace,
  // session, and request IDs so only the authenticated org scope can isolate drafts.
  const requestsByOrg = new Map([
    ['fixture-org-1', createRequests()],
    ['fixture-org-2', createRequests()],
  ])
  const workspaces = WORKSPACE_IDS.map(makeWorkspace)
  const conversations = sessions.map((session) => makeConversation(session.id as SessionId))
  const posts: Array<{ sessionId: string; requestId: string; body: unknown }> = []
  const failedRequests = new Set<string>()
  const errors: string[] = []
  let emitSocket: ((event: string, data: unknown) => void) | null = null
  const timelineOrganizations: string[] = []

  page.on('pageerror', (error) => errors.push(error.message))
  await page.addInitScript((accessToken) => {
    const marker = 'question-input-cache-fixtures-installed'
    if (sessionStorage.getItem(marker)) return
    localStorage.setItem('kern_access_token', accessToken)
    localStorage.setItem('kern_refresh_token', 'fixture-refresh')
    localStorage.setItem('kern_active_org_id', 'fixture-org-1')
    sessionStorage.setItem(marker, 'true')
  }, tokenFor(1))

  await page.routeWebSocket(/\/ws\/runner/, (socket) => {
    socket.send(
      '0' +
        JSON.stringify({
          sid: 'question-cache-socket',
          upgrades: [],
          pingInterval: 100_000,
          pingTimeout: 100_000,
        }),
    )
    socket.onMessage((message) => {
      if (String(message).startsWith('40/frontend')) {
        socket.send('40/frontend,' + JSON.stringify({ sid: 'question-cache-frontend' }))
        emitSocket = (event, data) =>
          socket.send('42/frontend,' + JSON.stringify([event, data]))
      } else if (message === '2') {
        socket.send('3')
      }
    })
  })

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()
    const json = (data: unknown, status = 200) => route.fulfill({ status, json: data })

    if (path === '/auth/me/') {
      const userId = tokenUserId(request)
      return json({
        id: userId,
        email: `question-cache-${userId}@example.test`,
        first_name: 'Question',
        last_name: 'Tester',
        organizations: [
          {
            id: 'fixture-org-1',
            name: 'Question cache org 1',
            slug: 'question-cache-1',
            role: 'admin',
            created_at: '2026-10-15T10:00:00Z',
          },
          {
            id: 'fixture-org-2',
            name: 'Question cache org 2',
            slug: 'question-cache-2',
            role: 'admin',
            created_at: '2026-10-15T10:00:00Z',
          },
        ],
      })
    }
    if (path === '/auth/login/' && method === 'POST') {
      const userId = 2
      return json({ access_token: tokenFor(userId), refresh_token: 'fixture-refresh-user-2' })
    }
    if (path === '/auth/providers/')
      return json({ password_enabled: true, sso: { enabled: false } })
    if (path === '/workspaces/') return json(workspaces)
    if (path === '/harness/conversations/') return json(conversations)
    if (path === '/skills/') return json([])
    if (path === '/harness/agent-configs/') return json([])
    if (path === '/harness/provider-config/') return json({})
    if (path === '/harness/provider-config/models/') return json([])
    if (path === '/harness/engines/')
      return json([{ id: 'native', name: 'OpenCuria', modes: ['build', 'plan'], connected: true }])
    if (path === '/harness/recent-models/') return json([])

    const questionPost = path.match(
      /^\/harness\/sessions\/([^/]+)\/questions\/([^/]+)\/?$/,
    )
    if (questionPost && method === 'POST') {
      const [, sessionId, requestId] = questionPost
      const body = request.postDataJSON()
      posts.push({ sessionId: sessionId!, requestId: requestId!, body })
      if (failedRequests.has(requestId!))
        return json({ detail: 'Fixture rejected this answer' }, 500)
      const orgId = request.headers()['x-organization-id'] ?? 'fixture-org-1'
      const scopedRequests = requestsByOrg.get(orgId) ?? requestsByOrg.get('fixture-org-1')!
      scopedRequests.set(
        sessionId!,
        (scopedRequests.get(sessionId!) ?? []).filter((item) => item.request_id !== requestId),
      )
      return json({ request_id: requestId, status: body.reject ? 'rejected' : 'answered' })
    }

    const timeline = path.match(/^\/harness\/sessions\/([^/]+)\/timeline\/?$/)
    if (timeline) {
      const sessionId = timeline[1]!
      const session = sessions.find((item) => item.id === sessionId)
      if (!session) return json({ detail: 'Not found' }, 404)
      const orgId = request.headers()['x-organization-id'] ?? 'fixture-org-1'
      timelineOrganizations.push(orgId)
      const requests = requestsByOrg.get(orgId) ?? requestsByOrg.get('fixture-org-1')!
      return json({ session, messages: [], permissions: [], questions: requests.get(sessionId) ?? [] })
    }

    const workspaceSessions = path.match(/^\/workspaces\/([^/]+)\/harness\/sessions\/$/)
    if (workspaceSessions)
      return json(sessions.filter((session) => session.workspace_id === workspaceSessions[1]))

    const workspaceProcessList = path.match(/^\/workspaces\/([^/]+)\/processes\/$/)
    if (workspaceProcessList) return json([])

    const detail = workspaces.find((workspace) => path === `/workspaces/${workspace.id}/`)
    if (detail) return json(detail)

    // Read-only app dependencies not needed by this focused, mocked-backend regression.
    return json([])
  })

  return {
    sessions,
    requestsByOrg,
    posts,
    failedRequests,
    errors,
    timelineOrganizations,
    emit: async (event: string, data: unknown) => {
      await expect.poll(() => emitSocket !== null).toBe(true)
      emitSocket!(event, data)
    },
  }
}

const sheet = (page: Page) => page.getByTestId('composer-question-sheet')
const options = (page: Page, role: 'radio' | 'checkbox') =>
  sheet(page).getByRole(role)
const customInputs = (page: Page) => sheet(page).getByTestId('composer-question-custom')
const submitButton = (page: Page) => page.getByTestId('composer-question-submit')
const skipButton = (page: Page) => page.getByTestId('composer-question-skip')
const workspaceGroup = (page: Page, id: WorkspaceId) =>
  page.locator(`[data-testid="workspace-conversation-group"][data-workspace-id="${id}"]`)
const sessionTitle = (page: Page, id: SessionId) =>
  workspaceGroup(page, SESSION_WORKSPACE[id])
    .getByTestId('conversation-row')
    .filter({ hasText: id })

async function openSession(page: Page, id: SessionId): Promise<void> {
  await sessionTitle(page, id).click()
  await expect(page).toHaveURL(new RegExp(`/workspaces/${SESSION_WORKSPACE[id]}\\?session=${id}$`))
}

async function chooseDraft(page: Page, marker = 'blue choice'): Promise<void> {
  await expect(sheet(page)).toBeVisible()
  await options(page, 'radio').getByText('Blue', { exact: true }).click()
  await options(page, 'checkbox').getByText('Audio', { exact: true }).click()
  await options(page, 'checkbox').getByText('Video', { exact: true }).click()
  await customInputs(page).nth(1).fill(`custom ${marker}`)
  await customInputs(page).nth(2).fill(`free text ${marker}`)
}

async function expectDraft(page: Page, marker = 'blue choice'): Promise<void> {
  await expect(sheet(page)).toBeVisible()
  await expect(options(page, 'radio').getByText('Blue', { exact: true })).toBeVisible()
  await expect(options(page, 'radio').filter({ hasText: 'Blue' })).toHaveAttribute(
    'aria-checked',
    'true',
  )
  await expect(options(page, 'checkbox').filter({ hasText: 'Audio' })).toHaveAttribute(
    'aria-checked',
    'true',
  )
  await expect(options(page, 'checkbox').filter({ hasText: 'Video' })).toHaveAttribute(
    'aria-checked',
    'true',
  )
  await expect(customInputs(page).nth(1)).toHaveValue(`custom ${marker}`)
  await expect(customInputs(page).nth(2)).toHaveValue(`free text ${marker}`)
}

async function draftEntries(page: Page): Promise<Array<{ key: string; value: DraftPayload }>> {
  return page.evaluate((prefix) => {
    const entries: Array<{ key: string; value: DraftPayload }> = []
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const key = sessionStorage.key(index)
      if (!key?.startsWith(prefix)) continue
      const raw = sessionStorage.getItem(key)
      if (raw === null) continue
      entries.push({ key, value: JSON.parse(raw) as DraftPayload })
    }
    return entries
  }, CACHE_PREFIX)
}

function isDraftWithMarker(
  entry: { key: string; value: DraftPayload },
  marker: string,
): boolean {
  const { key, value } = entry
  return (
    key.startsWith(CACHE_PREFIX) &&
    Object.keys(value).sort().join(',') === 'answers,custom' &&
    Array.isArray(value.answers) &&
    Array.isArray(value.custom) &&
    value.answers.every((answer) => typeof answer === 'string') &&
    value.custom.every((answer) => typeof answer === 'string') &&
    JSON.stringify(value).includes(marker)
  )
}

async function expectDraftPayload(
  page: Page,
  marker: string,
): Promise<{ key: string; value: DraftPayload }> {
  await expect
    .poll(async () => (await draftEntries(page)).some((entry) => isDraftWithMarker(entry, marker)))
    .toBe(true)
  return (await draftEntries(page)).find((entry) => isDraftWithMarker(entry, marker))!
}

async function expectMainDraft(page: Page, marker = 'blue choice'): Promise<void> {
  await expectDraft(page, marker)
  const { value } = await expectDraftPayload(page, marker)
  expect(value).toEqual({
    answers: ['Blue', `Audio${String.fromCharCode(0)}Video`, ''],
    custom: ['', `custom ${marker}`, `free text ${marker}`],
  })
}

async function expectDraftsUnchanged(
  page: Page,
  entries: Array<{ key: string; value: DraftPayload }>,
): Promise<void> {
  const sort = (items: Array<{ key: string; value: DraftPayload }>) =>
    [...items].sort((a, b) => a.key.localeCompare(b.key))
  await expect.poll(async () => sort(await draftEntries(page))).toEqual(sort(entries))
}

async function expectEmptyQuestionDraft(page: Page): Promise<void> {
  await expect(options(page, 'radio').filter({ hasText: 'Blue' })).toHaveAttribute(
    'aria-checked',
    'false',
  )
  for (const input of await customInputs(page).all()) await expect(input).toHaveValue('')
}

async function openProcesses(page: Page): Promise<void> {
  await page.getByTestId('workspace-chat-header-toggle-processes').click()
  await expect(page.getByTestId('composer-process-sheet')).toBeVisible()
}

async function closeProcesses(page: Page): Promise<void> {
  await page.getByTestId('composer-process-close').click()
  await expect(page.getByTestId('composer-process-sheet')).toHaveCount(0)
}

test.describe('question tool input cache in the real Vite Vue app', () => {
  test('persists immediately and restores across sheets, requests, chats, workspaces, leaving, and reload', async ({
    page,
  }) => {
    const fixture = await installFixtures(page)
    await page.goto('/workspaces/alpha?session=alpha-1')
    await expect(sheet(page)).toBeVisible()

    await chooseDraft(page)
    const originalDraft = await expectDraftPayload(page, 'free text blue choice')
    expect(originalDraft.value).toEqual({
      answers: ['Blue', `Audio${String.fromCharCode(0)}Video`, ''],
      custom: ['', 'custom blue choice', 'free text blue choice'],
    })
    expect(fixture.posts, 'typing/selecting must never submit the question').toEqual([])

    // Another composer sheet temporarily unmounts the question form; closing it
    // must restore the real question inputs rather than losing local component state.
    await openProcesses(page)
    await expect(sheet(page)).toHaveCount(0)
    await closeProcesses(page)
    await expectMainDraft(page)
    expect(fixture.posts).toEqual([])

    // A second request arriving during the same run receives an independent draft.
    const secondRequest = questionRequest(
      'request-alpha-1-second',
      'alpha-1',
      'alpha',
      'Second request',
    )
    for (const scopedRequests of fixture.requestsByOrg.values())
      scopedRequests.set('alpha-1', [...(scopedRequests.get('alpha-1') ?? []), secondRequest])
    await fixture.emit('harness.question_required', secondRequest)
    await expect(page.getByTestId('composer-question-pager')).toContainText('1 of 2')
    await page.getByTestId('composer-question-next').click()
    await expect(sheet(page)).toContainText('Second request: choose a color')
    await expect(options(page, 'radio').filter({ hasText: 'Blue' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
    await options(page, 'radio').getByText('Blue', { exact: true }).click()
    await options(page, 'checkbox').getByText('Text', { exact: true }).click()
    await customInputs(page).nth(1).fill('custom second request')
    await customInputs(page).nth(2).fill('free text second request')
    const secondDraft = await expectDraftPayload(page, 'free text second request')
    expect(secondDraft.value.answers).toEqual(['Blue', 'Text', ''])
    expect(secondDraft.value.custom).toEqual([
      '',
      'custom second request',
      'free text second request',
    ])
    expect(await draftEntries(page)).toHaveLength(2)
    await page.getByTestId('composer-question-prev').click()
    await expectDraft(page)
    await page.getByTestId('composer-question-next').click()
    await expect(options(page, 'radio').filter({ hasText: 'Blue' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    await expect(customInputs(page).nth(1)).toHaveValue('custom second request')
    await expect(customInputs(page).nth(2)).toHaveValue('free text second request')
    expect(fixture.posts).toEqual([])

    // Same tab, another session: the UI starts empty, but the first chat's draft remains intact.
    await openSession(page, 'alpha-2')
    await expect(sheet(page)).toBeVisible()
    await expectEmptyQuestionDraft(page)
    await expectDraftsUnchanged(page, [originalDraft, secondDraft])
    await openSession(page, 'alpha-1')
    await expectMainDraft(page)

    // Same workspace/session/request ids must still isolate drafts by organization.
    const orgOneSnapshot = await draftEntries(page)
    expect(orgOneSnapshot).toHaveLength(2)
    const orgOneKeys = orgOneSnapshot.map(({ key }) => key).sort()
    const orgOneStorage = await page.evaluate(() => ({
      accessToken: localStorage.getItem('kern_access_token'),
      refreshToken: localStorage.getItem('kern_refresh_token'),
      orgId: localStorage.getItem('kern_active_org_id'),
    }))
    expect(orgOneStorage.orgId).toBe('fixture-org-1')
    await page.getByRole('button', { name: /OpenCuria OpenCuria/ }).click()
    const otherOrg = page.getByRole('menuitem', { name: /Question cache org 2/ })
    await expect(otherOrg).toBeVisible()
    await otherOrg.click()
    await expect.poll(async () => page.evaluate(() => localStorage.getItem('kern_active_org_id')))
      .toBe('fixture-org-2')
    await expect(page).toHaveURL(/\/workspaces\/alpha\?session=alpha-1$/)
    await expect(sheet(page)).toBeVisible()
    await expectEmptyQuestionDraft(page)
    await expect.poll(() => fixture.timelineOrganizations.at(-1)).toBe('fixture-org-2')
    await expectDraftsUnchanged(page, orgOneSnapshot)

    await customInputs(page).nth(2).fill('org two private draft')
    const orgTwoDraft = await expectDraftPayload(page, 'org two private draft')
    expect(orgTwoDraft.value).toEqual({
      answers: ['', '', ''],
      custom: ['', '', 'org two private draft'],
    })
    expect(orgTwoDraft.key).not.toBe(originalDraft.key)
    const orgTwoKeys = (await draftEntries(page)).map(({ key }) => key).sort()
    expect(orgTwoKeys).toHaveLength(3)
    expect(orgTwoKeys).toContain(orgTwoDraft.key)
    expect(orgTwoKeys).toEqual(expect.arrayContaining(orgOneKeys))
    expect(orgOneKeys.every((key) => key.includes('fixture-org-1'))).toBe(true)
    expect(orgTwoDraft.key).toContain('fixture-org-2')

    await page.getByRole('button', { name: /OpenCuria OpenCuria/ }).click()
    const orgOne = page.getByRole('menuitem', { name: /Question cache org 1/ })
    await expect(orgOne).toBeVisible()
    await orgOne.click()
    await expect.poll(async () => page.evaluate(() => localStorage.getItem('kern_active_org_id')))
      .toBe('fixture-org-1')
    await expect(page).toHaveURL(/\/workspaces\/alpha\?session=alpha-1$/)
    await expectMainDraft(page)
    await expectDraftPayload(page, 'second request')
    await expectDraftsUnchanged(page, [...orgOneSnapshot, orgTwoDraft])

    // Switch to a different workspace and independently open its question request.
    await openSession(page, 'beta-1')
    await expect(page).toHaveURL(/\/workspaces\/beta\?session=beta-1$/)
    await expect(sheet(page)).toBeVisible()
    await expectEmptyQuestionDraft(page)
    await sessionTitle(page, 'alpha-1').click()
    await expect(page).toHaveURL(/\/workspaces\/alpha\?session=alpha-1$/)
    await expectMainDraft(page)

    // Leaving the session in the same workspace hides the question and returning restores it.
    await page.getByTestId('all-workspaces').click()
    await expect(page).toHaveURL('/workspaces')
    await expect(sheet(page)).toHaveCount(0)
    await openSession(page, 'alpha-1')
    await expectMainDraft(page)

    // A full reload is still the same tab/sessionStorage and must restore the live Vue UI.
    await page.reload()
    await expectMainDraft(page)
    await page.screenshot({ path: `${ARTIFACTS}/question-input-restored.png` })
    expect(fixture.posts).toEqual([])
    expect(fixture.errors).toEqual([])
  })

  test('retains failed submissions; clears on successful submit, skip, and WS termination', async ({
    page,
  }) => {
    const fixture = await installFixtures(page)
    await page.goto('/workspaces/alpha?session=alpha-1')
    await expect(sheet(page)).toBeVisible()
    await chooseDraft(page, 'failure path')
    await expectMainDraft(page, 'failure path')

    fixture.failedRequests.add('request-alpha-1')
    await submitButton(page).click()
    await expect.poll(() => fixture.posts.length).toBe(1)
    await expect(sheet(page)).toBeVisible()
    await expectMainDraft(page, 'failure path')
    expect(fixture.posts[0]).toEqual({
      sessionId: 'alpha-1',
      requestId: 'request-alpha-1',
      body: {
        answers: ['Blue', `Audio${String.fromCharCode(0)}Video${String.fromCharCode(0)}custom failure path`, 'free text failure path'],
        reject: false,
      },
    })
    await expectDraftPayload(page, 'free text failure path')

    // A failed request is recoverable after navigating to another chat and back.
    await openSession(page, 'alpha-2')
    await openSession(page, 'alpha-1')
    await expectMainDraft(page, 'failure path')

    fixture.failedRequests.clear()
    await submitButton(page).click()
    await expect.poll(() => fixture.posts.length).toBe(2)
    await expect(sheet(page)).toHaveCount(0)
    await expect
      .poll(async () => (await draftEntries(page)).length)
      .toBe(0)
    expect(fixture.posts[1]?.body).toMatchObject({ reject: false })

    // A new question arriving over Socket.IO can be skipped; the reject POST is successful cleanup.
    const skipRequest = questionRequest('request-skip', 'alpha-1', 'alpha', 'Skip path')
    fixture.requestsByOrg.get('fixture-org-1')!.set('alpha-1', [skipRequest])
    await fixture.emit('harness.question_required', skipRequest)
    await expect(sheet(page)).toBeVisible()
    await customInputs(page).nth(2).fill('skip should discard this')
    const skippedDraft = await expectDraftPayload(page, 'skip should discard this')
    expect(skippedDraft.value.custom[2]).toBe('skip should discard this')
    await skipButton(page).click()
    await expect.poll(() => fixture.posts.length).toBe(3)
    await expect(sheet(page)).toHaveCount(0)
    expect(fixture.posts[2]).toEqual({
      sessionId: 'alpha-1',
      requestId: 'request-skip',
      body: { answers: [], reject: true },
    })
    await expect
      .poll(async () => (await draftEntries(page)).length)
      .toBe(0)

    // External termination must clear a draft even without a submit/skip click.
    const terminatedRequest = questionRequest('request-ws-ended', 'alpha-1', 'alpha', 'WS end')
    fixture.requestsByOrg.get('fixture-org-1')!.set('alpha-1', [terminatedRequest])
    await fixture.emit('harness.question_required', terminatedRequest)
    await expect(sheet(page)).toBeVisible()
    await customInputs(page).nth(2).fill('socket termination draft')
    const terminatedDraft = await expectDraftPayload(page, 'socket termination draft')
    expect(terminatedDraft.value.custom[2]).toBe('socket termination draft')
    await fixture.emit('harness.question_required', {
      ...terminatedRequest,
      status: 'answered',
    })
    await expect(sheet(page)).toHaveCount(0)
    await expect
      .poll(async () => (await draftEntries(page)).length)
      .toBe(0)
    expect(fixture.posts).toHaveLength(3)
    expect(fixture.errors).toEqual([])
  })

  test('logout removes all question drafts before another account uses the tab', async ({ page }) => {
    const fixture = await installFixtures(page)
    await page.goto('/workspaces/alpha?session=alpha-1')
    await expect(sheet(page)).toBeVisible()
    await customInputs(page).nth(2).fill('account one private draft')
    const privateDraft = await expectDraftPayload(page, 'account one private draft')
    expect(privateDraft.value.custom).toEqual(['', '', 'account one private draft'])

    await page.getByRole('button', { name: /question-cache-1@example\.test/ }).click()
    await page.getByRole('menuitem', { name: 'Log out', exact: true }).click()
    await expect(page).toHaveURL(/\/login$/)
    await expect
      .poll(async () => (await draftEntries(page)).length)
      .toBe(0)

    await page.getByRole('textbox', { name: 'Email' }).fill('question-cache-2@example.test')
    await page.getByLabel('Password').fill('fixture-password')
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    await expect(page).toHaveURL('/')
    await expect(page.getByText('question-cache-2@example.test')).toBeVisible()
    await openSession(page, 'alpha-1')
    await expect(page).toHaveURL(/\/workspaces\/alpha\?session=alpha-1$/)
    await expect(sheet(page)).toBeVisible()
    await expectEmptyQuestionDraft(page)
    expect(fixture.errors).toEqual([])
  })
})
