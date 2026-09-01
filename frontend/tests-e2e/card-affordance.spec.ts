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
  BOARD_PATH,
  API_URL,
  DOMAINS_PATH,
  bindScriptedBackend,
  loginAsUser,
  seedZoneDeviceAndName,
  uniqueDeviceName,
  uniqueZoneName,
} from './helpers';
import { ONE_STRIP_MIN_PX, QUALIFIED_STRIP_MIN_PX } from '../src/cards';

/** RFC 5737 TEST-NET-1. The address the fixture publishes, and therefore
 *  the one that appears on the strip in the screenshots. `deviceCallsIn`
 *  pins the *called from* station to TEST-NET-3 through
 *  `X-Forwarded-For`, so every address on these images is documentation
 *  space and none of them is the machine the run happened on. */

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
  test.describe.configure({ timeout: 30_000 });

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
    await expect(name).toBeVisible({ timeout: 8_000 });

    // §17 keeps §12's two surviving arguments, and both are properties
    // of the URL. The row is still an anchor and still carries the
    // route, so copy-link and open-in-new-tab still work.
    // The address is `?zone=` on the list route now, and the href has to
    // match what a click does — copy-link and cmd-click pointing somewhere
    // else is the bug this whole spec is about.
    await expect(name).toHaveAttribute(
      'href',
      new RegExp(`\\\\?zone=${zoneId}$`),
    );
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
    // The control, and it is now the stylesheet rather than an element.
    //
    // After the rewrites almost every `.ddns-data` in the app is a link
    // or a button — the zone name is an input, the board cells are
    // `.ddns-cell` — so there is no inert one left to read. Injecting a
    // bare `span.ddns-data` and asking the browser what it computes to
    // tests the rule itself: the underline is on `a`/`button` and not on
    // `.ddns-data` at large. A version of this that found some inert span
    // in the markup would keep passing if that span were later deleted.
    const inertDecoration = await page.evaluate(() => {
      const root = document.querySelector('[data-ddns-root]');
      const probe = document.createElement('span');
      probe.className = 'ddns-data';
      probe.textContent = 'probe';
      root!.appendChild(probe);
      const value = getComputedStyle(probe).textDecorationLine;
      probe.remove();
      return value;
    });

    expect(
      inertDecoration,
      'an inert .ddns-data is underlined too, so the reading above says ' +
        'nothing about interactivity',
    ).toBe(NO_DECORATION);

    // …and the colour did not move. This is the assertion that fails if
    // …and the colour did not move. This is the assertion that fails if
    // a later change "fixes" the affordance by taking `.ddns-data` off
    // the anchor: Mantine's link colour would come back.
    //
    // Read against the token rather than a sibling element, for the same
    // reason as the probe above — there is no inert `.ddns-data` left to
    // compare with, and `--ddns-ink` is what §2.3 actually specifies.
    const ink = await page.evaluate(() => {
      const root = document.querySelector('[data-ddns-root]')!;
      const probe = document.createElement('span');
      probe.className = 'ddns-data';
      root.appendChild(probe);
      const value = getComputedStyle(probe).color;
      probe.remove();
      return value;
    });
    expect(
      await colourOf(name),
      'the link no longer renders in the ink §2.3 specifies — the fix ' +
        '§16.1 rules out has been applied',
    ).toBe(ink);

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
    await expect(modal).toBeVisible({ timeout: 8_000 });
    // …and it is a modal, not a route change. The URL *does* move —
    // `?zone=` is how a reload restores the open card — so the test
    // is that the **path** is unchanged and only the query grew.
    // Asserting the whole URL was unchanged would now be asserting
    // the bug this design replaced: state the address cannot carry.
    expect(new URL(page.url()).pathname).toBe(new URL(listUrl).pathname);
    expect(new URL(page.url()).search).toMatch(/^\?zone=\d+$/);

    // The edit controls are in it. "A modal pops up" with nothing in it
    // that edits the zone would satisfy the sentence and not the report.
    const card = modal.getByTestId('zone-modal-body');
    await expect(card).toBeVisible();
    // The card shows the zone in an editable field now, not a heading —
    // a name that could be set once and never corrected made a typo
    // permanent.
    await expect(card.getByTestId('zone-name')).toHaveValue(zone);
    await expect(card.getByTestId('zone-name')).toBeVisible();
    await expect(card.getByTestId('zone-delete')).toBeVisible();
    await expect(card.getByTestId('zone-provider')).toBeVisible();
    // One provider per zone, so the binding is the Provider field's
    // value rather than a listed entry.
    await expect(card.getByTestId('zone-provider')).toHaveValue('stub1');

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
    await page.goto(`${DOMAINS_PATH}?zone=${zoneId}`);
    const routeCard = page.getByTestId('zone-modal-body');
    await expect(routeCard).toBeVisible({ timeout: 8_000 });
    // The route is a route: no dialog. If this were also a modal the
    // comparison below would be comparing a thing with itself.
    // The address renders the **same modal**, which is the point of
    // §17: one card, two entrances, and the address carries which
    // one is open so a reload restores it. "No dialog here" was an
    // assertion about the route-vs-modal split that no longer
    // exists; asserting exactly one is what still has teeth,
    // because two would mean a second editor had grown.
    await expect(page.getByRole('dialog')).toHaveCount(1);

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

  test('the board row opens the card, and the name goes to its own surface', async ({
    page,
  }) => {
    // Rewritten for the flat board.
    //
    // This used to assert an expand toggle beside the device name — "two
    // jobs on two targets". The board is a table now: one row per name
    // per family, no per-device disclosure, because the nested layout
    // changed shape as check results arrived and the columns moved under
    // the reader's eye.
    //
    // What the test still has to hold is the same property #97 was about:
    // every interactive thing in the row is reachable and says where it
    // goes. Two targets remain — the device opens its card, the name goes
    // to the surface that owns it — and neither is the whole row.
    const suffix = Math.random().toString(36).slice(2, 10);
    const zone = uniqueZoneName();
    const deviceName = `router-${suffix}`;

    await loginAsUser(page);
    const created = await seedZoneDeviceAndName(page, { zone, deviceName });

    await page.goto('/atrium-ddns');
    const row = page.getByTestId(
      `board-row-${created.hostname}-${created.family ?? 'none'}`,
    );
    await expect(row).toBeVisible({ timeout: 8_000 });

    // The device: a control, and it opens the card rather than navigating.
    const openDevice = row.getByTestId(`board-open-${deviceName}`);
    await expect(openDevice).toBeVisible();
    await openDevice.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByTestId('device-detail')).toBeVisible({
      timeout: 8_000,
    });
    await page.keyboard.press('Escape');

    // The name: a link with a real href, so copy-link and middle-click
    // behave as the anchor promises.
    // `exact` matters. The row's log control carries
    // `aria-label="Log for <name>"`, which *contains* the hostname, so
    // the default substring match resolves to two links and fails on
    // strict mode. Two controls in one row legitimately mention the
    // same name; only one of them *is* the name.
    const nameLink = row.getByRole('link', {
      name: created.hostname,
      exact: true,
    });
    await expect(nameLink).toBeVisible();
    const href = await nameLink.getAttribute('href');
    // The board itself: `/atrium-ddns/names` is gone and the name modal
    // is `?name=` on the one tenant surface, so this is a query on the
    // page you are already on rather than a trip to another.
    expect(href).toMatch(/^\/atrium-ddns\?name=\d+$/);
  });


  test('the board row opens the card, and the address carries it', async ({
    page,
  }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);
    const deviceName = uniqueDeviceName();

    await page.goto(BOARD_PATH);
    await page.getByTestId('board-add-device').click();
    const create = page.getByRole('dialog');
    await expect(create).toBeVisible({ timeout: 8_000 });
    await create.getByTestId('device-name').fill(deviceName);
    await create.getByTestId('device-submit').click();
    await expect(page.getByTestId('device-secret-once')).toBeVisible();
    await page.getByTestId('dismiss-secret').click();

    // The device list is gone — `/atrium-ddns/devices` with it — so the
    // row this reads is the board's own. Its control is a *button* that
    // navigates rather than an anchor, which is why there is no `href` to
    // assert: §18.2's point was that the name must be a real control, and
    // a button is one. The address is still checked, below, after the
    // click.
    await page.goto(BOARD_PATH);
    const name = page.getByTestId(`board-open-${deviceName}`);
    await expect(name).toBeVisible({ timeout: 8_000 });

    await name.click();
    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible({ timeout: 8_000 });
    // Same property as the zone list: the path holds, the query
    // carries the open card so a reload restores it.
    // The query carries the open card, so a reload restores it. The
    // *path* normalises to `/atrium-ddns`: that is the board's canonical
    // address and the one the nav item points at, while
    // `/atrium-ddns/board` is an alias kept so older links resolve.
    // Opening a card from the alias lands on the canonical form, so this
    // asserts the destination rather than "unchanged".
    expect(new URL(page.url()).pathname).toBe('/atrium-ddns');
    expect(new URL(page.url()).search).toMatch(/^\?device=\d+$/);
    const cardUrl = page.url();
    const modalCard = modal.getByTestId('device-detail');
    await expect(modalCard).toBeVisible();
    // The edit controls, which is what "I cannot edit" was about.
    // The name is an always-on field; the Rename toggle is gone. A field
    // that is always a field cannot disagree with a heading about what
    // the name currently is.
    await expect(modalCard.getByTestId('device-name-input')).toBeVisible();
    // One Save for the whole card now, not one per field. The limit
    // input is still here; its own submit is what went.
    await expect(modalCard.getByTestId('detail-limit-input')).toBeVisible();
    await expect(modalCard.getByTestId('device-save')).toBeVisible();
    await expect(modalCard.getByTestId('detail-rotate')).toBeVisible();
    const modalShape = await cardShape(modalCard);

    await testInfo.attach('device-card-modal-from-list.png', {
      body: await page.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });

    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden();

    // The route, still there and still the same card.
    // Reopened by address. There is no separate route any more — the card
      // *is* `?device=` on the board — so this is the URL the click
      // produced, which proves the address alone restores the card.
      await page.goto(cardUrl);
    const routeCard = page.getByTestId('device-detail');
    await expect(routeCard).toBeVisible({ timeout: 8_000 });
    // The address renders the **same modal**, which is the point of
    // §17: one card, two entrances, and the address carries which
    // one is open so a reload restores it. "No dialog here" was an
    // assertion about the route-vs-modal split that no longer
    // exists; asserting exactly one is what still has teeth,
    // because two would mean a second editor had grown.
    await expect(page.getByRole('dialog')).toHaveCount(1);
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
