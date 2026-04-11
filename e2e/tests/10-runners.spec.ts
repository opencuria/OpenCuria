/**
 * 10-runners.spec.ts — Test runner list and monitoring.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('10 — Runners', () => {
  test('should display runners page with at least one runner', async ({ authedPage: page }) => {
    await page.goto(`${BASE_URL}/runners`);
    await page.waitForLoadState('networkidle');

    // Should show runners heading
    await expect(page.getByRole('heading', { name: /runner/i }).first()).toBeVisible();

    // At least one runner should be shown
    const runners = await api.get('/runners/');
    expect(runners.length).toBeGreaterThan(0);

    // The Docker runner should be visible
    const dockerRunner = runners.find((r: any) => r.available_runtimes?.includes('docker'));
    if (dockerRunner) {
      await expect(page.getByText(dockerRunner.name)).toBeVisible({ timeout: 10_000 });
    }
  });

  test('should show runner details and status', async ({ authedPage: page }) => {
    await page.goto(`${BASE_URL}/runners`);
    await page.waitForLoadState('networkidle');

    // Should show status badges (online/offline)
    const hasOnline = await page.getByText(/online/i).isVisible({ timeout: 5_000 }).catch(() => false);
    const hasOffline = await page.getByText(/offline/i).isVisible({ timeout: 5_000 }).catch(() => false);
    expect(hasOnline || hasOffline).toBe(true);
  });

  test('should fetch runner metrics via API', async ({ testState }) => {
    test.skip(!testState.dockerRunnerId, 'No Docker runner');

    const metrics = await api.get(`/runners/${testState.dockerRunnerId}/metrics/latest/`);
    // Metrics might be empty if no data, but API should not error
    console.log(`Runner metrics: ${JSON.stringify(metrics)?.substring(0, 200)}`);
  });
});
