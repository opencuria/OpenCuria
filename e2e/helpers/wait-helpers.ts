/**
 * Wait helpers for async UI operations.
 */
import type { Page } from '@playwright/test';
import { expect } from '@playwright/test';

/**
 * Wait for a toast notification containing the given text.
 */
export async function expectToast(page: Page, text: string | RegExp, timeout = 10_000): Promise<void> {
  const toastLocator = page.locator('[role="alert"], .toast, [class*="toast"], [class*="notification"]').first();
  await expect(toastLocator).toContainText(text, { timeout });
}

/**
 * Wait for text to appear anywhere on the page.
 */
export async function waitForText(page: Page, text: string, timeout = 15_000): Promise<void> {
  await expect(page.getByText(text).first()).toBeVisible({ timeout });
}

/**
 * Generic poll helper for the UI (checks a condition on the page).
 */
export async function pollPage(
  page: Page,
  action: () => Promise<boolean>,
  opts: { interval?: number; timeout?: number } = {},
): Promise<void> {
  const interval = opts.interval || 3000;
  const timeout = opts.timeout || 120_000;
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const result = await action();
    if (result) return;
    await page.waitForTimeout(interval);
  }
  throw new Error(`pollPage timed out after ${timeout}ms`);
}
