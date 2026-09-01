/** #90 in a real browser: the zone is a suffix, not a retype.
 *
 * ## Status when this landed — read this before believing it
 *
 * **This spec has never been executed.** It was written against the
 * harness #91 is standing up (`frontend/playwright.config.ts`,
 * `frontend/tests-e2e/`, `./helpers`), and at the time #90 was finished
 * #91 had not merged: there was a `playwright.config.ts` on its branch
 * and no `helpers.ts`, no `@playwright/test` dependency in
 * `frontend/package.json`, and no `make test-e2e`. So the file is
 * **runnable-but-unrun**, and saying so is the point — an unrun spec
 * described as a browser demonstration is worth less than no spec,
 * because it is believed.
 *
 * What *was* demonstrated for #90 is in `src/test/HostnamesPage.test.tsx`:
 * the composition table driven through the rendered form in jsdom, and
 * nine mutations of the composer each failing it. That is a DOM
 * instrument, not a browser one — which is exactly why #91 exists.
 *
 * ## The one coupling to reconcile on merge
 *
 * The import below binds to `./helpers`'s login helper by atrium's own
 * name (`loginAsSuperAdmin`, `API_URL` — see
 * `/Users/brendan/src/atrium/frontend/tests-e2e/helpers.ts`), because
 * #91's brief is to match atrium's helper vocabulary. If #91 landed a
 * different spelling, this is a one-line fix and the spec's body does
 * not change.
 *
 * ## What it demonstrates
 *
 * The zone is claimed over the API rather than through the UI — that
 * walk belongs to #91's own spec, and repeating it here would make this
 * file fail for reasons that are not about #90. Everything from the
 * register-a-name form onwards is done by clicking.
 */
import { expect, test } from '@playwright/test';

import { API_URL, loginAsSuperAdmin } from './helpers';

/** RFC 2606 §2 — reserved, resolves nowhere, and never a real estate
 *  name. Suffixed per run so repeated runs do not collide on
 *  `ddns_domain.name`, which is globally unique. */
const ZONE = `e2e-${Date.now()}.example.invalid`;

test.describe('the name field composes, and the server validates', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsSuperAdmin(page);
    const created = await page.request.post(`${API_URL}/atrium_ddns/domains`, {
      data: { name: ZONE },
    });
    // 201 the first time, 409 for every test after it: `ZONE` is
    // file-scoped and `ddns_domain.name` is unique across the whole
    // installation, so the second `beforeEach` re-claims a zone this
    // file already owns. Both are the intended state; anything else
    // (422, 403) still fails, and the body is named either way.
    // Adjusted by #91 on this spec's first actual run — a defect that
    // only exists once there is something to run it.
    expect(
      [201, 409],
      `zone claim failed: ${created.status()} ${await created.text()}`,
    ).toContain(created.status());
    // The board. `/atrium-ddns/names` is gone — the name modal is
    // `?name=` on the only tenant surface there is. Written out rather
    // than imported: `tsconfig.json` scopes type checking to `src`, so a
    // spec importing a constant from it would be the one file in the tree
    // the gate does not check.
    await page.goto('/atrium-ddns');
  });

  /** Open the modal and select the zone. Mantine's `Select` is not a
   *  `<select>` — `selectOption` does nothing on it and `getByLabel`
   *  matches the visible input, not an option list. Click it open, then
   *  click the option by role. */
  async function openFormAndPickZone(page: import('@playwright/test').Page) {
    await page.getByTestId('board-add-name').click();
    await expect(page.getByTestId('hostname-name')).toBeVisible();
    await page.getByTestId('hostname-zone').click();
    await page.getByRole('option', { name: ZONE, exact: true }).click();
  }

  test('the zone is rendered inside the field, and a bare label is enough', async ({
    page,
  }) => {
    await openFormAndPickZone(page);
    // The defect, gone: the zone is on screen beside the field, so
    // there is nothing to retype.
    // The  echo beside the field is gone — it repeated the zone
    // select next to it, and `will send:` below already shows the
    // composed result, which is the string that actually leaves the
    // browser. That preview is what this spec asserts instead.

    await page.getByTestId('hostname-name').fill('home');
    // The preview is read as its own node, not as a substring of the
    // sentence around it — `home.<zone>` is a substring of
    // `home.<zone>..<zone>`, so a `toContainText` here would pass on a
    // doubled composition.
    await expect(page.getByTestId('hostname-will-send')).toHaveText(
      `home.${ZONE}`,
    );

    await page.getByTestId('name-submit').click();
    // The row the list draws carries the composed name, which is the
    // only reading that proves the composition survived the round trip
    // rather than merely being rendered.
    await expect(
        page.getByTestId(`board-row-home.${ZONE}-none`),
      ).toBeVisible();
  });

  test('a pasted FQDN is not suffixed twice', async ({ page }) => {
    await openFormAndPickZone(page);
    // What an operator does with a name out of a zone file or a ticket.
    await page.getByTestId('hostname-name').fill(`attic.${ZONE}`);
    await expect(page.getByTestId('hostname-will-send')).toHaveText(
      `attic.${ZONE}`,
    );
    await page.getByTestId('name-submit').click();
    await expect(
        page.getByTestId(`board-row-attic.${ZONE}-none`),
      ).toBeVisible();
    // …and the doubled form is not what was stored. Named explicitly
    // rather than left to the positive assertion, because "the right
    // row exists" and "the wrong row does not" are two facts.
    await expect(
      page.getByTestId(`hostname-attic.${ZONE}.${ZONE}`),
    ).toHaveCount(0);
  });

  test('a name the server refuses is still sent, and the refusal renders', async ({
    page,
  }) => {
    // The guard against the second validator, in a browser. An
    // underscore is the first thing any client-side hostname regex
    // blocks; `providers/base.py`'s `_LABEL` refuses it, and that
    // refusal has to arrive from the server.
    await openFormAndPickZone(page);
    await page.getByTestId('hostname-name').fill('bad_label');
    await expect(page.getByTestId('hostname-will-send')).toHaveText(
      `bad_label.${ZONE}`,
    );
    // Nothing in the browser blocked it.
    await expect(page.getByTestId('name-submit')).toBeEnabled();
    await page.getByTestId('name-submit').click();

    const refusal = page.getByTestId('name-error');
    await expect(refusal).toBeVisible();
    // The server's own words, verbatim — including the wire status the
    // same name would have produced.
    await expect(refusal).toContainText('notfqdn');
    // And no row was created, so the refusal is the server's and not a
    // rendering of a success.
    await expect(page.getByTestId(`hostname-bad_label.${ZONE}`)).toHaveCount(0);
  });
});
