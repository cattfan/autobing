import { expect, test } from '@playwright/test';

test.describe('dashboard shell', () => {
  test('renders real-style dashboard card data from browser mocks', async ({ page }) => {
    await page.goto('/');

    await expect(page.locator('#dashboard-view')).toHaveClass(/active/);
    const card = page.locator('#jobs-grid .account-card').first();
    await expect(card.getByRole('heading', { name: 'cattfan239@gmail.com' })).toBeVisible();
    await expect(card.getByText('11,953')).toBeVisible();
    await expect(card.locator('.metric').filter({ hasText: 'PC Search' }).locator('.metric-value')).toHaveText('90/90');
    await expect(card.locator('.metric').filter({ hasText: 'Mobile Search' }).locator('.metric-value')).toHaveText('60/60');
    await expect(card.locator('.metric').filter({ hasText: 'Daily Set' }).locator('.metric-value')).toHaveText('0/3');
    await expect(card.locator('.metric').filter({ hasText: 'Bing Search Streak' }).locator('.metric-value')).toHaveText('3/3');
    await expect(card.locator('.metric-span-2 .metric-value')).toHaveText('30/30');
  });

  test('renders settings controls including AI toggles', async ({ page }) => {
    await page.goto('/');

    await page.locator('.nav-item[data-target="settings-view"]').click();
    await expect(page.locator('#settings-view')).toHaveClass(/active/);
    await expect(page.locator('#setting-browser-type')).toBeVisible();
    await expect(page.locator('#setting-api-url')).toBeVisible();
    await expect(page.locator('#setting-ai-enabled')).toBeChecked({ checked: false });
    await expect(page.locator('#setting-page-agent-enabled')).toBeChecked({ checked: false });
    await expect(page.locator('#save-settings-btn')).toBeVisible();
  });
});
