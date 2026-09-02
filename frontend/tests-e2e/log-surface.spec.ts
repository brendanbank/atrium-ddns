import { expect, test, type Locator, type Page } from '@playwright/test';

import {
  API_URL,
  DOC_ADDRESS_V4,
  LOG_PATH,
  bindScriptedBackend,
  deviceCallsIn,
  loginAsUser,
  seedZoneDeviceAndName,
  uniqueDeviceName,
  uniqueZoneName,
} from './helpers';

/**
 * The log surface, in a browser — #113.
 *
 * Until this file, **no e2e spec navigated to `/atrium-ddns/logs`**.
 * Measured on the tip of `v1m7-hardening-public-release` before writing
 * it: `grep -rn 'LOG_PATH\|log-ledger\|log-row-\|/logs'
 * frontend/tests-e2e/*.spec.ts` returned nothing, against a suite of
 * seven spec files and 20 passing tests. `nav.spec.ts` asserts the
 * *href* of the `Log search` item via `DDNS_NAV_ITEMS`, which is a
 * claim about a link and not about the surface behind it: the route
 * could have stopped mounting entirely and every spec would still have
 * been green.
 *
 * That is worse here than on any other surface, because this one
 * changed most recently and most structurally — one clickable row per
 * event, the per-row detail behind a modal, the second address as a `≠`
 * marker carrying its value in a `title`, and #64's unattributable
 * count. Every one of those is covered by vitest, and vitest renders
 * into jsdom: no cascade, no portal stacking, no real click target.
 * `docs/ops/overnight-template.md` records `make test-e2e` entering the
 * gate for exactly that gap — *"a curl cannot see a nav item that never
 * mounted or a board that throws in React"* — and a modal that portals
 * outside `[data-ddns-root]` is the same class of defect one layer in.
 *
 * ## What is asserted, and what deliberately is not
 *
 * Not a port of `LogLedger.test.tsx`. Four properties, each chosen
 * because a browser is the only instrument that can read it:
 *
 *   1. the route mounts and draws a ledger, **reached by clicking the
 *      nav item** rather than by typing the URL;
 *   2. a row is a real click target and the detail modal it opens
 *      carries a field the row does not show (`Declared myip`);
 *   3. a filter reaches the address bar and survives a reload — the
 *      whole point of `useLogQuery` holding state in `location.search`
 *      rather than in React;
 *   4. the empty states are three, not one, and *not measured* renders
 *      as nothing rather than as `0`.
 *
 * ## Every address here is documentation space
 *
 * The declared address is RFC 5737 TEST-NET-3 (`DOC_ADDRESS_V4`) and
 * the address the fixture calls *from* is TEST-NET-2, pinned through
 * `X-Forwarded-For` so the row cannot pick up the machine the run
 * happened on. Zones are under RFC 6761 `.invalid`.
 */

/** RFC 5737 TEST-NET-2 — the address the NAT'd call arrives *from*.
 *
 * Deliberately different from `DOC_ADDRESS_V4`, which is what that same
 * call *declares*. `LogLedger` renders the second address only when the
 * two differ, so a fixture that used one address for both would leave
 * the `≠` marker unrendered and the assertion below vacuously true. */
const NAT_ADDRESS_V4 = '198.51.100.7';

/** The rows of the ledger.
 *
 * `[role="button"]` is what separates a row from its own cells: the
 * cells carry `data-testid="log-row-<id>-<field>"` too, and the
 * filter cells are real `<button>`s — which have the *implicit* role
 * and not the attribute, so this matches the six-or-so rows and none
 * of their contents.
 */
function ledgerRows(page: Page): Locator {
  return page
    .getByTestId('log-ledger')
    .locator('[data-testid^="log-row-"][role="button"]');
}

/** One tenant, two log lines, scripted to two different results.
 *
 * Two zones rather than two calls to one, because the scripted result
 * is a property of the provider binding (`config.result`) and the point
 * of the pair is that a filter can tell them apart. Both lines are
 * written by the wire — `deviceCallsIn`, as a router drives it — since
 * that is the only writer of `ddns_event` there is; nothing in the
 * configuration API logs a line.
 *
 * The second call is the NAT'd shape: it declares `DOC_ADDRESS_V4` and
 * arrives from `NAT_ADDRESS_V4`, which is what makes the ledger's `≠`
 * marker and the modal's `Declared myip` field carry different values.
 *
 * Both wire answers are asserted here rather than in the tests, so a
 * fixture that silently seeded the wrong thing fails naming the wire
 * body instead of surfacing as a missing row three assertions later.
 */
async function seedTwoLines(page: Page): Promise<{
  goodName: string;
  nochgName: string;
}> {
  const seeded = await seedZoneDeviceAndName(page, {
    zone: uniqueZoneName(),
    deviceName: uniqueDeviceName(),
  });

  const api = page.request;
  const zone = uniqueZoneName();
  const zoneRes = await api.post(`${API_URL}/atrium_ddns/domains`, {
    data: { name: zone },
  });
  expect(zoneRes.status(), 'the second zone was created').toBe(201);
  const domainId = (await zoneRes.json()).id as number;
  await bindScriptedBackend(api, domainId, { result: 'nochg' });

  const nochgName = `home.${zone}`;
  const nameRes = await api.post(`${API_URL}/atrium_ddns/hostnames`, {
    data: {
      name: nochgName,
      domain_id: domainId,
      device_id: seeded.deviceId,
    },
  });
  expect(nameRes.status(), 'the second name was created').toBe(201);

  const call = await deviceCallsIn(api, {
    username: seeded.username,
    secret: seeded.secret,
    hostname: nochgName,
    ip: DOC_ADDRESS_V4,
    clientIp: NAT_ADDRESS_V4,
  });
  expect(call.body, 'the scripted slot answered nochg').toContain('nochg');

  return { goodName: seeded.hostname, nochgName };
}

test.describe('the log surface renders', () => {
  test.describe.configure({ timeout: 30_000 });

  test('the nav item reaches the log, and it draws a ledger with rows', async ({
    page,
  }) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await loginAsUser(page);
    const seeded = await seedTwoLines(page);

    // From the nav item, not by typing the URL. A route that resolves
    // but has lost its sidebar entry is one of the defects the e2e
    // harness exists for, and `page.goto(LOG_PATH)` cannot see it.
    await page.goto('/');
    const nav = page.getByRole('navigation');
    const link = nav.getByRole('link', { name: 'Log search', exact: true });
    await expect(link).toBeVisible({ timeout: 8_000 });
    await link.click();

    await expect(page).toHaveURL(/\/atrium-ddns\/logs$/);
    await expect(page.getByRole('heading', { name: 'Log search' })).toBeVisible();

    // The ledger, and the two lines the wire wrote. An exact count, not
    // `>= 1`: this tenant was minted for this test and only the two
    // calls above can have logged anything, so a third row means
    // something started writing `ddns_event` that did not before —
    // which is a fact worth failing over rather than tolerating.
    await expect(page.getByTestId('log-ledger')).toBeVisible({ timeout: 8_000 });
    await expect(ledgerRows(page)).toHaveCount(2);

    // Neither of the two states an empty ledger is confused with.
    await expect(page.getByTestId('log-loading')).toHaveCount(0);
    await expect(page.getByTestId('log-error')).toHaveCount(0);

    // The head is a sibling of the rows in one grid, so a ledger that
    // renders rows and no head is a broken layout rather than a broken
    // query — invisible to every instrument except this one.
    await expect(page.getByTestId('log-head')).toBeVisible();

    // Both names are on the surface, so the rows are this tenant's own
    // and not somebody's cached page.
    const ledger = page.getByTestId('log-ledger');
    await expect(ledger).toContainText(seeded.goodName);
    await expect(ledger).toContainText(seeded.nochgName);

    expect(pageErrors, 'the page threw nothing while rendering').toEqual([]);
  });

  test('a row opens the detail, which carries a field the row does not', async ({
    page,
  }) => {
    await loginAsUser(page);
    const seeded = await seedTwoLines(page);

    await page.goto(LOG_PATH);
    await expect(page.getByTestId('log-ledger')).toBeVisible({ timeout: 8_000 });

    const row = ledgerRows(page).filter({ hasText: seeded.nochgName });
    await expect(row).toHaveCount(1);

    // The `≠` marker: the second address is rendered only when it
    // differs, and its value lives in a `title` rather than in a
    // column, because §2.5's width budget has no room for a second
    // 380px address cell. A `title` is a browser affordance; jsdom will
    // happily report one on an element nothing can hover.
    const declared = row.locator('.ddns-log__declared');
    await expect(declared).toHaveAttribute(
      'title',
      `declared myip ${DOC_ADDRESS_V4}`,
    );
    await expect(row).toContainText(NAT_ADDRESS_V4);

    // The row does not show the label. This is the assertion that makes
    // the next one mean something: if `Declared myip` were still on the
    // row, opening the modal would prove nothing about the move.
    await expect(page.getByTestId('log-detail-Declared myip')).toHaveCount(0);

    // Click the `when` cell rather than the row's centre. The cell is an
    // inert `<span>`, so the click reaches the row's own handler by
    // bubbling; the centre of the row is occupied by the device and name
    // cells, which are filter buttons.
    await row.locator('[data-testid$="-when"]').click();

    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible({ timeout: 8_000 });
    await expect(dialog.getByTestId('log-detail')).toBeVisible();

    // The field the row moved into the modal, and its neighbour — both,
    // because "the two addresses differ" is the fact this detail exists
    // to carry and one value alone cannot say it.
    await expect(dialog.getByTestId('log-detail-Declared myip')).toHaveText(
      DOC_ADDRESS_V4,
    );
    await expect(dialog.getByTestId('log-detail-Called from')).toHaveText(
      NAT_ADDRESS_V4,
    );
    // `log-detail-<label>`: the testid is derived from the field's own
    // label, so renaming the label to `Hostname` moved it. That coupling
    // is why this spec caught the rename rather than silently passing on
    // a field that had quietly become something else.
    await expect(dialog.getByTestId('log-detail-Hostname')).toHaveText(
      seeded.nochgName,
    );

    // The modal portals outside `[data-ddns-root]` and re-scopes itself
    // with `DdnsPortalScope`; without that it renders with none of
    // `ddns.css`, which no jsdom test can detect. A resolved
    // `font-family` on a `.ddns-cell` inside the dialog is the cheapest
    // proof the scope crossed the portal.
    const scoped = await dialog
      .getByTestId('log-detail-Declared myip')
      .evaluate((el) => getComputedStyle(el).fontFamily);
    expect(scoped, 'the detail rendered inside the ddns css scope').not.toBe('');

    // And it closes, so the surface is usable rather than merely
    // reachable.
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
  });

  test('a filter round-trips through the URL and survives a reload', async ({
    page,
  }) => {
    await loginAsUser(page);
    const seeded = await seedTwoLines(page);

    await page.goto(LOG_PATH);
    await expect(page.getByTestId('log-ledger')).toBeVisible({ timeout: 8_000 });
    await expect(ledgerRows(page)).toHaveCount(2);
    // No filters is a stated fact on this surface, not a silence.
    await expect(page.getByTestId('log-applied-none')).toBeVisible();

    // A Mantine `Select` whose options come from the server's own
    // vocabulary, chosen through the portalled dropdown — the exact
    // shape that has no meaning in jsdom.
    const result = page.getByTestId('filter-response-code');
    await result.click();
    await page.getByRole('option', { name: 'nochg', exact: true }).click();

    // The address bar is the filter state. `useLogQuery` writes it with
    // `replaceState`, so this is also the assertion that the write
    // happened at all.
    await expect(page).toHaveURL(/[?&]response_code=nochg\b/);

    // The chip renders `page.filters` — what the **server ran with** —
    // so this narrowing is the server's, not the component's idea of it.
    await expect(page.getByTestId('log-applied-response_code')).toContainText(
      'nochg',
    );
    await expect(ledgerRows(page)).toHaveCount(1);
    await expect(page.getByTestId('log-ledger')).toContainText(seeded.nochgName);
    await expect(page.getByTestId('log-ledger')).not.toContainText(
      seeded.goodName,
    );

    // The reload. A filter held in React state would come back cleared
    // here while the address bar still claimed it was applied — the
    // address bar being the one the reader is looking at.
    await page.reload();
    await expect(page).toHaveURL(/[?&]response_code=nochg\b/);
    await expect(page.getByTestId('log-ledger')).toBeVisible({ timeout: 8_000 });
    await expect(page.getByTestId('filter-response-code')).toHaveValue('nochg');
    await expect(ledgerRows(page)).toHaveCount(1);
    await expect(page.getByTestId('log-applied-response_code')).toContainText(
      'nochg',
    );
  });
});

test.describe('the log surface is empty in three different ways', () => {
  test.describe.configure({ timeout: 30_000 });

  test('an account nothing has called says so — and says nothing about zero', async ({
    page,
  }) => {
    // A tenant minted seconds ago with no device, so `any_rows_in_scope`
    // is `false` and this is the *never logged* nothing.
    await loginAsUser(page);
    await page.goto(LOG_PATH);

    await expect(page.getByTestId('log-empty-never')).toBeVisible({
      timeout: 8_000,
    });

    // Not any of the others, and not a spinner or a refusal. The whole
    // value of `LogEmpty` is that these are four different panels; a
    // browser is where "renders as one blank panel" becomes visible.
    await expect(page.getByTestId('log-empty-filtered')).toHaveCount(0);
    await expect(page.getByTestId('log-empty-unmatchable')).toHaveCount(0);
    await expect(page.getByTestId('log-empty-unmeasured')).toHaveCount(0);
    await expect(page.getByTestId('log-ledger')).toHaveCount(0);
    await expect(page.getByTestId('log-loading')).toHaveCount(0);
    await expect(page.getByTestId('log-error')).toHaveCount(0);

    // #64's count, in its *not asked* state. The filter is not on a
    // partially-attributed code, so the server sends `null` and the
    // component must render **nothing at all** — not "0", which is a
    // different claim and the one this whole surface exists to stop
    // being made by accident.
    await expect(page.getByTestId('log-unattributable')).toHaveCount(0);
  });

  test('the other two nothings: excluded by a filter, and a filter that cannot match', async ({
    page,
  }) => {
    await loginAsUser(page);
    await seedTwoLines(page);

    // (2) Rows exist; these filters exclude all of them. Reached through
    // the address bar because that is how a pre-applied link arrives —
    // and it is the same read of `location.search` the reload above
    // exercises.
    await page.goto(`${LOG_PATH}?client_ip=198.51.100.99`);
    await expect(page.getByTestId('log-empty-filtered')).toBeVisible({
      timeout: 8_000,
    });
    await expect(page.getByTestId('log-empty-never')).toHaveCount(0);
    await expect(page.getByTestId('log-ledger')).toHaveCount(0);

    // Its invitation is a control, not a sentence: clearing gets the
    // rows back, which is also the proof that the filter was the reason
    // they were gone.
    await page.getByTestId('log-clear-filters').click();
    await expect(page.getByTestId('log-ledger')).toBeVisible();
    await expect(ledgerRows(page)).toHaveCount(2);
    await expect(page).toHaveURL(/\/atrium-ddns\/logs$/);

    // (3) A filter that structurally cannot have matched. `rout53` is
    // not in `known_services()`, so the zero it returns is not a
    // measurement — and saying so is the difference between "no traffic
    // for that provider" and "that provider does not exist here".
    await page.goto(`${LOG_PATH}?backend_type=rout53`);
    await expect(page.getByTestId('log-empty-unmatchable')).toBeVisible({
      timeout: 8_000,
    });
    await expect(
      page.getByTestId('log-unmatchable-values'),
    ).toContainText('backend_type=rout53');
    await expect(page.getByTestId('log-empty-filtered')).toHaveCount(0);
    await expect(page.getByTestId('log-empty-never')).toHaveCount(0);

    // #64's count in one of its two *asked* states. The value depends on
    // what every other tenant on this stack has done, so what is
    // asserted is that it is a figure and carries the window it was
    // counted over — never a caption, and never absent when asked.
    await page.goto(`${LOG_PATH}?response_code=badauth`);
    await expect(page.getByTestId('log-unattributable')).toBeVisible({
      timeout: 8_000,
    });
    await expect(page.getByTestId('log-unattributable-count')).toHaveText(
      /^(No|\d+)$/,
    );
    await expect(page.getByTestId('log-unattributable-window')).toContainText(
      'counted from',
    );
  });
});
