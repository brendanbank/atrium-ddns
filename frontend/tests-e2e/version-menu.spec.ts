import { expect, test } from '@playwright/test';

import { API_URL, loginAsSuperAdmin } from './helpers';

/**
 * The build stamp this repo bakes into its own image, read back the way
 * an operator reads it: from the user menu of a running stack.
 *
 * atrium 0.30 added `GET /api/version` and the two-line block at the top
 * of the signed-in menu — the atrium layer, and the host layer stacked on
 * it. atrium's own suite covers the rendering; what is unproven *here* is
 * the wiring that fills the second line: the `ATRIUM_APP_*` ARG/ENV pair
 * in this repo's Dockerfile, fed by the Makefile locally and by
 * `release.yml` for a published image.
 *
 * That wiring fails silently. A build arg the Dockerfile does not declare
 * is ignored by buildx without a warning, an ENV dropped from the runtime
 * stage still produces a working image, and the only symptom either way is
 * a user menu that quietly stops naming which build is deployed — noticed
 * months later, during the incident where it mattered.
 *
 * The spec asserts against `/api/version` first and the menu second, so a
 * failure says which half broke: no stamp in the image, or a stamp the UI
 * did not render.
 */
test.describe('Version in the user menu', () => {
  test.describe.configure({ timeout: 30_000 });

  test('the menu names this build of atrium-ddns, not just atrium', async ({
    page,
  }) => {
    await loginAsSuperAdmin(page);

    // `page.request` shares the context's cookie jar, so this is the
    // same authenticated caller the browser is about to be.
    const response = await page.request.get(`${API_URL}/version`);
    expect(response.ok(), `GET /version -> ${response.status()}`).toBeTruthy();
    const info = await response.json();

    // The atrium half rides in as inherited ENV from the base image.
    // Asserted because its absence would mean the pinned base predates
    // 0.30 — a different failure than a broken stamp of our own, and
    // one this spec should not report as ours.
    expect(info.atrium?.version ?? info.atrium?.commit).toBeTruthy();

    // The half this repo is responsible for. `app: null` is exactly what
    // a bare atrium returns, so it is the shape a dropped build arg
    // produces.
    expect(
      info.app,
      'the image carries no ATRIUM_APP_* stamp — check the Dockerfile ' +
        'build args reached the runtime stage',
    ).not.toBeNull();

    // Tag if there is one, commit otherwise. A release build is tagged;
    // a CI checkout has no tags, so it stamps the sha alone — both are
    // correct, and which one appears is not this spec's business.
    const stamp: string = info.app.version ?? info.app.commit.slice(0, 7);
    expect(stamp).toBeTruthy();

    await page.goto('/');
    await page.getByTestId('user-menu').click();

    const versions = page.getByTestId('version-info');
    await expect(versions).toBeVisible();
    await expect(versions).toContainText(stamp);
  });
});
