// Copyright (c) 2026 Brendan Bank
// SPDX-License-Identifier: BSD-2-Clause

/**
 * #97 — the destinations were built and nothing said they were there.
 *
 * `docs/ops/ui-design.md` **Part III (§16–§18)**. The operator opened
 * the zones page and the device board and reported *"I still cannot edit
 * the zone"*, twice. Both surfaces already had working destinations
 * behind them. The defect was not a missing feature; it was a missing
 * affordance, with three separate causes (§16's table) and one
 * appearance.
 *
 * ## Why this is a Playwright spec and not a vitest one
 *
 * Because the whole claim is about *rendered pixels*, and jsdom has no
 * cascade. `ddns.css` reaches the page as a runtime `<style>` tag; in
 * jsdom `getComputedStyle(link).textDecorationLine` reads `""` both
 * before and after the fix. A probe that prints the same string whether
 * or not the thing it measures exists is not a probe — this repository
 * has a section of its own contract about that family. So the reading is
 * taken in chromium.
 *
 * ## Every assertion here has something that can make it fail
 *
 * #89 shipped a strip assertion that could not fail: it asserted "the
 * strip renders" against a provider-less zone and passed on a
 * `no strip to draw` block. So:
 *
 * - the underline reading is paired with a **control** — a
 *   non-interactive `.ddns-data` on the same page, which must read
 *   `none`. If the rule leaked to every `.ddns-data`, or if the reading
 *   instrument were broken, that control fails.
 * - the colour reading is paired with the same control in the other
 *   direction: the link's colour must **equal** the inert value's. If
 *   somebody "fixes" the affordance by dropping `.ddns-data` from the
 *   anchor, Mantine's link colour returns and the two stop matching.
 * - the strips inside the device card are published through the scripted
 *   provider first, and the `no strip to draw` block is asserted
 *   **absent**, so a card that rendered an empty state cannot pass as a
 *   card that rendered the signature element.
 * - the width is measured off the rendered strip, not off the modal's
 *   `size` prop, which is the number this repo would be asserting about
 *   itself.
 *
 * ## Everything on every screenshot is documentation space
 *
 * Zones are under RFC 6761 `.invalid`, addresses are RFC 5737 / RFC 3849,
 * accounts are `@example.com`, and the host in the address bar is
 * `localhost`. Nothing real can appear on one of these images.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import {
  API_URL,
  BOARD_PATH,
  DEVICES_PATH,
  DOMAINS_PATH,
  bindScriptedBackend,
  deviceCallsIn,
  loginAsUser,
  uniqueDeviceName,
  uniqueZoneName,
} from './helpers';
import { ONE_STRIP_MIN_PX, QUALIFIED_STRIP_MIN_PX } from '../src/cards';

/** RFC 5737 TEST-NET-1. The address the fixture publishes, and therefore
 *  the one that appears on the strip in the screenshots. `deviceCallsIn`
 *  pins the *called from* station to TEST-NET-3 through
 *  `X-Forwarded-For`, so every address on these images is documentation
 *  space and none of them is the machine the run happened on. */
const DOC_PUBLISHED_V4 = '192.0.2.42';

/** How chromium reports "no underline". Named because `none` and the
 *  empty string are two different readings and only one of them means
 *  the property was resolved. */
const NO_DECORATION = 'none';

async function decorationOf(locator: Locator): Promise<string> {
  return locator.evaluate(
    (el) => getComputedStyle(el).textDecorationLine.trim(),
  );
}

async function colourOf(locator: Locator): Promise<string> {
  return locator.evaluate((el) => getComputedStyle(el).color.trim());
}

/** The testids rendered inside a card, as a sorted list.
 *
 * Used to compare the modal entrance against the route entrance. §17
 * asks for *one card component, two entrances*; `sharedCard.test.tsx`
 * asserts that by substituting the module, which is the strong form.
 * This is the browser-side corroboration of the same claim, and it is a
 * different instrument: it compares what actually reached the DOM
 * through atrium's router and through a Mantine portal.
 */
async function cardShape(root: Locator): Promise<string[]> {
  return root.evaluate((el) =>
    Array.from(el.querySelectorAll('[data-testid]'))
      .map((node) => node.getAttribute('data-testid') as string)
      .sort(),
  );
}

/** A zone with a provider bound to it, created over the API. */
async function makeZone(page: Page, name: string): Promise<number> {
  const resp = await page.request.post(`${API_URL}/atrium_ddns/domains`, {
    data: { name },
  });
  if (resp.status() !== 201) {
    throw new Error(`zone ${name} refused: ${await resp.text()}`);
  }
  return (await resp.json()).id as number;
}

test.describe('#97 — an interactive .ddns-data says so, at rest', () => {
  test.describe.configure({ timeout: 120_000 });

  test('the zone name is a link, underlined at rest, in the ink it always had', async ({
    page,
  }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);
    const zone = uniqueZoneName();
    const zoneId = await makeZone(page, zone);
    await bindScriptedBackend(page.request, zoneId);

    await page.goto(DOMAINS_PATH);
    const name = page.getByTestId(`open-domain-${zone}`);
    await expect(name).toBeVisible({ timeout: 15_000 });

    // §17 keeps §12's two surviving arguments, and both are properties
    // of the URL. The row is still an anchor and still carries the
    // route, so copy-link and open-in-new-tab still work.
    await expect(name).toHaveAttribute('href', new RegExp(`/zones/${zoneId}$`));
    expect(await name.evaluate((el) => el.tagName)).toBe('A');
    expect(
      await name.evaluate((el) => el.className),
      'the fix is not to drop .ddns-data from the anchor — that breaks §2.3 ' +
        'on the most important string on the page',
    ).toContain('ddns-data');

    // **The reading this whole issue is about.** No hover, no focus:
    // the pointer has not been moved and nothing has been tabbed to.
    const decoration = await decorationOf(name);
    expect(
      decoration,
      'the zone name carries no affordance at rest — this is the pixel the ' +
        'operator was looking at when they said "I still cannot edit the zone"',
    ).toContain('underline');

    // The control. Open the card and read an inert `.ddns-data` — the
    // provider's own name inside it — with the same instrument.
    await name.click();
    const card = page.getByTestId('zone-card');
    await expect(card).toBeVisible({ timeout: 15_000 });
    const inert = card.locator('span.ddns-data').first();
    await expect(inert).toBeVisible();

    expect(
      await decorationOf(inert),
      'an inert .ddns-data is underlined too, so the reading above says ' +
        'nothing about interactivity',
    ).toBe(NO_DECORATION);

    // …and the colour did not move. This is the assertion that fails if
    // a later change "fixes" the affordance by taking `.ddns-data` off
    // the anchor: Mantine's link colour would come back and the two
    // would stop matching.
    expect(
      await colourOf(name),
      'the link and the value no longer render in the same ink — §2.3 has ' +
        'been retracted on the anchors, which is the fix §16.1 rules out',
    ).toBe(await colourOf(inert));

    // The pixels the report was about. The list is behind the card, so
    // one image carries both the underlined name in the row and the
    // card it opens.
    await testInfo.attach('zone-name-affordance.png', {
      body: await page.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });

  test('a plain click opens a modal; the route still resolves to the same card', async ({
    page,
  }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);
    const zone = uniqueZoneName();
    const zoneId = await makeZone(page, zone);
    await bindScriptedBackend(page.request, zoneId);

    await page.goto(DOMAINS_PATH);
    const listUrl = page.url();
    await page.getByTestId(`open-domain-${zone}`).click();

    // §17's decision, rendered: a modal pops up.
    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible({ timeout: 15_000 });
    // …and it is a modal and not a navigation. Asserting the URL did
    // *not* move is what distinguishes the two shapes; without it this
    // test passes against a route that happens to render a dialog.
    expect(page.url()).toBe(listUrl);

    // The edit controls are in it. "A modal pops up" with nothing in it
    // that edits the zone would satisfy the sentence and not the report.
    const card = modal.getByTestId('zone-card');
    await expect(card).toBeVisible();
    await expect(card.getByTestId(`zone-${zone}`)).toBeVisible();
    await expect(card.getByTestId('zone-rename')).toBeVisible();
    await expect(card.getByTestId('zone-delete')).toBeVisible();
    await expect(card.getByTestId('zone-add-backend')).toBeVisible();
    await expect(card.getByTestId('backend-stub1')).toBeVisible();

    // §17's width condition. Measured off the modal's *content box* —
    // what a strip would actually be laid out in — rather than off the
    // `size` prop, which would be this repository asserting its own
    // arithmetic back to itself.
    const bodyWidth = await modal
      .locator('.mantine-Modal-body')
      .evaluate((el) => {
        const style = getComputedStyle(el);
        return (
          el.getBoundingClientRect().width -
          parseFloat(style.paddingLeft) -
          parseFloat(style.paddingRight)
        );
      });
    expect(
      bodyWidth,
      'the card modal is narrower than one resolution strip — this is ' +
        'exactly the failure §12 rejected a 620px drawer for, arriving ' +
        'inside the shape the operator asked for',
    ).toBeGreaterThanOrEqual(ONE_STRIP_MIN_PX);
    expect(
      bodyWidth,
      'the modal holds a plain strip but would wrap one carrying a ' +
        'qualified station label — ddns.css §3.3 measured that case at 728px',
    ).toBeGreaterThanOrEqual(QUALIFIED_STRIP_MIN_PX);

    const modalShape = await cardShape(card);
    await testInfo.attach('zone-card-modal.png', {
      body: await page.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    // Escape closes it, and the list is still underneath — the modal
    // did not consume the page it was opened from.
    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden();
    await expect(page.getByTestId(`domain-${zone}`)).toBeVisible();

    // --- the same card, through the route ---------------------------
    await page.goto(`/atrium-ddns/zones/${zoneId}`);
    const routeCard = page.getByTestId('zone-card');
    await expect(routeCard).toBeVisible({ timeout: 15_000 });
    // The route is a route: no dialog. If this were also a modal the
    // comparison below would be comparing a thing with itself.
    await expect(page.getByRole('dialog')).toHaveCount(0);

    const routeShape = await cardShape(routeCard);
    // Vacuity: a card that rendered nothing would compare equal to
    // another card that rendered nothing.
    expect(routeShape.length).toBeGreaterThan(5);
    expect(
      routeShape,
      'the modal and the route rendered different cards — §17 asks for one ' +
        'component behind both, and two that have started to drift is the ' +
        'exact failure it names',
    ).toEqual(modalShape);

    await testInfo.attach('zone-card-route.png', {
      body: await page.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });

  test('the board row does two jobs on two targets, and a keyboard reaches both', async ({
    page,
  }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);
    const zone = uniqueZoneName();
    const deviceName = uniqueDeviceName();
    const fqdn = `home.${zone}`;

    // A device created through the UI, because the secret is shown
    // exactly once and the only way a browser holds one is to have been
    // there when it was issued.
    await page.goto(DEVICES_PATH);
    await page.getByTestId('add-device').click();
    const create = page.getByRole('dialog');
    await expect(create).toBeVisible({ timeout: 15_000 });
    await create.getByTestId('device-name').fill(deviceName);
    await create.getByTestId('device-submit').click();
    await expect(page.getByTestId('device-secret-once')).toBeVisible();
    const username = (
      await page.getByTestId('issued-username').innerText()
    ).trim();
    const secret = (await page.getByTestId('issued-secret').innerText()).trim();
    await page.getByTestId('dismiss-secret').click();

    // A zone with a *scripted* provider, so nothing here contacts a
    // nameserver, and a name under it assigned to this device.
    const zoneId = await makeZone(page, zone);
    await bindScriptedBackend(page.request, zoneId);
    const devices = await page.request.get(`${API_URL}/atrium_ddns/devices`);
    const deviceId = (await devices.json()).find(
      (row: { name: string }) => row.name === deviceName,
    ).id as number;
    const nameResp = await page.request.post(
      `${API_URL}/atrium_ddns/hostnames`,
      { data: { name: fqdn, domain_id: zoneId, device_id: deviceId } },
    );
    expect(nameResp.status()).toBe(201);

    // One `/nic/update` over HTTP Basic, exactly as a router does. This
    // is what writes `last_ip_*` and therefore what makes a strip exist
    // at all — without it the card renders `no strip to draw` and the
    // screenshot below would be of an empty state.
    const published = await deviceCallsIn(page.request, {
      username,
      secret,
      hostname: fqdn,
      ip: DOC_PUBLISHED_V4,
    });
    expect(published.status).toBe(200);
    expect(published.body).toMatch(/^(good|nochg)\b/);

    await page.goto(BOARD_PATH);
    const open = page.getByTestId(`board-open-${deviceName}`);
    const expand = page.getByTestId(`device-${deviceName}-expand`);
    await expect(open).toBeVisible({ timeout: 15_000 });
    await expect(expand).toBeVisible();

    // §16's first row: on the board the name was *not a link at all* —
    // a bare span inside the expand toggle. It is a control now, and it
    // carries the same at-rest affordance the anchors do.
    expect(await open.evaluate((el) => el.tagName)).toBe('BUTTON');
    expect(await decorationOf(open)).toContain('underline');

    // The control for that reading, on this page: a `.ddns-data` that
    // is not a control — the hostname inside the expanded block.
    //
    // The disclosure is toggled *conditionally*, never blindly. This
    // device is auto-expanded already: nothing has health-checked the
    // name yet, so both joints are `not_measured_never`, the strip is
    // not collapsible, and `anythingWrong` opens the block without
    // being asked (§3.4 — nothing that hides a divergence may be the
    // default). An unconditional click here *collapses* it, which is
    // what the first run of this spec did.
    if ((await expand.getAttribute('aria-expanded')) === 'false') {
      await expand.click();
    }
    const inert = page
      .getByTestId(`device-${deviceName}`)
      .locator('.ddns-device__hostnames span.ddns-data')
      .first();
    await expect(inert).toBeVisible();
    expect(await decorationOf(inert)).toBe(NO_DECORATION);

    // §18.2 — two jobs, two targets. The disclosure owns the state and
    // the name does not, which is what a screen reader reads.
    await expect(expand).toHaveAttribute('aria-expanded', 'true');
    expect(await open.getAttribute('aria-expanded')).toBeNull();

    // **The keyboard path.** Tab from the name and the very next stop is
    // the disclosure: two controls, adjacent, both reachable, neither
    // hidden behind the other. Before this they were one target.
    await open.focus();
    expect(
      await page.evaluate(() =>
        document.activeElement?.getAttribute('data-testid'),
      ),
    ).toBe(`board-open-${deviceName}`);
    await page.keyboard.press('Tab');
    expect(
      await page.evaluate(() =>
        document.activeElement?.getAttribute('data-testid'),
      ),
      'Tab from the device name does not land on its disclosure — the two ' +
        'controls are not adjacent in the tab order',
    ).toBe(`device-${deviceName}-expand`);

    // …and Enter on the name opens the card, from the keyboard alone.
    await open.focus();
    await page.keyboard.press('Enter');
    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible({ timeout: 15_000 });
    await expect(modal.getByTestId('device-detail')).toBeVisible();
    await expect(modal.getByTestId('device-name')).toHaveText(deviceName);

    // **The signature element, inside the modal.** Either shape counts —
    // a strip whose joints all agree collapses to one line by design
    // (§3.4) — and the `no strip to draw` block is asserted absent, so
    // an empty state cannot pass for a rendered strip. #89's assertion
    // could not fail for exactly the want of this line.
    const strip = modal.getByTestId(`strip-${fqdn}-A`);
    const collapsed = modal.getByTestId(`strip-collapsed-${fqdn}-A`);
    await expect(strip.or(collapsed)).toBeVisible({ timeout: 15_000 });
    const block = modal.getByTestId(`hostname-${fqdn}`);
    await expect(block).not.toContainText('no strip to draw');
    // The published address is on it, so this is this name's data and
    // not an empty template.
    await expect(block).toContainText(DOC_PUBLISHED_V4);

    // And it is not wrapped. §17's condition, measured on the element
    // that would have been the casualty.
    const stripWidth = await block.evaluate(
      (el) => el.getBoundingClientRect().width,
    );
    expect(
      stripWidth,
      'the strip inside the card modal is below §3.1’s one-strip minimum — ' +
        'the signature element is wrapping inside its own detail view',
    ).toBeGreaterThanOrEqual(ONE_STRIP_MIN_PX);

    await testInfo.attach('device-card-modal-from-board.png', {
      body: await page.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });

  test('the device list opens the same card, and its route still resolves', async ({
    page,
  }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);
    const deviceName = uniqueDeviceName();

    await page.goto(DEVICES_PATH);
    await page.getByTestId('add-device').click();
    const create = page.getByRole('dialog');
    await expect(create).toBeVisible({ timeout: 15_000 });
    await create.getByTestId('device-name').fill(deviceName);
    await create.getByTestId('device-submit').click();
    await expect(page.getByTestId('device-secret-once')).toBeVisible();
    await page.getByTestId('dismiss-secret').click();

    const name = page.getByTestId(`open-${deviceName}`);
    await expect(name).toBeVisible();
    expect(await decorationOf(name)).toContain('underline');
    const href = await name.getAttribute('href');
    expect(href).toBeTruthy();

    const listUrl = page.url();
    await name.click();
    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible({ timeout: 15_000 });
    expect(page.url()).toBe(listUrl);
    const modalCard = modal.getByTestId('device-detail');
    await expect(modalCard).toBeVisible();
    // The edit controls, which is what "I cannot edit" was about.
    await expect(modalCard.getByTestId('device-rename')).toBeVisible();
    await expect(modalCard.getByTestId('detail-limit-save')).toBeVisible();
    await expect(modalCard.getByTestId('detail-rotate')).toBeVisible();
    const modalShape = await cardShape(modalCard);

    await testInfo.attach('device-card-modal-from-list.png', {
      body: await page.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden();

    // The route, still there and still the same card.
    await page.goto(href as string);
    const routeCard = page.getByTestId('device-detail');
    await expect(routeCard).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    const routeShape = await cardShape(routeCard);
    expect(routeShape.length).toBeGreaterThan(5);
    expect(
      routeShape,
      'the device modal and the device route rendered different cards',
    ).toEqual(modalShape);

    await testInfo.attach('device-card-route.png', {
      body: await page.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });
});
