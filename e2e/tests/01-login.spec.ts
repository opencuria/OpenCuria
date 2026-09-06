/**
 * 01-login.spec.ts — Test login flow and session persistence.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('01 — Login', () => {
  test('should show login page with email and password fields', async ({ page }) => {
    // Sign out first to get to login
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // If already logged in, sign out
    const signOut = page.getByRole('button', { name: 'Sign out' });
    if (await signOut.isVisible({ timeout: 3000 }).catch(() => false)) {
      await signOut.click();
      await page.waitForURL(/\/login/);
    } else {
      await page.goto(`${BASE_URL}/login`);
    }

    await expect(page.locator('input#email')).toBeVisible();
    await expect(page.locator('input#password')).toBeVisible();
    await expect(page.getByRole('button', { name: /sign in/i })).toBeVisible();
  });

  test('should reject invalid credentials', async ({ page }) => {
    await page.goto(`${BASE_URL}/login`);
    await page.waitForLoadState('networkidle');

    // If redirected to home (already logged in), sign out
    if (!page.url().includes('/login')) {
      const signOut = page.getByRole('button', { name: 'Sign out' });
      if (await signOut.isVisible({ timeout: 3000 }).catch(() => false)) {
        await signOut.click();
        await page.waitForURL(/\/login/);
        await page.waitForLoadState('networkidle');
      }
    }

    // Wait for inputs to be ready
    await expect(page.locator('input#email')).toBeVisible({ timeout: 5_000 });
    await page.locator('input#email').fill('wrong@example.com');
    await page.locator('input#password').fill('wrongpassword');
    await page.getByRole('button', { name: /sign in/i }).click();

    // Wait for the API call to complete and error to render
    await page.waitForTimeout(3000);

    // Should stay on login page
    expect(page.url()).toContain('/login');

    // Verify the error text exists in the DOM (the <p> element may not appear in accessibility tree)
    const hasError = await page.evaluate(() =>
      document.body.innerText.includes('Invalid email or password')
    );
    expect(hasError).toBe(true);
  });

  test('should login successfully and reach home', async ({ page }) => {
    await page.goto(`${BASE_URL}/login`);
    await page.waitForLoadState('networkidle');

    if (!page.url().includes('/login')) {
      // Already logged in — home screen shows the greeting
      await expect(page.getByTestId('chat-home')).toBeVisible({ timeout: 15_000 });
      return;
    }

    await page.locator('input#email').fill('admin@localhost.test');
    await page.locator('input#password').fill('OpenCuria2026!');
    await page.getByRole('button', { name: /sign in/i }).click();

    // Should redirect away from login
    await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15_000 });

    // Home screen shows the greeting after login
    await expect(page.getByTestId('chat-home')).toBeVisible({ timeout: 15_000 });
  });

  test('should persist session across navigation', async ({ authedPage: page }) => {
    // Already authed via fixture (settings routes redirect to / with sheet)
    await page.goto(`${BASE_URL}/?settings=skills`);
    await page.waitForLoadState('networkidle');
    expect(page.url()).not.toContain('/login');
    await expect(page.getByTestId('settings-sheet')).toBeVisible({ timeout: 10_000 });

    await page.goto(`${BASE_URL}/?settings=credentials`);
    await page.waitForLoadState('networkidle');
    expect(page.url()).not.toContain('/login');
    await expect(page.getByTestId('settings-sheet')).toBeVisible({ timeout: 10_000 });
  });

  test('should discover runners and save to test state', async ({ authedPage: page, testState }) => {
    // Use API to discover runners
    const runners = await api.get('/runners/');

    expect(Array.isArray(runners)).toBe(true);

    for (const runner of runners) {
      if (runner.available_runtimes?.includes('docker') && runner.status === 'online') {
        testState.dockerRunnerId = runner.id;
      }
      if (runner.available_runtimes?.includes('qemu')) {
        testState.qemuRunnerId = runner.id;
        testState.qemuRunnerOnline = runner.status === 'online';
      }
    }

    expect(testState.dockerRunnerId).toBeTruthy();
    console.log(`Docker runner: ${testState.dockerRunnerId}`);
    console.log(`QEMU runner: ${testState.qemuRunnerId || 'none'} (online: ${testState.qemuRunnerOnline})`);
  });
});
