/**
 * Navigation helpers for the OpenCuria sidebar.
 */
import type { Page } from '@playwright/test';

export async function navigateTo(page: Page, name: string): Promise<void> {
  const routes: Record<string, string> = {
    dashboard: '/',
    workspaces: '/workspaces',
    runners: '/runners',
    images: '/images',
    skills: '/skills',
    credentials: '/credentials',
    'api-keys': '/api-keys',
    'org-settings': '/org-settings',
  };
  const path = routes[name];
  if (!path) throw new Error(`Unknown route: ${name}`);
  await page.goto(path);
  await page.waitForLoadState('networkidle');
}

export async function navigateToOrgSettingsTab(page: Page, tabName: string): Promise<void> {
  await navigateTo(page, 'org-settings');
  // Click the tab by its text
  await page.getByRole('tab', { name: tabName }).or(page.locator(`button:has-text("${tabName}")`)).first().click();
  await page.waitForTimeout(500);
}
