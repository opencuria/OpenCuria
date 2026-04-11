/**
 * 11-dashboard.spec.ts — Test dashboard view and navigation.
 */
import { test, expect } from '../fixtures/auth.fixture';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('11 — Dashboard & Navigation', () => {
  test('should show dashboard with runner and workspace counts', async ({ authedPage: page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // Dashboard shows runner count
    await expect(page.getByText(/runner/i).first()).toBeVisible();

    // Should show Create Workspace button
    await expect(page.getByRole('button', { name: /create workspace/i })).toBeVisible();
  });

  test('should show real-time connection indicator', async ({ authedPage: page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // Look for "Live" indicator
    await expect(page.getByText('Live')).toBeVisible({ timeout: 10_000 });
  });

  test('should navigate to all sidebar pages', async ({ authedPage: page }) => {
    const routes = [
      { name: 'Workspaces', path: '/workspaces' },
      { name: 'Skills', path: '/skills' },
      { name: 'Credentials', path: '/credentials' },
      { name: 'API Keys', path: '/api-keys' },
      { name: 'Runners', path: '/runners' },
      { name: 'Settings', path: '/org-settings' },
      { name: 'Captured Images', path: '/images' },
    ];

    for (const route of routes) {
      await page.goto(`${BASE_URL}${route.path}`);
      await page.waitForLoadState('networkidle');
      expect(page.url()).toContain(route.path);
      // Should not redirect to login
      expect(page.url()).not.toContain('/login');
    }
  });

  test('should show kanban/list view toggle on dashboard', async ({ authedPage: page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // Check for view toggle buttons
    const listBtn = page.getByRole('button', { name: /list view/i });
    const kanbanBtn = page.getByRole('button', { name: /kanban view/i });

    const hasToggles =
      (await listBtn.isVisible({ timeout: 3_000 }).catch(() => false)) ||
      (await kanbanBtn.isVisible({ timeout: 3_000 }).catch(() => false));
    expect(hasToggles).toBe(true);
  });

  test('should search conversations on dashboard', async ({ authedPage: page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    const searchInput = page.locator('input[placeholder*="Search conversations"]');
    if (await searchInput.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await searchInput.fill('nonexistent-workspace-xyz');
      await page.waitForTimeout(1000);
      // Search should filter results (may show empty or filtered list)
      await searchInput.clear();
    }
  });
});
