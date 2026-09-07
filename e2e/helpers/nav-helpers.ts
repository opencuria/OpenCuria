/**
 * Navigation helpers for the OpenCuria home screen + settings sheet.
 *
 * (Schritt 6: alte Settings-Routen sind Redirects auf `/?settings=<tab>`;
 *  UI-Interaktion läuft über das Sheet auf `/`.)
 */
import type { Page } from '@playwright/test';

export async function navigateTo(page: Page, name: string): Promise<void> {
  const routes: Record<string, string> = {
    dashboard: '/',
    home: '/',
    workspaces: '/workspaces',
    // Legacy settings routes redirect to /?settings=<tab> (sheet opens there).
    runners: '/?settings=runners',
    images: '/?settings=images',
    skills: '/?settings=skills',
    credentials: '/?settings=credentials',
    'api-keys': '/?settings=api-keys',
    'org-settings': '/?settings=organization',
  };
  const path = routes[name];
  if (!path) throw new Error(`Unknown route: ${name}`);
  await page.goto(path);
  await page.waitForLoadState('networkidle');
}

export async function navigateToOrgSettingsTab(page: Page, tabName: string): Promise<void> {
  await navigateTo(page, 'org-settings');
  // Sheet-Nav: role="tab" mit data-testid settings-nav-<id>
  await page.getByRole('tab', { name: tabName }).first().click();
  await page.waitForTimeout(500);
}
