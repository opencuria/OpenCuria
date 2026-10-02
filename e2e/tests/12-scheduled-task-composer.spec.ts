import { expect, test } from '@playwright/test'

const baseURL = process.env.E2E_BASE_URL || 'http://127.0.0.1:5178'
const accessToken = process.env.E2E_ACCESS_TOKEN
const organizationId = process.env.E2E_ORGANIZATION_ID
const apiBaseURL = process.env.E2E_API_URL || `${new URL(baseURL).origin}/api/v1`

test('scheduled composer suggestions do not shift the form and safely edit a draft', async ({
  browser,
}) => {
  test.skip(
    !accessToken || !organizationId,
    'Set E2E_ACCESS_TOKEN and E2E_ORGANIZATION_ID to use the existing test account.',
  )

  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  await context.addInitScript(
    ({ token, orgId }) => {
      localStorage.setItem('kern_access_token', token)
      localStorage.setItem('kern_active_org_id', orgId)
    },
    { token: accessToken!, orgId: organizationId! },
  )
  const page = await context.newPage()
  const errors: string[] = []
  const scheduledTaskWrites: string[] = []
  await page.route('**/api/v1/skills/', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 'composer-e2e-skill',
          name: 'Composer E2E skill',
          body: 'Use this skill only in the unsaved composer draft.',
          scope: 'personal',
          created_by_email: null,
          created_at: '2026-10-01T00:00:00.000Z',
          updated_at: '2026-10-01T00:00:00.000Z',
        },
      ]),
    }),
  )
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('request', (request) => {
    if (
      /\/scheduled-tasks(?:\/|$)/.test(new URL(request.url()).pathname) &&
      request.method() !== 'GET'
    )
      scheduledTaskWrites.push(`${request.method()} ${request.url()}`)
  })

  try {
    const tasksResponse = await page.request.get(`${apiBaseURL}/scheduled-tasks/`, {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'X-Organization-Id': organizationId!,
      },
    })
    expect(tasksResponse.ok(), 'could not read scheduled tasks using the supplied token').toBe(true)
    const tasks = (await tasksResponse.json()) as Array<{ id: string }>
    test.skip(!tasks[0], 'The supplied test account has no scheduled task to inspect.')

    await page.goto(`/?scheduledTask=${encodeURIComponent(tasks[0]!.id)}`)
    const dialog = page.getByTestId('scheduled-task-dialog')
    const editor = page.getByTestId('task-prompt')
    await expect(dialog).toBeVisible()
    await expect(editor).toBeVisible()
    await page.evaluate(() => document.fonts.ready)

    // Scope selectors to the task dialog, and measure only stable nearby controls.
    const bounds = async () =>
      dialog.evaluate((root) => {
        const box = (selector: string) => {
          const element = root.querySelector<HTMLElement>(selector)
          if (!element) throw new Error(`Missing scheduled-dialog element: ${selector}`)
          const { x, y, width, height } = element.getBoundingClientRect()
          return { x, y, width, height }
        }
        return {
          card: box('[data-testid="composer-card"]'),
          name: box('[data-testid="task-name"]'),
          schedule: box('#schedule-heading'),
        }
      })

    await editor.fill('Check this prompt')
    const before = await bounds()
    await editor.fill('Check this prompt @agent:')
    const portal = page.getByTestId('composer-suggestions-portal')
    await expect(portal).toBeVisible()
    await expect(portal.getByRole('option').first()).toBeVisible()
    await expect(editor).toBeFocused()
    const portalBox = await portal.boundingBox()
    const after = await bounds()
    expect(portalBox).not.toBeNull()
    expect(portalBox!.y + portalBox!.height).toBeLessThanOrEqual(1000)
    expect(portalBox!.y).toBeLessThan(after.card.y)
    expect(after.card.height).toBeCloseTo(before.card.height, 0)
    expect(after.name.y).toBeCloseTo(before.name.y, 0)
    expect(after.schedule.y).toBeCloseTo(before.schedule.y, 0)

    // Escape dismisses suggestions without closing the dialog; Enter inserts a
    // newline instead of sending because this is a scheduled-task draft editor.
    await page.keyboard.press('Escape')
    await expect(portal).toHaveCount(0)
    await expect(dialog).toBeVisible()
    await editor.press('Enter')
    await expect(editor).toContainText('Check this prompt @agent:')
    const promptAfterEnter = await editor.innerText()
    expect(promptAfterEnter).toContain('\n')

    const workspace = dialog.getByTestId('task-workspace')
    await expect(workspace).toBeDisabled()
    const attach = dialog.getByTestId('composer-attach')
    await expect(attach).toBeDisabled()

    // Existing file references are parsed locally even while file search/upload is offline.
    await editor.fill('Check this prompt @file:/workspace/e2e-smoke.txt ')
    await expect(editor.locator('[data-testid="composer-file-badge"]')).toHaveCount(1)

    // Skill selection updates the unsaved prompt form only; never call save or run.
    const skillOption = page.getByTestId('composer-skill-option').first()
    await editor.fill('Check this prompt /')
    await expect(skillOption).toBeVisible()
    await skillOption.click()
    const selectedSkill = dialog.locator('[data-testid="composer-skill-chip-composer-e2e-skill"]')
    await expect(selectedSkill).toHaveCount(1)
    await expect(dialog.getByTestId('task-prompt-error')).toHaveCount(0)

    // At mobile height put the editor into view before opening the portal.
    await page.setViewportSize({ width: 390, height: 560 })
    await editor.scrollIntoViewIfNeeded()
    await editor.fill('Check this prompt @agent:')
    await expect(portal).toBeVisible()
    const mobileBox = await portal.boundingBox()
    expect(mobileBox).not.toBeNull()
    expect(mobileBox!.y).toBeGreaterThanOrEqual(0)
    expect(mobileBox!.y + mobileBox!.height).toBeLessThanOrEqual(560)
    await expect(editor).toBeFocused()
    expect(errors).toEqual([])
    expect(scheduledTaskWrites).toEqual([])
  } finally {
    await context.close()
  }
})
