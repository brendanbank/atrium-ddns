import { expect, test } from '@playwright/test';

import {
  API_URL,
  BOARD_PATH,
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
} from './helpers';
import {
  PUBLISHES_NOWHERE,
  WIRE_CONSEQUENCE,
} from '../src/tenant/ZoneStatus';

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
  test.describe.configure({ timeout: 120_000 });

  test('zone, provider, device, name — and a rendered strip', async ({
    page,
  }, testInfo) => {
    // Surface a crashing render rather than waiting 30 s for a locator
    // on a blank page — the Mantine failure mode the playwright-debug
    // skill catalogues.
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);

    const zone = uniqueZoneName();
    const deviceName = uniqueDeviceName();
    const fqdn = `home.${zone}`;

    // --- 1. a zone, through the UI ---------------------------------
    //
    // Deliberately with **no** provider, which since #88 is a departure
    // from the form's own reading order rather than the thing that
    // happens when you fill in the only field on offer: the link has to
    // be taken and the consequence confirmed. The walk takes it because
    // the provider it needs is not one the catalogue offers (see step
    // 2), and because the zero-provider state is worth rendering once.
    await page.goto(DOMAINS_PATH);
    await expect(page.getByTestId('domains-empty')).toBeVisible({
      timeout: 15_000,
    });
    await page.getByTestId('add-domain').click();
    const zoneModal = page.getByRole('dialog');
    await expect(zoneModal).toBeVisible();
    await zoneModal.getByTestId('zone-name').fill(zone);
    // Neither the consequence nor its confirm button exists until the
    // link is taken.
    await expect(zoneModal.getByTestId('zone-later-warning')).toHaveCount(0);
    await zoneModal.getByTestId('zone-later-link').click();
    await expect(zoneModal.getByTestId('zone-later-warning')).toBeVisible();
    await zoneModal.getByTestId('zone-later-submit').click();
    await expect(page.getByRole('dialog')).toHaveCount(0);

    const zoneRow = page.getByTestId(`domain-${zone}`);
    await expect(zoneRow).toBeVisible();
    // §10.1: a zone with no provider is the *exceptional* row, in the
    // operator's terms rather than the protocol's. Asserted against
    // `ZoneStatus`'s own exported strings — a spec that retyped them
    // would keep passing after the product stopped saying them.
    await expect(zoneRow).toHaveAttribute('data-diverged', 'true');
    await expect(page.getByTestId(`nowhere-${zone}`)).toContainText(
      PUBLISHES_NOWHERE,
    );
    await expect(page.getByTestId(`nowhere-why-${zone}`)).toContainText(
      WIRE_CONSEQUENCE,
    );
    await expect(page.getByTestId(`providers-${zone}`)).toHaveText(
      '0 providers',
    );

    // --- 2. the provider ------------------------------------------
    // On the zone's own route since #88 — §12's linkable destination,
    // reached the way an operator reaches it, by clicking the zone.
    await page.getByTestId(`open-domain-${zone}`).click();
    await expect(page.getByTestId(`zone-${zone}`)).toBeVisible();
    await expect(page.getByTestId('zone-no-providers')).toBeVisible();

    // The modal, opened and read: it is the surface an operator uses,
    // and the catalogue it offers is a property of the build.
    await page.getByTestId('zone-add-backend').click();
    const providerModal = page.getByRole('dialog');
    await expect(providerModal).toBeVisible();
    await providerModal.getByTestId('backend-service').click();
    // The three adapters this build ships (`providers.known_services()`).
    // Read as a set from the dropdown rather than asserted one by one,
    // so a fourth adapter fails this loudly instead of passing quietly.
    const offered = await page.getByRole('option').allTextContents();
    expect(offered.sort()).toEqual(['hetzner', 'nsupdate', 'route53']);
    // Twice: the first closes the Select's dropdown, the second the
    // modal. Two unconditional presses rather than a branch on which
    // one the first landed on.
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);

    // The binding itself — §3.3.1 step 4, over HTTP, for the reason on
    // `bindScriptedBackend`.
    const domains = await page.request.get(`${API_URL}/atrium_ddns/domains`);
    expect(domains.ok()).toBeTruthy();
    const owned = (await domains.json()) as Array<{ id: number; name: string }>;
    const domainId = owned.find((entry) => entry.name === zone)?.id;
    expect(domainId, `the zone ${zone} should exist after the UI created it`)
      .toBeDefined();
    const binding = await bindScriptedBackend(page.request, domainId!);
    expect(binding.credentials_set).toBe(true);

    // …and read back through the UI, which is what an operator sees:
    // on the zone's route, and — the mark being a *measurement* rather
    // than a decoration — gone from the list row.
    await page.reload();
    await expect(
      page.getByTestId(`backend-${binding.backend_type}`),
    ).toBeVisible();
    await expect(page.getByTestId(`credentials-${binding.id}`)).toHaveText(
      'credential stored',
    );
    await expect(page.getByTestId('zone-no-providers')).toHaveCount(0);

    await page.goto(DOMAINS_PATH);
    await expect(zoneRow).toHaveAttribute('data-diverged', 'false');
    await expect(page.getByTestId(`nowhere-${zone}`)).toHaveCount(0);
    await expect(page.getByTestId(`providers-${zone}`)).toHaveText(
      '1 provider',
    );

    // --- 3. a device, through the UI ------------------------------
    await page.goto(DEVICES_PATH);
    await expect(page.getByTestId('devices-empty')).toBeVisible();
    await page.getByTestId('add-device').click();
    const deviceModal = page.getByRole('dialog');
    await expect(deviceModal).toBeVisible();
    await deviceModal.getByTestId('device-name').fill(deviceName);
    await deviceModal.getByTestId('device-submit').click();

    // `SecretOnce` — shown once, never re-displayable. The spec reads
    // the credential off the screen because that is the only place it
    // exists, which is also the strongest available assertion that the
    // display works: the secret is used to authenticate a real
    // /nic/update three steps below.
    const secretPanel = page.getByTestId('device-secret-once');
    await expect(secretPanel).toBeVisible();
    const username = (
      await page.getByTestId('issued-username').innerText()
    ).trim();
    const secret = (await page.getByTestId('issued-secret').innerText()).trim();
    expect(username).toMatch(/^ddns-[0-9a-f]{12}$/);
    expect(secret.length).toBeGreaterThan(20);
    await page.getByTestId('dismiss-secret').click();
    await expect(page.getByTestId('device-secret-once')).toHaveCount(0);

    // --- 4. a name, through the UI --------------------------------
    await page.goto(NAMES_PATH);
    await expect(page.getByTestId('hostnames-empty')).toBeVisible();
    await page.getByTestId('add-hostname').click();
    const nameModal = page.getByRole('dialog');
    await expect(nameModal).toBeVisible();
    await chooseFromSelect(page, 'hostname-zone', zone);
    // Since #90 the zone is a suffix and not a retype: the bare label
    // goes in the field, the zone is rendered beside it, and `will
    // send:` is the one line that shows what composition did.
    await expect(nameModal.getByTestId('hostname-suffix')).toHaveText(
      `.${zone}`,
    );
    await nameModal.getByTestId('hostname-name').fill('home');
    await chooseFromSelect(page, 'hostname-device', deviceName);
    await expect(nameModal.getByTestId('hostname-will-send')).toHaveText(fqdn);
    await nameModal.getByTestId('hostname-submit').click();
    await expect(page.getByTestId(`hostname-${fqdn}`)).toBeVisible();

    // --- 5. the board, BEFORE the device has published anything ----
    // §3.3.1 step 7, reproduced in a browser: three steps produce a
    // hostname with no strip, and that is correct.
    await page.goto(BOARD_PATH);
    const deviceSection = page.getByTestId(`device-${deviceName}`);
    await expect(deviceSection).toBeVisible({ timeout: 15_000 });
    await expect(deviceSection).toHaveAttribute('data-liveness', 'never_seen');
    await expect(page.getByTestId(`hostname-${fqdn}`)).toContainText(
      'nothing published yet — no strip to draw',
    );
    await expect(
      page.getByTestId(`strip-${fqdn}-A`),
      'no strip before the device has published',
    ).toHaveCount(0);

    // --- 6. the device calls in, over HTTP Basic -------------------
    const update = await deviceCallsIn(page.request, {
      username,
      secret,
      hostname: fqdn,
      ip: DOC_ADDRESS_V4,
    });
    expect(update.status).toBe(200);
    expect(update.body).toBe(`good ${DOC_ADDRESS_V4}`);

    // --- 7. THE STRIP ----------------------------------------------
    await page.goto(BOARD_PATH);
    await expect(deviceSection).toBeVisible({ timeout: 15_000 });
    await expect(deviceSection).toHaveAttribute('data-liveness', 'active');

    // A device collapses only when everything under it agrees, and a
    // strip collapses under the same rule — "page height is an
    // instrument only holds if a healthy device is short". Before the
    // publish this device was auto-expanded (a hostname with no strip
    // counts as something wrong); after it, it is short and shut, and
    // the strip is not in the DOM at all until the line is clicked.
    const deviceLine = deviceSection.locator('button.ddns-device__line');
    if ((await deviceLine.getAttribute('aria-expanded')) === 'false') {
      await deviceLine.click();
    }
    await expect(deviceLine).toHaveAttribute('aria-expanded', 'true');

    const collapsedStrip = page.getByTestId(`strip-collapsed-${fqdn}-A`);
    if ((await collapsedStrip.count()) > 0) {
      await collapsedStrip.click();
    }
    const strip = page.getByTestId(`strip-${fqdn}-A`);
    await expect(strip).toBeVisible();
    await expect(strip).toHaveAttribute('data-family', 'A');

    // The three stations, by their labels and their values. The `; `
    // prefix the design specifies is generated by CSS
    // (`.ddns-label::before`), so the DOM text is the bare word.
    await expect(strip).toContainText('answered');
    await expect(strip).toContainText('published');
    await expect(strip).toContainText('called from');

    const published = strip.getByTestId('published-address');
    await expect(published).toHaveAttribute('title', DOC_ADDRESS_V4);
    await expect(published).toHaveText(DOC_ADDRESS_V4);
    // Nothing has resolved this name, and the strip says so with the
    // word §4.2 reserves for it rather than with a zero or a dash.
    await expect(strip.getByTestId('answered-address')).toHaveText('n/a');
    await expect(strip.getByTestId('called-from-address')).toHaveAttribute(
      'title',
      DOC_ADDRESS_V4,
    );

    // The two joints, which are the element's whole argument.
    //
    // **This reading differs from `ui-parity.md` §3.3.1 step 9 and the
    // difference is the design behaving as specified.** That walk's
    // device declared a `myip=` different from the address it called
    // from, so its lower joint read `not_applicable` and its divisor
    // was `0 of 0 compared`. This device calls in through
    // `X-Forwarded-For: 203.0.113.10` and declares the same address, so
    // `called_from.reason` is `evaluated` and the lower joint is a real
    // verdict — the moving denominator §3.4 describes, moving.
    await expect(
      strip.locator('.ddns-rail__joint[data-verdict="not_measured_never"]'),
      'the upper joint: nothing has resolved this name yet',
    ).toHaveCount(1);
    await expect(
      strip.locator('.ddns-rail__joint[data-verdict="agreed"]'),
      'the lower joint: the device called from the address it published',
    ).toHaveCount(1);
    await expect(strip).toHaveAttribute('data-diverged', 'false');
    // The A-family strip is the only one: nothing has been published or
    // answered over v6, so no blank AAAA rail is reserved (§3.4).
    await expect(page.getByTestId(`strip-${fqdn}-AAAA`)).toHaveCount(0);

    // --- 8. the image ---------------------------------------------
    // The acceptance criterion is the picture, not the assertion above
    // it. Everything on screen is documentation space by construction:
    // the zone is under RFC 6761 `.invalid`, both addresses are RFC
    // 5737 TEST-NET-3, and the device name is generated.
    const stripShot = await strip.screenshot({
      path: 'test-results/resolution-strip.png',
    });
    await testInfo.attach('resolution-strip', {
      body: stripShot,
      contentType: 'image/png',
    });
    const boardShot = await deviceSection.screenshot({
      path: 'test-results/resolution-strip-board.png',
    });
    await testInfo.attach('resolution-strip-board', {
      body: boardShot,
      contentType: 'image/png',
    });

    expect(pageErrors, 'the page threw while the walk ran').toEqual([]);
  });
});
