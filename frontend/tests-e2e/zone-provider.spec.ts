import { expect, test } from '@playwright/test';

import {
  API_URL,
  DEVICES_PATH,
  DOC_ADDRESS_V4,
  DOMAINS_PATH,
  NAMES_PATH,
  chooseFromSelect,
  deviceCallsIn,
  loginAsUser,
  uniqueDeviceName,
  uniqueZoneName,
} from './helpers';

/**
 * #88 — a zone that publishes nowhere answers `911` and looked fine.
 *
 * ## What this spec is for
 *
 * `docs/ops/ui-design.md` Part II §8.1 states the defect as a wire fact
 * rather than as a preference: the create-zone modal offered one field
 * and a Create button, so **one click produced a zone whose every
 * update answers `911`** — frozen at
 * `tests/compat/protocol_cases.yaml:211`, `update/no-backends-911` —
 * and the list then drew that zone identically to a working one.
 *
 * The unit tests assert what leaves the browser. This asserts what an
 * operator sees, in a browser, which is the thing nobody had checked:
 * *"This entire milestone exists because UI was shipped that nobody had
 * loaded."*
 *
 * ## The `911` is demonstrated, not quoted
 *
 * The second test below takes the "add a provider later" link, creates a
 * name inside the resulting zone through the UI, and then has a device
 * call `/nic/update` exactly as a router does. The assertion is on the
 * wire body. Quoting the YAML would prove that the table says `911`;
 * this proves the deployed service answers it, on a zone this UI just
 * created, today.
 *
 * The control matters as much as the claim: the *same* device, on a name
 * in a zone that **does** have a provider, gets a different answer. A
 * test that only saw `911` could not distinguish "no provider" from
 * "this stack is broken".
 *
 * ## Layout
 *
 * `frontend/tests-e2e/`, atrium's own convention
 * (`/Users/brendan/src/atrium/frontend/tests-e2e/`), and the helper
 * vocabulary is #91's — `loginAsUser`, `deviceCallsIn`,
 * `chooseFromSelect`, `uniqueZoneName`. This file therefore **depends on
 * #91 having landed**; it is written to that harness and does not carry
 * a second copy of it.
 */

/** The credential the create form stores. Never used to reach AWS: the
 *  zone is under RFC 6761 `.invalid`, nothing resolves it, and every
 *  address on screen is RFC 5737 documentation space. Named so that a
 *  reader of a screenshot cannot mistake it for a real key. */
const DEMO_CREDENTIAL = {
  aws_access_key_id: 'AKIA-E2E-NOT-A-SECRET',
  aws_secret_access_key: 'e2e-not-a-secret',
};

test.describe('#88 — zones and providers are one object', () => {
  test.describe.configure({ timeout: 120_000 });

  test('a zone and its first provider arrive in one submission', async ({
    page,
  }) => {
    // Surface a crashing render rather than waiting 30s for a locator on
    // a blank page — the Mantine failure mode the playwright-debug skill
    // catalogues.
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    // Every request the browser makes, so "one submission" can be
    // asserted as *one request* rather than as one button press. Two
    // requests would be able to half-succeed, and the half that succeeds
    // is the zone.
    const creates: string[] = [];
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().includes('atrium_ddns')) {
        creates.push(request.url());
      }
    });

    await loginAsUser(page);
    const zone = uniqueZoneName();

    await page.goto(DOMAINS_PATH);
    await expect(page.getByTestId('domains-empty')).toBeVisible({
      timeout: 15_000,
    });
    await page.getByTestId('add-domain').click();

    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible();
    // The zone field and the provider fields are in one modal, in one
    // reading order — §10.1's wireframe, rendered.
    await modal.getByTestId('zone-name').fill(zone);
    await expect(modal.getByTestId('backend-service')).toBeVisible();
    // Pick the provider explicitly. `BackendForm` defaults to
    // `providers[0]`, and `known_services()` is **sorted**, so the
    // default is `hetzner` — whose one credential key is
    // `hetzner_api_token`, not the route53 pair below. Adjusted by #91
    // when this spec was first run: it is a fact about the catalogue's
    // order, which nothing in the file could have known unrun.
    await chooseFromSelect(page, 'backend-service', 'route53');
    // The credential fields come from `GET /providers`, i.e. from
    // `BaseProvider.REQUIRED_CREDENTIALS`. This is `BackendForm`, not a
    // create-only copy of it.
    for (const [key, value] of Object.entries(DEMO_CREDENTIAL)) {
      await modal.getByTestId(`credential-${key}`).fill(value);
    }
    await modal.getByTestId('backend-submit').click();

    const row = page.getByTestId(`domain-${zone}`);
    await expect(row).toBeVisible({ timeout: 15_000 });
    // Not diverged. §1.2 Rule 1 — agreement has no colour, so a working
    // zone carries no mark at all.
    await expect(row).toHaveAttribute('data-diverged', 'false');
    await expect(row).not.toContainText('publishes nowhere');
    await expect(row).not.toContainText('911');
    await expect(page.getByTestId(`providers-${zone}`)).toContainText(
      '1 provider',
    );

    // One request, and it is the zone's. A second POST to `/backends`
    // would mean the browser had assembled the state in two steps.
    expect(
      creates.filter((url) => url.includes('/atrium_ddns/domains')),
    ).toHaveLength(1);
    expect(creates.filter((url) => url.includes('/backends'))).toHaveLength(0);

    // --- the detail route, §10.2 -----------------------------------
    //
    // **Rewritten by #97.** A plain click on the row now opens the card
    // in a *modal* — Part III §17, the operator's reversal of §12 —
    // rather than navigating. The route is not gone and is not
    // demoted: §12's two surviving arguments are linkability and Back,
    // both properties of the URL, so the row is still an `<a href>` and
    // the address still resolves. That is what is exercised here.
    // #97's own spec (`card-affordance.spec.ts`) covers the modal.
    const detailUrl = new URL(
      (await page
        .getByTestId(`open-domain-${zone}`)
        .getAttribute('href')) as string,
      page.url(),
    ).toString();
    expect(detailUrl).toMatch(/\/atrium-ddns\/zones\/\d+$/);
    await page.goto(detailUrl);
    await expect(page).toHaveURL(/\/atrium-ddns\/zones\/\d+$/);

    await expect(page.getByTestId(`zone-${zone}`)).toBeVisible({
      timeout: 15_000,
    });
    // The provider is listed *inside* the zone, which is the whole of
    // §10.2: the previous build nested it in an accordion on a shared
    // list page, three clicks from the thing it describes.
    await expect(page.getByTestId('backend-route53')).toBeVisible();
    await expect(page.getByTestId(`zone-${zone}`)).toContainText(
      '1 provider',
    );
    // The credential is a word, never a masked value, and never a
    // prefix — "a prefix of an API token is still a disclosure".
    await expect(page.locator('body')).toContainText('credential stored');
    await expect(page.locator('body')).not.toContainText('AKIA-E2E');

    // The width the route exists to preserve (§12). Asserted rather
    // than assumed: the argument for a route over a Mantine `lg` drawer
    // is that 620px is below the 592px one-strip minimum once the
    // drawer's own padding is taken, and a detail surface narrower than
    // its own signature element is the failure the drawer was rejected
    // for.
    const contentWidth = await page
      .getByTestId(`zone-${zone}`)
      .evaluate((el) => el.getBoundingClientRect().width);
    expect(contentWidth).toBeGreaterThan(592);

    // Back works — the second of §12's two survivors. A drawer teaches
    // the browser nothing, and neither would a modal; this is the
    // reason §17 kept the route rather than replacing it.
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`${DOMAINS_PATH}$`));

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });

  test('"add a provider later" is a link, and the zone it makes answers 911', async ({
    page,
  }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);
    const zoneBroken = uniqueZoneName();
    const zoneWorking = uniqueZoneName();
    const deviceName = uniqueDeviceName();
    const fqdnBroken = `home.${zoneBroken}`;
    const fqdnWorking = `home.${zoneWorking}`;

    // --- 1. the link, and what it is not ---------------------------
    await page.goto(DOMAINS_PATH);
    await expect(page.getByTestId('domains-empty')).toBeVisible({
      timeout: 15_000,
    });
    await page.getByTestId('add-domain').click();
    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible();

    // "a link, not a checkbox, and not the default" — all three,
    // checked before it is taken.
    const later = modal.getByTestId('zone-later-link');
    await expect(later).toBeVisible();
    await expect(modal.locator('input[type="checkbox"]')).toHaveCount(0);
    await expect(modal.getByTestId('zone-later-warning')).toHaveCount(0);
    await expect(modal.getByTestId('zone-later-submit')).toHaveCount(0);

    await modal.getByTestId('zone-name').fill(zoneBroken);
    await later.click();
    // The consequence, next to the act, before it can be taken.
    await expect(modal.getByTestId('zone-later-warning')).toContainText(
      'publishes nowhere',
    );
    await expect(modal.getByTestId('zone-later-warning')).toContainText('911');
    await modal.getByTestId('zone-later-submit').click();

    // --- 2. the zone renders diverged ------------------------------
    const brokenRow = page.getByTestId(`domain-${zoneBroken}`);
    await expect(brokenRow).toBeVisible({ timeout: 15_000 });
    await expect(brokenRow).toHaveAttribute('data-diverged', 'true');
    await expect(brokenRow).toContainText('publishes nowhere');
    await expect(brokenRow).toContainText(
      'every update for a name in this zone answers 911',
    );
    // The operator's terms, never the protocol's. §10.1: "The operator
    // does not own a backend; they own a zone that does or does not
    // work."
    await expect(brokenRow).not.toContainText(/backend/i);

    // The treatment is `--ddns-diverge` — **no new palette value**.
    // Resolved off the live stylesheet rather than asserted as a class
    // name, so a rule that stopped applying fails here.
    //
    // Both scheme values are accepted because atrium owns the colour
    // scheme and a spec that pinned the light one would be asserting
    // which theme the browser happened to boot in. The pair is the
    // design's own (`ddns.css`, §1.3), so a *seventh* value still fails.
    const accent = await brokenRow.evaluate((el) =>
      getComputedStyle(el).getPropertyValue('--ddns-diverge').trim(),
    );
    expect(['#b4500a', '#f59042']).toContain(accent);

    // --- 3. a control zone, with a provider ------------------------
    await page.getByTestId('add-domain').click();
    const second = page.getByRole('dialog');
    await expect(second).toBeVisible();
    await second.getByTestId('zone-name').fill(zoneWorking);
    // Same reason as above: the default provider is the catalogue's
    // first, sorted, which is not route53.
    await chooseFromSelect(page, 'backend-service', 'route53');
    for (const [key, value] of Object.entries(DEMO_CREDENTIAL)) {
      await second.getByTestId(`credential-${key}`).fill(value);
    }
    await second.getByTestId('backend-submit').click();
    const workingRow = page.getByTestId(`domain-${zoneWorking}`);
    await expect(workingRow).toBeVisible({ timeout: 15_000 });
    await expect(workingRow).toHaveAttribute('data-diverged', 'false');

    // The two rows side by side, and the border is the channel that
    // separates them. Compared to each other rather than to a literal,
    // so this holds in either colour scheme — and it is the assertion
    // §8.1 is about: "the exceptional row … drawn in the same ink as
    // the other 500".
    const brokenBorder = await brokenRow.evaluate(
      (el) => getComputedStyle(el).borderTopColor,
    );
    const workingBorder = await workingRow.evaluate(
      (el) => getComputedStyle(el).borderTopColor,
    );
    expect(brokenBorder).not.toBe(workingBorder);

    // The screenshot of the two rows together. The whole argument of
    // §8.1 is that these two were drawn in the same ink; this image is
    // the evidence that they no longer are. Everything on it is
    // documentation space: both zones are under RFC 6761 `.invalid`.
    await testInfo.attach('zone-list-diverged.png', {
      body: await page.screenshot(),
      contentType: 'image/png',
    });

    // --- 4. a device, through the UI -------------------------------
    await page.goto(DEVICES_PATH);
    await page.getByTestId('add-device').click();
    const deviceModal = page.getByRole('dialog');
    await expect(deviceModal).toBeVisible();
    await deviceModal.getByTestId('device-name').fill(deviceName);
    await deviceModal.getByTestId('device-submit').click();
    await expect(page.getByTestId('device-secret-once')).toBeVisible();
    const username = (
      await page.getByTestId('issued-username').innerText()
    ).trim();
    const secret = (await page.getByTestId('issued-secret').innerText()).trim();
    await page.getByTestId('dismiss-secret').click();

    // --- 5. a name in each zone, through the UI --------------------
    for (const [zone, fqdn] of [
      [zoneBroken, fqdnBroken],
      [zoneWorking, fqdnWorking],
    ] as const) {
      await page.goto(NAMES_PATH);
      await page.getByTestId('add-hostname').click();
      const nameModal = page.getByRole('dialog');
      await expect(nameModal).toBeVisible();
      await chooseFromSelect(page, 'hostname-zone', zone);
      await nameModal.getByTestId('hostname-name').fill(fqdn);
      await chooseFromSelect(page, 'hostname-device', deviceName);
      await nameModal.getByTestId('hostname-submit').click();
      await expect(page.getByTestId(`hostname-${fqdn}`)).toBeVisible({
        timeout: 15_000,
      });
    }

    // --- 6. THE WIRE CLAIM -----------------------------------------
    // The issue's whole argument rests on this being true today, so it
    // is demonstrated rather than quoted from
    // `protocol_cases.yaml:211`.
    const broken = await deviceCallsIn(page.request, {
      username,
      secret,
      hostname: fqdnBroken,
      ip: DOC_ADDRESS_V4,
    });
    expect(broken.status).toBe(200);
    expect(
      broken.body,
      'a name in a zone with no provider must answer 911 — this is the ' +
        'state the create modal used to manufacture in one click',
    ).toBe(`911 ${DOC_ADDRESS_V4}`);

    // The control. The *same* device, the same call, on a name in a
    // zone that has a provider bound to it: a different answer. Without
    // this, `911` above is equally consistent with "the stack is
    // broken", and the assertion could not fail for the right reason.
    const working = await deviceCallsIn(page.request, {
      username,
      secret,
      hostname: fqdnWorking,
      ip: DOC_ADDRESS_V4,
    });
    expect(working.status).toBe(200);
    expect(
      working.body,
      'the control must not also be 911, or the assertion above says ' +
        'nothing about zero providers',
    ).not.toBe(`911 ${DOC_ADDRESS_V4}`);

    // --- 7. the detail route says it too ---------------------------
    // Reached through the row's own `href` rather than through a click:
    // #97 made the plain click open the card in a modal (Part III §17),
    // and the point of this step is the *route*, which §17 kept.
    await page.goto(DOMAINS_PATH);
    const brokenHref = (await page
      .getByTestId(`open-domain-${zoneBroken}`)
      .getAttribute('href')) as string;
    expect(brokenHref).toMatch(/\/atrium-ddns\/zones\/\d+$/);
    await page.goto(brokenHref);
    await expect(page).toHaveURL(/\/atrium-ddns\/zones\/\d+$/);
    const detail = page.getByTestId(`zone-${zoneBroken}`);
    await expect(detail).toBeVisible({ timeout: 15_000 });
    await expect(detail).toHaveAttribute('data-diverged', 'true');
    await expect(detail).toContainText('publishes nowhere');
    // …and the name is listed inside the zone, which is how an operator
    // learns which names are affected by it.
    await expect(page.getByTestId(`zone-name-${fqdnBroken}`)).toBeVisible();

    await testInfo.attach('zone-detail-diverged.png', {
      body: await detail.screenshot(),
      contentType: 'image/png',
    });

    // --- 8. and adding a provider clears it ------------------------
    // The mark is a measurement, not a label: it goes away when the
    // thing it measures does. Bound over HTTP because the point here is
    // the *rendering*, and the modal's own click path is covered by the
    // first test in this file.
    const domains = await page.request.get(`${API_URL}/atrium_ddns/domains`);
    const owned = (await domains.json()) as Array<{ id: number; name: string }>;
    const brokenId = owned.find((entry) => entry.name === zoneBroken)?.id;
    expect(brokenId).toBeDefined();
    const bound = await page.request.post(
      `${API_URL}/atrium_ddns/domains/${brokenId}/backends`,
      {
        data: {
          backend_type: 'route53',
          config: { ttl: 300 },
          credentials: DEMO_CREDENTIAL,
        },
      },
    );
    expect(bound.status()).toBe(201);

    await page.reload();
    await expect(detail).toHaveAttribute('data-diverged', 'false', {
      timeout: 15_000,
    });
    await expect(detail).not.toContainText('publishes nowhere');

    // …and the wire agrees with the screen. Two instruments on one
    // fact, and this is the direction that would catch a mark rendered
    // off something other than the binding.
    const after = await deviceCallsIn(page.request, {
      username,
      secret,
      hostname: fqdnBroken,
      ip: DOC_ADDRESS_V4,
    });
    expect(after.body).not.toBe(`911 ${DOC_ADDRESS_V4}`);

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });
});
