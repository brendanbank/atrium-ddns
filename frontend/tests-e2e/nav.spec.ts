import { expect, test } from '@playwright/test';

import {
  DDNS_NAV_ITEMS,
  loginAsUser,
} from './helpers';

/**
 * Spec 1 of the floor: a tenant logs in and the atrium-ddns nav items
 * render.
 *
 * This is the assertion nobody had ever made in a browser. Everything
 * up to V1M4 was demonstrated over HTTP — the bundle's *bytes* were
 * grepped for `atrium-ddns-names` (`ui-parity.md` §3.3.1 step 2), which
 * proves the string is in the file and says nothing about whether
 * atrium's registry ever called it, whether React mounted it, or
 * whether a tenant with only the `user` role can see it.
 *
 * So the assertions are deliberately about *rendering*: the seven nav
 * items in the shell's own navigation landmark, and then one of them
 * clicked through to a host route that draws its own surface.
 */
test.describe('atrium-ddns nav', () => {
  test.describe.configure({ timeout: 30_000 });

  test('a user-role tenant sees every registered nav item', async ({
    page,
  }) => {
    await loginAsUser(page);
    await page.goto('/');

    // The bundle is loaded asynchronously by the SPA after
    // /api/app-config resolves `system.host_bundle_url`, so the first
    // nav item is the one that waits.
    const nav = page.getByRole('navigation');
    await expect(
      nav.getByRole('link', { name: DDNS_NAV_ITEMS[0].label, exact: true }),
    ).toBeVisible({ timeout: 8_000 });

    for (const item of DDNS_NAV_ITEMS) {
      const link = nav.getByRole('link', { name: item.label, exact: true });
      await expect(link, `nav item ${item.label}`).toBeVisible();
      await expect(link, `nav item ${item.label} -> ${item.to}`).toHaveAttribute(
        'href',
        item.to,
      );
    }
  });

  test('the board nav item reaches a host route that renders', async ({
    page,
  }) => {
    await loginAsUser(page);
    await page.goto('/');

    const nav = page.getByRole('navigation');
    const boardLink = nav.getByRole('link', {
      name: 'Devices and names',
      exact: true,
    });
    await expect(boardLink).toBeVisible({ timeout: 8_000 });
    await boardLink.click();

    // The item points at the host root now, which renders the board.
    // `BOARD_PATH` still resolves and renders the same page — it is
    // simply no longer where the sidebar sends you.
    await expect(page).toHaveURL(/\/atrium-ddns$/);
    await expect(
      page.getByRole('heading', { name: 'Devices and names' }),
    ).toBeVisible();
    // A fresh tenant owns no devices, so the board's own empty state is
    // the proof that the *page* rendered rather than a spinner or a
    // refusal: `board-refused` would mean the permission grant never
    // reached this account, `board-error` that the query failed.
    await expect(page.getByTestId('board-empty')).toBeVisible();
    await expect(page.getByTestId('board-refused')).toHaveCount(0);
    await expect(page.getByTestId('board-error')).toHaveCount(0);
  });
});
