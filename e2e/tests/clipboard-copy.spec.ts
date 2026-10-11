import { mkdirSync } from 'node:fs'
import { test, expect, type Page } from '@playwright/test'

const viteUrl = process.env.CLIPBOARD_VITE_URL || 'http://127.0.0.1:5173'
const origins = {
  insecure: 'http://clipboard-test.invalid',
  secure: 'https://clipboard-test.invalid',
} as const
const artifacts = '/workspace/.opencuria/playwright'
mkdirSync(artifacts, { recursive: true })

type WindowWithClipboardDiagnostics = Window & {
  __realExecCommand?: typeof document.execCommand
  __deferredCopyStarted?: boolean
  __resolveDeferredCopy?: () => void
  __rejectDeferredCopy?: () => void
}

/**
 * Mount real Vite-compiled Vue components without App/backend/auth. This
 * follows chat-image-preview.spec.ts: use Vite's optimized Vue/Pinia runtime
 * imports, serve a diagnostic entry module, and seed productive stores.
 */
async function installDiagnostic(page: Page, origin: string): Promise<void> {
  const [graph, message, gitStore] = await Promise.all([
    fetch(`${viteUrl}/src/components/git/GitGraphSection.vue`),
    fetch(`${viteUrl}/src/components/chat/HarnessMessageView.vue`),
    fetch(`${viteUrl}/src/stores/git.ts`),
  ])
  expect(graph.ok, 'GitGraphSection served by Vite').toBeTruthy()
  expect(message.ok, 'HarnessMessageView served by Vite').toBeTruthy()
  expect(gitStore.ok, 'git store served by Vite').toBeTruthy()

  const source = [await graph.text(), await message.text(), await gitStore.text()].join('\n')
  const vue = source.match(/(["'])(\/node_modules\/\.vite\/deps\/vue\.js\?v=[^"']+)\1/)?.[2]
  const pinia = source.match(/(["'])(\/node_modules\/\.vite\/deps\/pinia\.js\?v=[^"']+)\1/)?.[2]
  expect(vue, 'optimized Vue import').toBeTruthy()
  expect(pinia, 'optimized Pinia import').toBeTruthy()

  // Seed the productive store with realistic refs, commits, and a stash. The
  // stash base commit is present, so GitGraphSection renders its synthetic row.
  const module = `
import { createApp, h, provide, ref } from ${JSON.stringify(vue)};
import { createPinia } from ${JSON.stringify(pinia)};
import '/src/assets/main.css';
import GitGraphSection from '/src/components/git/GitGraphSection.vue';
import HarnessMessageView from '/src/components/chat/HarnessMessageView.vue';
import CopyButton from '/src/components/common/CopyButton.vue';
import ClipboardDialog from '/src/components/common/ClipboardDialog.vue';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '/src/components/ui/dialog/index.ts';
import { harnessWorkspaceIdKey } from '/src/lib/harnessWorkspaceContext.ts';
import { useGitStore, normalizeRepoSnapshot } from '/src/stores/git.ts';

const rawRepo = {
  id: 'clipboard-fixture', name: 'clipboard-fixture', path: '/workspace/clipboard-fixture',
  current_branch: 'main', head_hash: 'commit-head',
  branches: [
    { name: 'main', tip_hash: 'commit-head', upstream: 'origin/main', ahead: 0, behind: 0 },
    { name: 'feature/copy', tip_hash: 'commit-base', upstream: null, ahead: 0, behind: 0 },
  ],
  remote_refs: [
    { name: 'origin/main', tip_hash: 'commit-head' },
    { name: 'origin/release', tip_hash: 'commit-base' },
  ],
  remotes: ['origin'], default_remote: 'refs/remotes/origin/HEAD', upstream: 'origin/main',
  ahead: 0, behind: 0,
  merge_state: { merging: false, rebasing: false, cherry_picking: false },
  commits: [
    { hash: 'commit-head', message: 'Clipboard regression head', body: '', author: 'E2E',
      author_email: 'e2e@example.invalid', timestamp: '2026-10-10T10:00:00Z',
      author_date: '2026-10-10T10:00:00Z', committer: 'E2E', committer_email: 'e2e@example.invalid',
      committer_date: '2026-10-10T10:00:00Z', parents: ['commit-base'] },
    { hash: 'commit-base', message: 'Clipboard regression base', body: '', author: 'E2E',
      author_email: 'e2e@example.invalid', timestamp: '2026-10-09T10:00:00Z',
      author_date: '2026-10-09T10:00:00Z', committer: 'E2E', committer_email: 'e2e@example.invalid',
      committer_date: '2026-10-09T10:00:00Z', parents: [] },
  ],
  stashes: [{ selector: 'stash@{0}', hash: 'stash-hash', base_hash: 'commit-base',
    message: 'WIP on main: clipboard fixture', author: 'E2E', author_email: 'e2e@example.invalid',
    timestamp: '2026-10-10T11:00:00Z', author_date: '2026-10-10T11:00:00Z',
    committer: 'E2E', committer_email: 'e2e@example.invalid', committer_date: '2026-10-10T11:00:00Z' }],
  has_more: false, history_skip: 0, history_limit: 50, changes: [],
};
const textPart = (id, output) => ({ id, session_id: 'clipboard-session', type: 'text',
  state: 'completed', title: '', output });
const assistantText = 'Final assistant clipboard answer';
const codeMarkdown = 'Code below\\n\\n\u0060\u0060\u0060ts\\nconst clipboardPayload = "from code block";\\n\u0060\u0060\u0060';
const assistantWithWork = [
  textPart('assistant-copy-intro', 'Intermediate assistant explanation; not the answer.'),
  { id: 'assistant-copy-tool', session_id: 'clipboard-session', type: 'tool', state: 'completed',
    title: 'Read fixture', tool: 'read', output: 'Tool result; not the answer.' },
  textPart('assistant-copy-final', assistantText),
];
const makeMessage = (id, content, role = 'assistant', partsOverride) => ({ id,
  session_id: 'clipboard-session', role, content,
  parts: role === 'assistant' ? (partsOverride ?? [textPart(id + '-part', content)]) : [],
  created_at: '2026-10-11T10:00:00Z', completed_at: '2026-10-11T10:00:01Z' });
const deferredPayload = () => new Promise(resolve => {
  window.__deferredCopyStarted = true;
  window.__resolveDeferredCopy = () => resolve('deferred clipboard payload');
});
const rejectedDeferredPayload = () => new Promise((_, reject) => {
  window.__deferredCopyStarted = true;
  window.__rejectDeferredCopy = () => reject(new Error('deferred fixture failure'));
});
const app = createApp({ setup() {
  provide(harnessWorkspaceIdKey, ref('clipboard-diagnostic'));
  const git = useGitStore();
  const repo = normalizeRepoSnapshot(rawRepo);
  git.repos = [{ id: repo.id, name: repo.name, path: repo.path,
    currentBranch: repo.currentBranch, headHash: repo.headHash }];
  git.selectedRepoId = repo.id;
  git.repoDetails = { [repo.path]: repo };
  const hostDialogOpen = ref(false);
  return () => h('main', { class: 'mx-auto max-w-4xl space-y-5 p-6 pb-10 text-foreground' }, [
    h('header', { class: 'border-b border-border pb-3' }, [
      h('p', { class: 'text-xs font-medium uppercase tracking-wider text-muted-foreground' }, 'OpenCuria browser regression'),
      h('h1', { class: 'text-xl font-semibold' }, 'Clipboard verification'),
      h('p', { class: 'text-sm text-muted-foreground' }, 'Real Git graph, chat, Reka UI dialogs, and native keyboard clipboard.'),
    ]),
    h('button', { 'data-testid': 'outside-focus', class: 'rounded-md border px-3 py-2 text-sm' }, 'Outside dialog focus target'),
    h('section', { 'data-testid': 'git-diagnostic', class: 'h-[400px] overflow-hidden rounded-xl border border-border bg-card' }, [h(GitGraphSection)]),
    h('section', { 'data-testid': 'message-diagnostic', class: 'space-y-2 rounded-xl border border-border bg-card p-4' }, [
      h('h2', { class: 'font-medium' }, 'Assistant messages'),
      h('div', { 'data-testid': 'assistant-copy', class: 'rounded-lg bg-muted/40 px-3' }, [
        h(HarnessMessageView, {
          message: makeMessage('assistant-copy', 'Intermediate assistant explanation; not the answer. Tool result; not the answer. Final assistant clipboard answer', 'assistant', assistantWithWork),
          models: [],
        }),
      ]),
      h('div', { 'data-testid': 'user-copy', class: 'rounded-lg bg-muted/40 px-3' }, [
        h(HarnessMessageView, { message: makeMessage('user-copy', 'User prompt **with markdown**', 'user'), models: [] }),
      ]),
      h('div', { 'data-testid': 'assistant-code', class: 'rounded-lg bg-muted/40 px-3' }, [
        h(HarnessMessageView, { message: makeMessage('assistant-code', codeMarkdown), models: [] }),
      ]),
      h('div', { class: 'flex flex-wrap gap-2' }, [
        h(CopyButton, { text: deferredPayload, label: 'deferred text', 'data-testid': 'deferred-copy' }),
        h(CopyButton, { text: deferredPayload, label: 'secure deferred text', 'data-testid': 'secure-deferred-copy' }),
        h(CopyButton, { text: rejectedDeferredPayload, label: 'rejected deferred text', 'data-testid': 'rejected-deferred-copy' }),
      ]),
      h('div', { class: 'flex items-center gap-2' }, [
        h('button', { 'data-testid': 'keyboard-copy-start', class: 'rounded-md border px-3 py-2 text-sm' }, 'Keyboard copy focus start'),
        h(CopyButton, { text: 'keyboard clipboard payload', label: 'keyboard fixture', 'data-testid': 'keyboard-copy' }),
      ]),
    ]),
    h('section', { class: 'space-y-2 rounded-xl border border-border bg-card p-4' }, [
      h('label', { for: 'clipboard-paste-target', class: 'text-sm font-medium' }, 'Clipboard read-back (native Ctrl+V)'),
      h('textarea', { id: 'clipboard-paste-target', 'data-testid': 'clipboard-paste-target',
        'aria-label': 'Clipboard paste target', rows: 2, placeholder: 'Copied text appears here after native paste',
        class: 'w-full resize-y rounded-md border border-border bg-background p-2 font-mono text-xs' }),
    ]),
    h(Dialog, { open: hostDialogOpen.value, 'onUpdate:open': value => { hostDialogOpen.value = value; } }, () => [
      h(DialogTrigger, { asChild: true }, () => h('button', {
        'data-testid': 'open-host-dialog', class: 'rounded-md border px-3 py-2 text-sm',
      }, 'Open real shadcn Dialog copy test')),
      h(DialogContent, { class: 'max-w-lg' }, () => [
        h(DialogTitle, null, () => 'Clipboard regression modal'),
        h(DialogDescription, null, () => 'The regular HTTP copy must keep focus inside this real Reka UI dialog.'),
        h('p', { class: 'text-sm text-muted-foreground' }, 'Copy button and focus stay in this modal.'),
        h(CopyButton, { text: 'modal dialog clipboard payload', label: 'modal fixture', 'data-testid': 'dialog-copy' }),
      ]),
    ]),
    h(ClipboardDialog),
  ]);
} });
app.use(createPinia()); app.mount('#diagnostic-app');
`

  // Proxy Vite's modules/CSS through either fixture origin, preserving the
  // browser-visible HTTP or HTTPS origin (never localhost/loopback).
  await page.context().route(`${origin}/**`, async (route) => {
    const url = new URL(route.request().url())
    await route.fulfill({
      response: await route.fetch({ url: `${viteUrl}${url.pathname}${url.search}` }),
    })
  })
  await page.route('**/clipboard-copy-diagnostic', (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Clipboard diagnostic</title></head><body><div id="diagnostic-app"></div><script type="module" src="/src/clipboard-copy-diagnostic.js"></script></body></html>',
    }),
  )
  await page.route('**/src/clipboard-copy-diagnostic.js', (route) =>
    route.fulfill({ contentType: 'text/javascript', body: module }),
  )
  // Vite's dev client normally opens HMR WebSockets to localhost. That would
  // violate this diagnostic's non-local-hostname HTTP/HTTPS trust boundary;
  // serve the actual client minus only its HMR bootstrap, not a fake client.
  await page.route('**/@vite/client', async (route) => {
    const response = await route.fetch({ url: `${viteUrl}/@vite/client` })
    const body = await response.text()
    const withoutHmrBootstrap = body.replace(/transport\.connect\(createHMRHandler\(handleMessage\)\);/, '')
    expect(withoutHmrBootstrap).not.toBe(body)
    await route.fulfill({ response, body: withoutHmrBootstrap })
  })
  await page.route('**/api/**', (route) => route.abort())
  await page.routeWebSocket(/clipboard-test\.invalid/, (socket) => socket.close())
  await page.routeWebSocket(/127\.0\.0\.1:5173/, (socket) => socket.close())

  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.stack ?? error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`)
  })
  await page.goto(`${origin}/clipboard-copy-diagnostic`)
  await expect(page.getByTestId('git-graph-section'), `Vue mount diagnostics: ${errors.join('\\n')}\\n${await page.locator('body').innerText().catch(() => '')}`).toBeVisible()
  await expect(page.getByTestId('message-diagnostic').getByTestId('message-copy')).toHaveCount(1)
  await expect(page.getByTestId('user-copy').getByTestId('message-copy')).toHaveCount(1)
  await expect(page.getByTestId('assistant-copy').getByTestId('answer-copy')).toHaveCount(1)
  await expect(page.getByTestId('assistant-code').getByTestId('answer-copy')).toHaveCount(1)
  await expect(page.getByTestId('clipboard-paste-target')).toBeVisible()
  expect(await page.evaluate(() => window.isSecureContext)).toBe(origin.startsWith('https:'))
  if (origin.startsWith('http:')) {
    expect(await page.evaluate(() => typeof navigator.clipboard?.writeText)).toBe('undefined')
  }
  expect(errors, 'real Vue component mount errors').toEqual([])
}

async function primeClipboard(page: Page, text: string): Promise<void> {
  const target = page.getByTestId('clipboard-paste-target')
  await target.fill(text)
  await target.focus()
  await target.selectText()
  // Native keyboard copy/paste are trusted user events. Every action primes an
  // explicit old value, ruling out false success from a previously full clipboard.
  await page.keyboard.press('Control+C')
  await expect.poll(() => pasteClipboard(page)).toBe(text)
}

async function pasteClipboard(page: Page): Promise<string> {
  const dialogs = page.getByRole('dialog')
  const dialogCount = await dialogs.count()
  let target
  if (dialogCount > 0) {
    const topDialog = dialogs.nth(dialogCount - 1)
    target = topDialog.getByTestId('clipboard-paste-target')
    if ((await target.count()) === 0) {
      await topDialog.evaluate((dialog) => {
        const textarea = document.createElement('textarea')
        textarea.dataset.testid = 'clipboard-paste-target'
        textarea.setAttribute('aria-label', 'Clipboard paste target')
        textarea.rows = 2
        textarea.style.cssText = 'display:block;width:100%;height:48px;margin-top:12px;padding:8px;font:12px monospace'
        dialog.append(textarea)
      })
      target = topDialog.getByTestId('clipboard-paste-target')
    }
  } else {
    target = page.getByTestId('clipboard-paste-target')
  }
  await target.focus()
  await target.fill('')
  await page.keyboard.press('Control+V')
  return target.inputValue()
}

async function expectNoRetryDialog(page: Page): Promise<void> {
  await expect(page.getByRole('dialog', { name: /^Copy / })).toHaveCount(0)
}

async function screenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${artifacts}/clipboard-copy-${name}.png` })
}

test.describe('clipboard copy through real Vue/shadcn components', () => {
  test.afterEach(async ({ page }, testInfo) => {
    if (testInfo.status !== testInfo.expectedStatus) {
      const title = testInfo.title.toLowerCase().replace(/[^a-z0-9]+/g, '-')
      await screenshot(page, `${testInfo.project.name}-failure-${title}`)
    }
  })

  test('GitGraphSection copies commit hash, stash selector, and remote ref via real menus on insecure HTTP', async ({ page }) => {
    await installDiagnostic(page, origins.insecure)
    await primeClipboard(page, 'old system clipboard sentinel')

    const commitRow = page.locator('[data-testid="git-graph-row"][data-hash="commit-head"]')
    await expect(commitRow).toBeVisible()
    await commitRow.click({ button: 'right' })
    const commitCopy = page.getByTestId('git-commit-copy-commit-head')
    await expect(commitCopy).toBeVisible()
    await commitCopy.click()
    await expectNoRetryDialog(page)
    await expect.poll(() => pasteClipboard(page)).toBe('commit-head')

    await primeClipboard(page, 'old stash clipboard sentinel')
    const stashRow = page.locator('[data-testid="git-graph-row"][data-hash="stash-hash"]')
    await expect(stashRow).toBeVisible()
    await stashRow.click({ button: 'right' })
    const stashCopy = page.getByTestId('git-stash-copy-stash-hash')
    await expect(stashCopy).toBeVisible()
    await stashCopy.click()
    await expectNoRetryDialog(page)
    await expect.poll(() => pasteClipboard(page)).toBe('stash@{0}')

    await primeClipboard(page, 'old remote clipboard sentinel')
    await page.getByTestId('git-ref-tag-origin/release').click()
    const remoteCopy = page.getByTestId('git-remote-copy-origin/release')
    await expect(remoteCopy).toBeVisible()
    await remoteCopy.click()
    await expectNoRetryDialog(page)
    await expect.poll(() => pasteClipboard(page)).toBe('origin/release')
    await screenshot(page, `${test.info().project.name}-git-menus`)
  })

  test('chat message and rendered code buttons copy exact payloads with native paste', async ({ page }) => {
    await installDiagnostic(page, origins.insecure)
    await primeClipboard(page, 'old message clipboard sentinel')
    const answerCopy = page.getByTestId('assistant-copy').getByTestId('answer-copy').last()
    await answerCopy.click()
    await expectNoRetryDialog(page)
    await expect(answerCopy).toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe('Final assistant clipboard answer')

    await primeClipboard(page, 'old user-message clipboard sentinel')
    const userCopy = page.getByTestId('user-copy').getByTestId('message-copy')
    await userCopy.click()
    await expectNoRetryDialog(page)
    await expect(userCopy).toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe('User prompt **with markdown**')

    await primeClipboard(page, 'old code clipboard sentinel')
    const codeBlock = page.getByTestId('assistant-code').getByTestId('harness-code-block')
    const code = codeBlock.locator('code')
    await expect(code).toBeVisible()
    const expectedCode = await code.textContent()
    const codeCopy = codeBlock.getByTestId('code-copy')
    await codeCopy.click()
    await expectNoRetryDialog(page)
    await expect(codeCopy).toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe(expectedCode)
    await screenshot(page, `${test.info().project.name}-chat-code`)
  })

  test('ordinary legacy HTTP copy stays focused and succeeds inside a real shadcn Dialog', async ({ page }) => {
    await installDiagnostic(page, origins.insecure)
    await primeClipboard(page, 'old modal clipboard sentinel')
    await page.getByTestId('open-host-dialog').click()
    const hostDialog = page.getByRole('dialog', { name: 'Clipboard regression modal' })
    await expect(hostDialog).toBeVisible()

    const copy = hostDialog.getByTestId('dialog-copy')
    await copy.click()
    await expectNoRetryDialog(page)
    await expect(copy).toHaveAttribute('data-copied', 'true')
    await expect(copy).toBeFocused()
    await expect(hostDialog).toBeVisible()
    await expect.poll(() => pasteClipboard(page)).toBe('modal dialog clipboard payload')
    await screenshot(page, `${test.info().project.name}-real-dialog`)
  })

  test('failed legacy write preserves old clipboard and traps focus until trusted manual retry', async ({ page }) => {
    await installDiagnostic(page, origins.insecure)
    await primeClipboard(page, 'sentinel must survive failed write')
    await page.evaluate(() => {
      const win = window as WindowWithClipboardDiagnostics
      win.__realExecCommand = document.execCommand.bind(document)
      document.execCommand = () => false
    })

    const copy = page.getByTestId('assistant-copy').getByTestId('answer-copy').last()
    await copy.click()
    const dialog = page.getByRole('dialog', { name: 'Copy answer' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('button', { name: 'Copy now', exact: true })).toBeVisible()
    await expect(dialog.getByLabel('Text to copy')).toHaveValue('Final assistant clipboard answer')
    await expect(copy).not.toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe('sentinel must survive failed write')

    const retry = dialog.getByRole('button', { name: 'Copy now', exact: true })
    await retry.focus()
    await page.keyboard.press('Tab')
    expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true)
    await page.keyboard.press('Shift+Tab')
    expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true)
    await expect(page.getByTestId('outside-focus')).not.toBeFocused()

    // Simulate a transient first failure; recovery still requires the actual
    // trusted retry click from inside ClipboardDialog.
    await page.evaluate(() => {
      const win = window as WindowWithClipboardDiagnostics
      if (win.__realExecCommand) document.execCommand = win.__realExecCommand
    })
    await retry.click()
    await expect(dialog).toHaveCount(0)
    await expect(copy).toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe('Final assistant clipboard answer')
    await screenshot(page, `${test.info().project.name}-manual-retry`)
  })

  test('insecure HTTP deferred loader waits in ClipboardDialog and copies resolved text on retry', async ({ page }) => {
    await installDiagnostic(page, origins.insecure)
    await primeClipboard(page, 'old deferred clipboard sentinel')
    await page.getByTestId('deferred-copy').click()
    const dialog = page.getByRole('dialog', { name: 'Copy deferred text' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('status')).toBeVisible()
    const retry = dialog.getByRole('button', { name: 'Copy now', exact: true })
    await expect(retry).toBeDisabled()
    expect(await page.evaluate(() => typeof (window as WindowWithClipboardDiagnostics).__resolveDeferredCopy)).toBe('function')
    await page.evaluate(() => (window as WindowWithClipboardDiagnostics).__resolveDeferredCopy?.())
    await expect.poll(() => dialog.getByLabel('Text to copy').inputValue()).toBe('deferred clipboard payload')
    await expect(retry).toBeEnabled()
    await expect.poll(() => pasteClipboard(page)).toBe('old deferred clipboard sentinel')
    await retry.click()
    await expect(dialog).toHaveCount(0)
    await expect.poll(() => pasteClipboard(page)).toBe('deferred clipboard payload')
    await screenshot(page, `${test.info().project.name}-insecure-deferred`)
  })

  test('rejected deferred loader disables Copy now, preserves old clipboard, and dismisses without copy', async ({ page }) => {
    await installDiagnostic(page, origins.insecure)
    await primeClipboard(page, 'old rejected-loader clipboard sentinel')
    await page.getByTestId('rejected-deferred-copy').click()
    const dialog = page.getByRole('dialog', { name: 'Copy rejected deferred text' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('status')).toBeVisible()
    await expect(dialog.getByRole('button', { name: 'Copy now', exact: true })).toBeDisabled()
    expect(await page.evaluate(() => typeof (window as WindowWithClipboardDiagnostics).__rejectDeferredCopy)).toBe('function')

    await page.evaluate(() => (window as WindowWithClipboardDiagnostics).__rejectDeferredCopy?.())
    await expect(dialog.getByRole('alert')).toHaveText('Clipboard content could not be loaded. Close and try again.')
    await expect(dialog.getByLabel('Text to copy')).toHaveValue('')
    await expect(dialog.getByRole('button', { name: 'Copy now', exact: true })).toBeDisabled()
    await expect.poll(() => pasteClipboard(page)).toBe('old rejected-loader clipboard sentinel')

    await dialog.getByRole('button', { name: 'Close', exact: true }).first().click()
    await expect(dialog).toHaveCount(0)
    await expect(page.getByTestId('rejected-deferred-copy')).not.toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe('old rejected-loader clipboard sentinel')
    await screenshot(page, `${test.info().project.name}-rejected-deferred`)
  })

  test('mobile touchscreen keyboard tab and Enter copy through the real button', async ({ page }) => {
    await installDiagnostic(page, origins.insecure)
    await page.setViewportSize({ width: 390, height: 844 })
    await primeClipboard(page, 'old keyboard clipboard sentinel')
    const start = page.getByTestId('keyboard-copy-start')
    const copy = page.getByTestId('keyboard-copy')
    await start.focus()
    await page.keyboard.press('Tab')
    await expect(copy).toBeFocused()
    await page.keyboard.press('Enter')
    await expectNoRetryDialog(page)
    await expect(copy).toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe('keyboard clipboard payload')
    await screenshot(page, `${test.info().project.name}-mobile-keyboard-copy`)
  })

  test('secure-context deferred producer writes a ClipboardItem promise from trusted click', async ({ page }) => {
    await installDiagnostic(page, origins.secure)
    await primeClipboard(page, 'old secure clipboard sentinel')
    expect(await page.evaluate(() => typeof navigator.clipboard?.write)).toBe('function')
    expect(await page.evaluate(() => typeof ClipboardItem)).toBe('function')

    const copy = page.getByTestId('secure-deferred-copy')
    await copy.click()
    await expect.poll(() => page.evaluate(() => (window as WindowWithClipboardDiagnostics).__deferredCopyStarted)).toBe(true)
    expect(await page.evaluate(() => typeof (window as WindowWithClipboardDiagnostics).__resolveDeferredCopy)).toBe('function')
    await page.evaluate(() => (window as WindowWithClipboardDiagnostics).__resolveDeferredCopy?.())
    await expectNoRetryDialog(page)
    await expect(copy).toHaveAttribute('data-copied', 'true')
    await expect.poll(() => pasteClipboard(page)).toBe('deferred clipboard payload')
    await screenshot(page, `${test.info().project.name}-secure-deferred`)
  })
})
