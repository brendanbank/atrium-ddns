import { expect, test } from '@playwright/test';

/** Not a secret, and shaped like one on purpose: the credential has to
 *  reach the encrypted column for the form to be exercised, and a value
 *  that looks like a key is what a reader checks is never echoed back. */
const DEMO_CREDENTIAL: Record<string, string> = {
  aws_access_key_id: 'AKIA-E2E-NOT-A-SECRET',
  aws_secret_access_key: 'e2e-not-a-secret',
};

import {
  API_URL,
  DEVICES_PATH,
  DOC_ADDRESS_V4,
  DOMAINS_PATH,
  NAMES_PATH,
  bindScriptedBackend,
  chooseFromSelect,
  deviceCallsIn,
  loginAsUser,
  uniqueDeviceName,
  uniqueZoneName,
  watchPageErrors,
} from './helpers';

/**
 * Spec 2 of the floor, and the one this milestone exists for: the
 * `ui-parity.md` §3.3.1 walk — zone, provider, device, name — driven
 * through the **UI** by a `user`-role tenant, ending with a **rendered
 * resolution strip**.
 *
 * §3.3.1 drove the same walk over HTTP and printed the strip as JSON.
 * That proves the board's *arithmetic*. It does not prove that anything
 * draws it: `ResolutionStrip.tsx` is the milestone's signature element
 * (`ui-design.md` §4) and until this spec ran, no browser had ever
 * rendered one.
 *
 * Two things about the walk are worth reading before changing it:
 *
 *   1. **Step 7 of §3.3.1 reproduces here, in the browser.** Zone +
 *      provider + device + name gives a hostname with *zero* strips,
 *      and that is the correct answer — `_strips_for` renders a family
 *      only once the name has been published or answered in it. The
 *      strip needs a fourth step, and the fourth step is the device
 *      doing the thing a device exists for.
 *   2. **The provider binding is the one call this spec makes over
 *      HTTP**, for a product reason documented on `bindScriptedBackend`
 *      in `helpers.ts`: the UI's provider `Select` offers
 *      `known_services()`, and the compat fixture's scripted slots are
 *      deliberately withheld from it by a guard in
 *      `backend/tests/test_compat_stub.py`. The modal that would do it
 *      is still opened and asserted below, so the surface is covered
 *      even though the click cannot complete on a hermetic stack.
 */
test.describe('the §3.3.1 walk, through the UI', () => {
  test.describe.configure({ timeout: 30_000 });

  test('zone, provider, device, name — and a rendered strip', async ({
    page,
  }, testInfo) => {
    // The §3.3.1 walk, driven through the UI, ending at a strip a browser
    // has actually drawn.
    //
    // **Rewritten for the board's flat table.** The walk used to end on
    // the board, which drew a rail per name. The board is a table now —
    // one row per name per family — because the nested layout changed
    // shape as check results arrived. The strip survives where it is
    // still the right drawing: inside one device's card. So the walk
    // ends there, and the board is asserted as the table it is.
    //
    // It also used to create the zone with **no** provider, through an
    // "add a provider later" link, and add one afterwards. That link is
    // gone: a zone with no provider answers `911` for every update under
    // it, and the operator ruled that a form whose only escape hatch
    // produces that state is offering a trap. The zone is created with
    // its provider in one submission.
    // `stillAlive()` is checked between steps rather than only at the
    // end, and that is the whole of #122's second half. An uncaught
    // throw inside the host tree makes React unmount the *root* — the
    // dialog and the list go together and the `main` landmark is left
    // empty — so the next locator assertion fails with "element(s) not
    // found" and names the element instead of the crash. Three agents
    // read that report and called it a flake. Asserting the error
    // channel between steps makes the spec say what actually happened.
    const stillAlive = watchPageErrors(page);

    const zone = uniqueZoneName();
    const deviceName = uniqueDeviceName();
    const hostname = `home.${zone}`;

    await loginAsUser(page);

    // --- 1. the zone and its provider, in one submission --------------
    await page.goto(DOMAINS_PATH);
    await page.getByTestId('add-domain').click();
    const zoneModal = page.getByRole('dialog');
    await expect(zoneModal).toBeVisible();
    // Wait for the **body**, not the shell. The dialog frame appears
    // immediately; the form inside renders only once the zone and
    // provider queries have resolved, and until then `ZoneModalBody`
    // renders "Loading…". Filling the name auto-waits and so happened
    // to work, which made the miss surface later and elsewhere — on
    // the provider select, in one run out of three.
    await expect(zoneModal.getByTestId('zone-modal-body')).toBeVisible({
      timeout: 8_000,
    });
    await zoneModal.getByTestId('zone-name').fill(zone);
    await chooseFromSelect(page, 'zone-provider', 'route53');
    for (const [key, value] of Object.entries(DEMO_CREDENTIAL)) {
      await zoneModal.getByTestId(`zone-credential-field-${key}`).fill(value);
    }
    // #122's crash landed exactly here — on the credential field's
    // change handler, which read `event.currentTarget.value` from
    // inside a functional `setState`.
    await stillAlive('after filling the credential fields');
    // Assert the select took before touching the submit. `zone-submit`
    // is disabled until a provider is chosen, so a click after a
    // select that silently missed does not fail — it waits for the
    // button to become actionable and times out naming the button
    // rather than the cause. Same guard as `zone-provider.spec.ts`.
    await expect(zoneModal.getByTestId('zone-provider')).toHaveValue(
      'route53',
    );
    await expect(zoneModal.getByTestId('zone-submit')).toBeEnabled();
    await zoneModal.getByTestId('zone-submit').click();

    const zoneRow = page.getByTestId(`domain-${zone}`);
    await expect(zoneRow).toBeVisible({ timeout: 8_000 });
    // Not diverged: §1.2 Rule 1 — agreement has no colour, so a working
    // zone carries no mark at all.
    await expect(zoneRow).toHaveAttribute('data-diverged', 'false');
    await expect(page.getByTestId(`provider-${zone}`)).toContainText('route53');

    // --- 2. the scripted backend, over HTTP ---------------------------
    // The one step not driven through the UI, and deliberately so: a
    // strip renders only once a name has been *published*, `last_ip_*` is
    // written on `good` only, and the only provider that answers `good`
    // without contacting a real nameserver is the compat stub — which
    // `known_services()` withholds from the catalogue on purpose.
    // Widening the catalogue to make this clickable would delete a guard
    // the product wrote deliberately.
    const domains = await (
      await page.request.get(`${API_URL}/atrium_ddns/domains`)
    ).json();
    const domainId = (
      domains as Array<{ id: number; name: string }>
    ).find((d) => d.name === zone)!.id;
    await bindScriptedBackend(page.request, domainId);

    await stillAlive('while creating the zone and its provider');
    // --- 3. the device ------------------------------------------------
    await page.goto(DEVICES_PATH);
    await page.getByTestId('add-device').click();
    const deviceModal = page.getByRole('dialog');
    await deviceModal.getByTestId('device-name').fill(deviceName);
    await deviceModal.getByTestId('device-submit').click();
    // The secret is shown once, over the create form, and dismissing it
    // closes both — the operator asked for that shape explicitly.
    const secret = page.getByTestId('device-secret-once');
    await expect(secret).toBeVisible({ timeout: 8_000 });
    const username = await page.getByTestId('issued-username').innerText();
    const password = await page.getByTestId('issued-secret').innerText();
    await page.getByTestId('dismiss-secret').click();

    await stillAlive('while creating the device');
    // --- 4. the name --------------------------------------------------
    await page.goto(NAMES_PATH);
    await page.getByTestId('add-hostname').click();
    const nameModal = page.getByRole('dialog');
    await expect(nameModal).toBeVisible();
    await chooseFromSelect(page, 'hostname-zone', zone);
    await nameModal.getByTestId('hostname-name').fill('home');
    await expect(nameModal.getByTestId('hostname-will-send')).toHaveText(
      hostname,
    );
    await chooseFromSelect(page, 'hostname-device', deviceName);
    await nameModal.getByTestId('name-submit').click();
    await expect(page.getByTestId(`hostname-${hostname}`)).toBeVisible({
      timeout: 8_000,
    });

    // --- 5. the device calls in, as a router does ---------------------
    const update = await deviceCallsIn(page.request, {
      username,
      secret: password,
      hostname,
      ip: DOC_ADDRESS_V4,
    });
    //  returns { status, body } — the status matters as
    // much as the word, because the v2 protocol answers 200 for refusals
    // too and a body check alone would pass on a 500 that happened to
    // contain the string.
    expect(update.status, 'the wire did not answer 200').toBe(200);
    expect(update.body, 'the wire did not answer good').toContain('good');

    await stillAlive('while creating the name');
    // --- 6. the board, as a table -------------------------------------
    await page.goto('/atrium-ddns');
    const row = page.getByTestId(`board-row-${hostname}-A`);
    await expect(row).toBeVisible({ timeout: 8_000 });
    await expect(row).toContainText(DOC_ADDRESS_V4);
    await expect(row).toContainText(deviceName);

    await stillAlive('while rendering the board');
    // --- 7. the strip, where it still lives ---------------------------
    await row.getByTestId(`board-open-${deviceName}`).click();
    const card = page.getByTestId('device-detail');
    await expect(card).toBeVisible({ timeout: 8_000 });
    // Either rendering counts. §3.4 collapses a strip whose joints all
    // agree, and which one you get depends on whether the health check
    // has run — so pinning one testid makes this pass or fail on a
    // scheduler, not on whether a strip was drawn.
    const strip = page.locator(
      `[data-testid="strip-${hostname}-A"], ` +
        `[data-testid="strip-collapsed-${hostname}-A"]`,
    );
    await expect(strip).toBeVisible({ timeout: 8_000 });
    // The three stations, and the published address among them. A strip
    // that rendered its frame and no values would satisfy `toBeVisible`.
    await expect(strip).toContainText(DOC_ADDRESS_V4);

    await testInfo.attach('resolution-strip.png', {
      body: await card.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    await stillAlive('at the end of the walk');
  });
});
