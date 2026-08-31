import { expect, test } from '@playwright/test';

import { loginAsAdmin, loginAsUser } from './helpers';

/**
 * Spec 3 of the floor: one surface a `user`-role tenant must not see.
 *
 * The surface is the DDNS configuration pages (`ui-parity.md`'s #73
 * settings group). They are gated on `app_setting.manage`, which
 * `0001_init` grants to `admin` and not to `user`, and they are the
 * installation-wide knobs — a rate limit or a retention window changed
 * by a tenant is changed for every tenant.
 *
 * **Both halves run, in one spec, against the same URL.** A refusal
 * assertion on its own is the classic probe that cannot fail: a typo in
 * the path, a bundle that never loaded, a 404 — every one of them
 * renders "the settings form is not here" just as convincingly as the
 * permission check does. So the admin half is the control, and it
 * asserts the *positive*: the same route, for an account that holds the
 * permission, draws the form.
 */
test.describe('DDNS configuration is admin-only', () => {
  test.describe.configure({ timeout: 30_000 });

  const RATE_LIMITS_PATH = '/atrium-ddns/settings/rate-limits';

  test('a user-role tenant is refused, in the page', async ({ page }) => {
    await loginAsUser(page);
    await page.goto(RATE_LIMITS_PATH);

    const refusal = page.getByTestId('settings-refused');
    await expect(refusal).toBeVisible({ timeout: 8_000 });
    await expect(refusal).toContainText('app_setting.manage');
    // The form itself is absent, not merely hidden behind the alert.
    await expect(page.getByTestId('settings-blurb')).toHaveCount(0);

    // …and the nav never offered it. The route stays registered on
    // purpose — hiding it would turn "you may not read this" into "this
    // does not exist" — but the sidebar group is `perm`-gated.
    await expect(
      page.getByRole('navigation').getByText('DDNS configuration'),
    ).toHaveCount(0);
  });

  test('an admin sees the same page render — the control', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(RATE_LIMITS_PATH);

    await expect(page.getByTestId('settings-blurb')).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByTestId('settings-refused')).toHaveCount(0);
  });
});
