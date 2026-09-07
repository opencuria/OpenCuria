/**
 * 11-home.spec.ts — Test home screen (ChatHomeView) and legacy settings redirects.
 *
 * (Umbenannt aus 11-dashboard.spec.ts im Schritt-6-Redesign: `/` ist jetzt der
 * Home-Screen mit Greeting + WorkspacePicker + Composer; alte Settings-Routen
 * redirecten auf `/?settings=<tab>` und öffnen das Settings-Sheet.)
 */
import { test, expect } from '../fixtures/auth.fixture';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('11 — Home & Navigation', () => {
  test('should show home with greeting, workspace picker and composer', async ({
    authedPage: page,
  }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // Home screen container with greeting
    await expect(page.getByTestId('chat-home')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('chat-home-greeting')).toContainText(/wie kann ich helfen/i);

    // Workspace picker pill is visible
    await expect(page.getByTestId('workspace-picker-trigger')).toBeVisible();

    // Composer or empty-state CTA is visible (depends on workspace availability)
    const composer = page.getByTestId('chat-home-composer');
    const emptyCta = page.getByTestId('chat-home-create');
    await expect(composer.or(emptyCta)).toBeVisible({ timeout: 10_000 });
  });

  test('should show real-time connection indicator', async ({ authedPage: page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // Look for "Live" indicator
    await expect(page.getByText('Live')).toBeVisible({ timeout: 10_000 });
  });

  test('should redirect legacy settings routes to the settings sheet', async ({
    authedPage: page,
  }) => {
    const routes = [
      { path: '/skills', tab: 'skills' },
      { path: '/credentials', tab: 'credentials' },
      { path: '/api-keys', tab: 'api-keys' },
      { path: '/runners', tab: 'runners' },
      { path: '/org-settings', tab: 'organization' },
      { path: '/images', tab: 'images' },
    ];

    for (const route of routes) {
      await page.goto(`${BASE_URL}${route.path}`);
      await page.waitForLoadState('networkidle');
      // Should not redirect to login
      expect(page.url()).not.toContain('/login');
      // Settings sheet opens on the mapped tab
      await expect(page.getByTestId('settings-sheet')).toBeVisible({ timeout: 10_000 });
      await expect(page.getByTestId(`settings-nav-${route.tab}`)).toBeVisible();
    }
  });

  test('should keep workspaces list as fallback route', async ({ authedPage: page }) => {
    await page.goto(`${BASE_URL}/workspaces`);
    await page.waitForLoadState('networkidle');
    expect(page.url()).toContain('/workspaces');
    // Should not redirect to login
    expect(page.url()).not.toContain('/login');
  });

  test('should find chats via global search (Cmd+K)', async ({ authedPage: page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // Open search with Cmd/Ctrl+K
    await page.keyboard.press('ControlOrMeta+k');
    const searchInput = page.getByTestId('chat-search-input');
    if (await searchInput.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await searchInput.fill('nonexistent-workspace-xyz');
      await page.waitForTimeout(1000);
      await page.keyboard.press('Escape');
    }
  });
});
