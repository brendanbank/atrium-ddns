// Copyright (c) 2026 Brendan Bank
// SPDX-License-Identifier: BSD-2-Clause

/**
 * `/atrium-ddns/devices/:id` in a real browser — #89.
 *
 * This milestone exists because UI shipped that nobody had loaded. Every
 * other assertion about this route is a vitest render or an HTTP call;
 * this file is the one that opens a page.
 *
 * ## Status when this landed: written, run, and not yet wired
 *
 * **#91 owns the harness** — `playwright.config.ts`, `helpers.ts`, the
 * `@playwright/test` dependency and the make target — and had not merged
 * when this was written. #88's two specs are already in this directory
 * under the same condition. So in *this* repository the file is inert:
 * it is not in `tsconfig.app.json`'s `include`, no config names it, and
 * `pnpm test` does not collect it. Saying so here rather than leaving a
 * reader to discover it, because a spec nothing runs is the same class
 * of artefact as a metric nothing writes.
 *
 * It has nonetheless **been run in a browser**, byte-identical, against
 * a real stack, from a scratch harness outside the repository: six
 * tests, six passed, Playwright 1.61.0 / chromium. The PR body for #89
 * carries the transcript. When #91 lands, this file needs the two
 * imports below to resolve and nothing else — a deliberate subset of
 * what #88's specs already ask of `helpers.ts`, so satisfying them
 * satisfies this.
 *
 * ## What it needs from `helpers.ts`
 *
 * Two exports, both spelled the way atrium's own
 * `frontend/tests-e2e/helpers.ts` spells them, so the two harnesses stay
 * learnable as one thing:
 *
 * - `API_URL` — the API base, `http://localhost:<port>/api`.
 * - `loginAsUser(page)` — provisions a fresh **`user`-role** tenant and
 *   leaves `page` logged in as them, returning their credentials.
 *   `user` is the right role here rather than an admin: the DDNS
 *   permissions a tenant needs (`atrium_ddns.device.manage` among them)
 *   are granted to `user` by `backend/alembic/versions/0002_ddns_core.py`,
 *   and running this as an administrator would prove the page works for
 *   the one account whose permissions are least representative.
 *
 * ## The four things this proves that nothing else can
 *
 * 1. **The route renders.** Registered, matched by atrium's router,
 *    mounted through `makeWrapperElement`, with the host bundle's own
 *    provider stack around it. Four things a component test stubs.
 * 2. **The strip is on it, at full width.** §12 argues route-over-drawer
 *    on the measured width budget — a 620px drawer would wrap the
 *    signature element. The strip's own container is measured here.
 * 3. **The 409 reaches the screen.** Not "the mutation rejected", which
 *    is what a stubbed fetch shows: the server's sentence, rendered.
 * 4. **The rename does not break the device.** The secret is captured
 *    from the one moment it is displayed, the device is renamed through
 *    the UI, and the *original* secret is then driven at `/nic/update`
 *    over HTTP Basic. `badauth` there would mean the rename rotated the
 *    credential.
 */
import { expect, test, type Page } from '@playwright/test';

import { API_URL, loginAsUser } from './helpers';

const DEVICES_PATH = '/atrium-ddns/devices';

/** §3.1's measured minimum for one resolution strip. §12 rejects a
 *  Mantine `lg` drawer (620px) because it is below this. */
const ONE_STRIP_MIN_PX = 592;

function unique(): string {
  return `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
}

/**
 * Create a device through the UI and return its name and the secret the
 * interface showed exactly once.
 *
 * Through the UI rather than through the API on purpose: the secret is
 * displayed at creation and never again, so the only way a browser can
 * hold one is to have been present when it was issued — which is also
 * the claim `SecretOnce` makes and the one worth demonstrating.
 */
async function createDevice(
  page: Page,
  name: string,
): Promise<{ name: string; username: string; secret: string }> {
  await page.goto(DEVICES_PATH);
  await page.getByTestId('add-device').click();
  await page.getByTestId('device-name').fill(name);
  await page.getByTestId('device-submit').click();
  await expect(page.getByTestId('device-secret-once')).toBeVisible();
  const username = (await page.getByTestId('issued-username').textContent()) ?? '';
  const secret = (await page.getByTestId('issued-secret').textContent()) ?? '';
  expect(secret.length).toBeGreaterThan(8);
  await page.getByTestId('dismiss-secret').click();
  return { name, username: username.trim(), secret: secret.trim() };
}

test.describe('the device detail route', () => {
  test('a tenant reaches it from the list and renames in place', async ({
    page,
  }) => {
    await loginAsUser(page);

    // The nav items render at all. #91's first spec asserts this; it is
    // repeated here because everything below is vacuous without it.
    await page.goto(DEVICES_PATH);
    await expect(page.getByTestId('add-device')).toBeVisible();

    const suffix = unique();
    const original = `router-${suffix}`;
    const created = await createDevice(page, original);

    // §12: "this adds a destination, it does not redraw the list." The
    // row's own name is the link, and it is the only way in — the route
    // carries a literal `:id`, so it cannot have a nav item.
    await page.getByTestId(`open-${original}`).click();
    await expect(page).toHaveURL(new RegExp(`${DEVICES_PATH}/\\d+$`));
    await expect(page.getByTestId('device-name')).toHaveText(original);
    await expect(page.getByTestId('detail-username')).toHaveText(
      created.username,
    );

    // Edited **in place at the top of the route**, not in a modal.
    await page.getByTestId('device-rename').click();
    await expect(page.getByTestId('device-name-input')).toBeVisible();
    await expect(page.locator('[role="dialog"]')).toHaveCount(0);

    const renamed = `renamed-${suffix}`;
    await page.getByTestId('device-name-input').fill(renamed);
    await page.getByTestId('device-name-save').click();
    await expect(page.getByTestId('device-name')).toHaveText(renamed);

    // Linkable, and it survives a reload — the first of §12's three
    // things a drawer cannot do.
    await page.reload();
    await expect(page.getByTestId('device-name')).toHaveText(renamed);

    // Back works — the second.
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`${DEVICES_PATH}$`));

    // **The rename did not rotate the credential.** The secret captured
    // at creation, driven at `/nic/update` over HTTP Basic. `badauth`
    // here would be the rename having touched the hash; `nohost` is the
    // device authenticating and owning no such name, which is the
    // answer for a device with nothing assigned to it.
    const reply = await page.request.get(`${API_URL}/../nic/update`, {
      params: { hostname: `nothing-${suffix}.example.invalid` },
      headers: {
        Authorization:
          'Basic ' +
          Buffer.from(`${created.username}:${created.secret}`).toString(
            'base64',
          ),
      },
    });
    expect(reply.status()).toBe(200);
    const code = (await reply.text()).trim().split(/\s+/)[0];
    expect(
      code,
      'the secret issued before the rename no longer authenticates',
    ).not.toBe('badauth');

    // …and the probe can still say `badauth`, so the assertion above is
    // not one that could not fail.
    const refused = await page.request.get(`${API_URL}/../nic/update`, {
      params: { hostname: `nothing-${suffix}.example.invalid` },
      headers: {
        Authorization:
          'Basic ' +
          Buffer.from(`${created.username}:${created.secret}x`).toString(
            'base64',
          ),
      },
    });
    expect((await refused.text()).trim().split(/\s+/)[0]).toBe('badauth');
  });

  test('a name the tenant already uses is refused in the server’s own words', async ({
    page,
  }) => {
    await loginAsUser(page);
    const suffix = unique();
    const taken = `occupied-${suffix}`;
    await createDevice(page, taken);
    const mover = `mover-${suffix}`;
    await createDevice(page, mover);

    await page.goto(DEVICES_PATH);
    await page.getByTestId(`open-${mover}`).click();
    await page.getByTestId('device-rename').click();
    await page.getByTestId('device-name-input').fill(taken);
    await page.getByTestId('device-name-save').click();

    // Verbatim, including the offending name. Not "that name is in
    // use", which would be the browser's words about the server's
    // answer — and not a silently generated `occupied (2)`, which is the
    // implementation this assertion exists to fail.
    await expect(page.getByTestId('device-name-refusal')).toContainText(
      `you already have a device called '${taken}'`,
    );
    await expect(page.getByTestId('device-name-input')).toHaveValue(taken);

    // Nothing was written: the list still has one of each name and no
    // suffixed variant.
    await page.goto(DEVICES_PATH);
    await expect(page.getByTestId(`open-${taken}`)).toHaveCount(1);
    await expect(page.getByTestId(`open-${mover}`)).toHaveCount(1);
  });

  test('a name another tenant uses is accepted — the constraint is per user', async ({
    browser,
  }) => {
    const shared = `shared-${unique()}`;

    const first = await browser.newContext();
    const firstPage = await first.newPage();
    await loginAsUser(firstPage);
    await createDevice(firstPage, shared);

    const second = await browser.newContext();
    const secondPage = await second.newPage();
    await loginAsUser(secondPage);
    const mine = `mine-${unique()}`;
    await createDevice(secondPage, mine);

    await secondPage.goto(DEVICES_PATH);
    await secondPage.getByTestId(`open-${mine}`).click();
    await secondPage.getByTestId('device-rename').click();
    await secondPage.getByTestId('device-name-input').fill(shared);
    await secondPage.getByTestId('device-name-save').click();

    // Succeeds. A test that only checked "a duplicate is a 409" would
    // pass identically against an installation-wide constraint, which
    // would let one tenant's naming choices refuse another's.
    await expect(secondPage.getByTestId('device-name')).toHaveText(shared);
    await expect(secondPage.getByTestId('device-name-refusal')).toHaveCount(0);

    await first.close();
    await second.close();
  });

  test('another tenant’s device is a missing device, not a forbidden one', async ({
    browser,
  }) => {
    const first = await browser.newContext();
    const firstPage = await first.newPage();
    await loginAsUser(firstPage);
    await createDevice(firstPage, `victim-${unique()}`);
    await firstPage.goto(DEVICES_PATH);
    const href = await firstPage
      .getByTestId(/^open-victim-/)
      .first()
      .getAttribute('href');
    expect(href).toBeTruthy();

    const second = await browser.newContext();
    const secondPage = await second.newPage();
    await loginAsUser(secondPage);
    await secondPage.goto(href as string);

    // 404 rather than 403, and rendered as "no such device" rather than
    // as a load failure: a 403 would confirm the row exists, and a
    // failure banner would read as a bug in the page.
    await expect(secondPage.getByTestId('detail-error')).toContainText(
      /no such device/i,
    );

    await first.close();
    await second.close();
  });

  test('the device’s names reach the route at the width one strip needs', async ({
    page,
  }) => {
    const tenant = await loginAsUser(page);
    expect(tenant.email).toContain('@');

    const suffix = unique();
    const zone = `e2e-${suffix}.example.invalid`;
    const deviceName = `named-${suffix}`;
    const { deviceId } = await attachName(page, deviceName, zone, suffix);

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`${DEVICES_PATH}/${deviceId}`);
    await expect(page.getByTestId('device-name')).toHaveText(deviceName);

    // The name block reaches the route. This zone has no provider, so
    // §10.1's state applies and the block says *nothing published yet —
    // no strip to draw* rather than drawing two empty rails. That is a
    // real rendering, and it is deliberately **not** called a strip: the
    // strip itself is the next test, which fails rather than passes if
    // nothing published.
    const block = page.getByTestId(`hostname-home.${zone}`);
    await expect(block).toBeVisible();
    await expect(block).toContainText('no strip to draw');

    // §12's measurement, taken rather than restated from the document:
    // the container the strip is laid out in is at least as wide as one
    // strip needs. A Mantine `lg` drawer would be 620px of chrome and
    // less than this of content, which is the whole argument for a
    // route.
    const box = await block.boundingBox();
    expect(box).not.toBeNull();
    expect(
      (box as { width: number }).width,
      'the name block is below §3.1’s one-strip minimum — the ' +
        'signature element would wrap inside its own detail view',
    ).toBeGreaterThanOrEqual(ONE_STRIP_MIN_PX);
  });

  test('a published name draws its resolution strip on the route', async ({
    page,
  }) => {
    await loginAsUser(page);
    const suffix = unique();
    const zone = `e2e-strip-${suffix}.example.invalid`;
    const deviceName = `stripped-${suffix}`;
    const { deviceId, secret, username } = await attachName(
      page,
      deviceName,
      zone,
      suffix,
    );
    const fqdn = `home.${zone}`;

    // A **scripted** provider, so nothing here contacts a nameserver.
    // `atrium_ddns.compat_stub` registers `stub1` only when the stack
    // was told to (`ATRIUM_DDNS_COMPAT_STUB=1`, and never in prod), and
    // it is deliberately absent from `GET /providers` — it is
    // resolvable, not offered. So the binding is attempted and the
    // refusal is read: a stack without the opt-in makes this test
    // **skip with a named reason**, which is *not measured* rather than
    // a pass. Grading those the same way is what makes a green run
    // meaningless.
    const domainId = await zoneIdFor(page, zone);
    const binding = await page.request.post(
      `${API_URL}/atrium_ddns/domains/${domainId}/backends`,
      {
        data: {
          backend_type: 'stub1',
          config: { result: 'good', ttl: 60 },
          // Not a secret and not a credential for anything: the stub
          // declares `REQUIRED_CREDENTIALS = ()` and contacts nothing.
          // The value is here because a backend row with
          // `credentials_ct IS NULL` answers `911` by the frozen table's
          // own `update-911-backend-without-stored-credentials`, so
          // omitting it would make this test measure that rule instead.
          credentials: { stub_token: 'fixture-not-a-secret' },
        },
      },
    );
    test.skip(
      binding.status() !== 201,
      'this stack does not resolve the scripted compat providers — ' +
        'start it with ATRIUM_DDNS_COMPAT_STUB=1 (and ENVIRONMENT!=prod) ' +
        'for the strip half of this spec. NOT MEASURED, not passed.',
    );

    // One `/nic/update` over HTTP Basic with the device's own
    // credential — the wire this whole product is, driven from the
    // browser's request context. It writes `ddns_hostname.last_ip_v4`,
    // which is what makes a strip exist at all (`_strips_for`).
    const published = await page.request.get(`${API_URL}/../nic/update`, {
      params: { hostname: fqdn, myip: '192.0.2.42' },
      headers: { Authorization: 'Basic ' + basic(username, secret) },
    });
    expect(published.status()).toBe(200);
    expect((await published.text()).trim()).toMatch(/^(good|nochg)\b/);

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`${DEVICES_PATH}/${deviceId}`);
    await expect(page.getByTestId('device-name')).toHaveText(deviceName);

    // The signature element, on the detail route, for the first time in
    // this repository's history in a browser. Either shape counts — a
    // strip whose joints all agree collapses to one line by design
    // (§3.4) — and both are the strip, which the *absence* of a
    // `no strip to draw` block underneath confirms.
    const expanded = page.getByTestId(`strip-${fqdn}-A`);
    const collapsed = page.getByTestId(`strip-collapsed-${fqdn}-A`);
    await expect(expanded.or(collapsed)).toBeVisible();
    await expect(page.getByTestId(`hostname-${fqdn}`)).not.toContainText(
      'no strip to draw',
    );
    // The published address is on the page, so the strip is rendering
    // this name's data and not an empty template.
    await expect(page.getByTestId(`hostname-${fqdn}`)).toContainText(
      '192.0.2.42',
    );
  });
});

function basic(username: string, secret: string): string {
  return Buffer.from(`${username}:${secret}`).toString('base64');
}

async function zoneIdFor(page: Page, zone: string): Promise<number> {
  const zones = await page.request.get(`${API_URL}/atrium_ddns/domains`);
  const row = (await zones.json()).find((z: { name: string }) => z.name === zone);
  if (!row) throw new Error(`zone ${zone} was not created`);
  return row.id;
}

/**
 * A device, a zone and a name under it, assigned to that device.
 *
 * The zone and the name go in over the API: #91's walk spec drives
 * those surfaces through the UI, and repeating them here would make
 * this spec fail for reasons that are not about this route.
 */
async function attachName(
  page: Page,
  deviceName: string,
  zone: string,
  suffix: string,
): Promise<{
  deviceId: number;
  username: string;
  secret: string;
}> {
  const created = await createDevice(page, deviceName);

  const zoneResp = await page.request.post(`${API_URL}/atrium_ddns/domains`, {
    data: { name: zone },
  });
  if (zoneResp.status() !== 201) {
    throw new Error(`zone ${zone} refused: ${await zoneResp.text()}`);
  }
  const domainId = (await zoneResp.json()).id;

  const listing = await page.request.get(`${API_URL}/atrium_ddns/devices`);
  const deviceId = (await listing.json()).find(
    (row: { name: string }) => row.name === deviceName,
  ).id;

  const nameResp = await page.request.post(`${API_URL}/atrium_ddns/hostnames`, {
    data: { name: `home.${zone}`, domain_id: domainId, device_id: deviceId },
  });
  if (nameResp.status() !== 201) {
    throw new Error(`name refused (${suffix}): ${await nameResp.text()}`);
  }

  return { deviceId, username: created.username, secret: created.secret };
}
